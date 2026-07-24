#!/usr/bin/env python3
"""Transactionally publish a freshly validated generated skill tree."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

from libskillpack import BUILD_ROOT, REPO_ROOT
from release_state import (
    SKILL_FOLDERS,
    VALIDATION_RECEIPT_PATH,
    tree_digest,
    validate_complete_skill_tree,
    verify_validation_receipt,
)


RECEIPT_MAX_AGE = timedelta(hours=1)
RECEIPT_FUTURE_TOLERANCE = timedelta(minutes=1)
TRANSACTION_PREFIX = ".stata-codex-skills-publish-"
TRANSACTION_LOCK_NAME = ".stata-codex-skills-publish.lock"


class PublishError(RuntimeError):
    """A publication preflight, transaction, or rollback failed."""

    def __init__(self, message: str, *, preserve_transaction: bool = False) -> None:
        super().__init__(message)
        self.preserve_transaction = preserve_transaction


@dataclass
class ReplacementState:
    destination: Path
    backup: Path
    staged: Path
    original_existed: bool
    original_state: DestinationState
    backup_created: bool = False
    installed: bool = False
    staged_state: DestinationState | None = None
    installed_state: DestinationState | None = None


@dataclass(frozen=True)
class DestinationState:
    kind: str
    device: int | None = None
    inode: int | None = None
    tree_sha256: str | None = None


@dataclass(frozen=True)
class TransactionLock:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class DestinationRootIdentity:
    path: Path
    device: int
    inode: int


def default_skills_dir() -> Path:
    """Resolve the destination at call time so ``CODEX_HOME`` is honored."""

    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    return codex_home / "skills"


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _capture_destination_root_identity(
    destination_root: Path,
    approved_destination: Path,
) -> DestinationRootIdentity:
    try:
        metadata = destination_root.lstat()
        resolved = destination_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PublishError(
            f"Could not verify publication destination root: {destination_root}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PublishError(
            "Publication destination root must be a non-symlink directory: "
            f"{destination_root}"
        )
    if resolved != approved_destination:
        raise PublishError(
            "Publication destination root no longer resolves to the approved "
            f"path: expected {approved_destination}; observed {resolved}"
        )
    return DestinationRootIdentity(
        path=approved_destination,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _assert_destination_root(identity: DestinationRootIdentity) -> None:
    observed = _capture_destination_root_identity(identity.path, identity.path)
    if observed.device != identity.device or observed.inode != identity.inode:
        raise PublishError(
            "Publication destination root identity changed; refusing to modify "
            f"it: {identity.path}"
        )


def _acquire_transaction_lock(
    destination_root: DestinationRootIdentity,
) -> TransactionLock:
    _assert_destination_root(destination_root)
    lock_path = destination_root.path / TRANSACTION_LOCK_NAME
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        file_descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as error:
        raise PublishError(
            "Another publication transaction is active or left a recovery "
            f"lock at {lock_path}."
        ) from error
    except OSError as error:
        raise PublishError(
            f"Could not acquire publication transaction lock {lock_path}: {error}"
        ) from error

    lock_stat: os.stat_result | None = None
    try:
        lock_stat = os.fstat(file_descriptor)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException as error:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        try:
            _assert_destination_root(destination_root)
            observed = lock_path.lstat()
            if (
                lock_stat is not None
                and observed.st_dev == lock_stat.st_dev
                and observed.st_ino == lock_stat.st_ino
            ):
                lock_path.unlink()
        except (OSError, PublishError):
            pass
        raise PublishError(
            f"Could not initialize publication transaction lock {lock_path}: {error}"
        ) from error
    if lock_stat is None:
        raise PublishError(
            f"Could not initialize publication transaction lock {lock_path}"
        )
    return TransactionLock(
        path=lock_path,
        device=lock_stat.st_dev,
        inode=lock_stat.st_ino,
    )


def _assert_transaction_lock(
    lock: TransactionLock,
    destination_root: DestinationRootIdentity,
) -> None:
    _assert_destination_root(destination_root)
    try:
        observed = lock.path.lstat()
    except OSError as error:
        raise PublishError(
            f"Publication transaction lock disappeared: {lock.path}"
        ) from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_dev != lock.device
        or observed.st_ino != lock.inode
    ):
        raise PublishError(
            "Publication transaction lock was replaced; refusing to modify "
            f"destinations: {lock.path}"
        )


def _assert_mutation_authorized(
    destination_root: DestinationRootIdentity,
    lock: TransactionLock,
) -> None:
    _assert_transaction_lock(lock, destination_root)


def _release_transaction_lock(
    lock: TransactionLock,
    destination_root: DestinationRootIdentity,
) -> None:
    _assert_mutation_authorized(destination_root, lock)
    lock.path.unlink()


def _same_or_descendant(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def preflight_publication_paths(
    source_root: Path,
    destination_root: Path,
) -> tuple[Path, Path]:
    """Resolve publication roots and reject overlapping or unsafe locations."""

    source = Path(source_root).expanduser()
    destination = Path(destination_root).expanduser()
    if source.is_symlink():
        raise PublishError(f"Publication source root must not be a symlink: {source}")
    if destination.is_symlink():
        raise PublishError(
            f"Publication destination root must not be a symlink: {destination}"
        )

    try:
        canonical_source = source.resolve(strict=True)
        canonical_destination = destination.resolve(strict=False)
        canonical_repo = REPO_ROOT.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PublishError(f"Could not resolve publication paths: {error}") from error

    if (
        _same_or_descendant(canonical_destination, canonical_source)
        or _same_or_descendant(canonical_source, canonical_destination)
    ):
        raise PublishError(
            "Publication source and destination must not be equal or contain "
            f"one another: source={canonical_source}; "
            f"destination={canonical_destination}"
        )
    if _same_or_descendant(canonical_destination, canonical_repo):
        raise PublishError(
            "Publication destination must be outside the repository: "
            f"{canonical_destination}"
        )
    return canonical_source, canonical_destination


def _parse_receipt_time(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise PublishError(
            "Validation receipt has no valid validated_at timestamp. "
            "Run make validate."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PublishError(
            "Validation receipt has no valid validated_at timestamp. "
            "Run make validate."
        ) from error
    if parsed.tzinfo is None:
        raise PublishError(
            "Validation receipt timestamp has no timezone. Run make validate."
        )
    return parsed.astimezone(timezone.utc)


def require_fresh_receipt(
    payload: dict,
    *,
    now: datetime | None = None,
    max_age: timedelta = RECEIPT_MAX_AGE,
) -> None:
    """Reject receipts too old to establish a fresh validate-then-publish run."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)
    validated_at = _parse_receipt_time(payload.get("validated_at"))
    age = current_time - validated_at
    if age < -RECEIPT_FUTURE_TOLERANCE:
        raise PublishError(
            "Validation receipt timestamp is in the future. Run make validate."
        )
    if age > max_age:
        raise PublishError(
            "Validation receipt is older than one hour. Run make validate."
        )


def preflight_skill_tree(root: Path) -> None:
    """Require exactly three complete, ordinary directory trees."""

    errors = validate_complete_skill_tree(root)
    if root.is_dir():
        for folder in SKILL_FOLDERS:
            skill_root = root / folder
            if skill_root.is_symlink():
                errors.append(f"{folder}: skill root must not be a symlink")
                continue
            if not skill_root.is_dir():
                continue
            for path in skill_root.rglob("*"):
                if path.is_symlink():
                    errors.append(
                        f"{folder}: symlink is not publishable: "
                        f"{path.relative_to(skill_root)}"
                    )
                elif not (path.is_file() or path.is_dir()):
                    errors.append(
                        f"{folder}: unsupported filesystem entry: "
                        f"{path.relative_to(skill_root)}"
                    )
    if errors:
        raise PublishError("Generated skill preflight failed: " + "; ".join(errors))


def _update_digest_field(digest: object, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _destination_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        paths = sorted(root.rglob("*"))
        for path in paths:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise PublishError(
                    f"Refusing destination tree containing a symlink: {path}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                kind = b"directory"
                payload = b""
            elif stat.S_ISREG(metadata.st_mode):
                kind = b"file"
                payload = path.read_bytes()
            else:
                raise PublishError(
                    f"Refusing unsupported destination filesystem entry: {path}"
                )
            _update_digest_field(digest, relative)
            _update_digest_field(digest, kind)
            _update_digest_field(digest, payload)
    except PublishError:
        raise
    except OSError as error:
        raise PublishError(
            f"Could not capture destination state for {root}: {error}"
        ) from error
    return digest.hexdigest()


def _capture_destination_state(destination: Path) -> DestinationState:
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return DestinationState(kind="absent")
    except OSError as error:
        raise PublishError(
            f"Could not inspect skill destination {destination}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PublishError(
            f"Refusing to replace symlinked skill destination: {destination}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublishError(
            f"Skill destination exists but is not a directory: {destination}"
        )
    return DestinationState(
        kind="directory",
        device=metadata.st_dev,
        inode=metadata.st_ino,
        tree_sha256=_destination_tree_digest(destination),
    )


def preflight_destinations(dest_root: Path) -> dict[str, DestinationState]:
    states: dict[str, DestinationState] = {}
    for folder in SKILL_FOLDERS:
        destination = dest_root / folder
        states[folder] = _capture_destination_state(destination)
    return states


def _stage_all(source_root: Path, transaction_root: Path) -> Path:
    stage_root = transaction_root / "stage"
    stage_root.mkdir()
    for folder in SKILL_FOLDERS:
        shutil.copytree(source_root / folder, stage_root / folder)
    return stage_root


def _rollback(
    states: list[ReplacementState],
    destination_root: DestinationRootIdentity,
    transaction_lock: TransactionLock,
) -> list[str]:
    errors: list[str] = []
    for state in reversed(states):
        try:
            _assert_mutation_authorized(destination_root, transaction_lock)
            if state.backup_created:
                observed_backup = _capture_destination_state(state.backup)
                if observed_backup != state.original_state:
                    raise PublishError(
                        "Original skill backup changed before rollback: "
                        f"{state.backup}"
                    )
            if state.installed:
                observed_installed = _capture_destination_state(state.destination)
                if (
                    state.installed_state is None
                    or observed_installed != state.installed_state
                ):
                    raise PublishError(
                        "Installed skill changed before rollback; refusing to "
                        f"remove concurrent state: {state.destination}"
                    )
                _assert_mutation_authorized(destination_root, transaction_lock)
                _remove_path(state.destination)
            if state.backup_created:
                observed_destination = _capture_destination_state(state.destination)
                if observed_destination != DestinationState(kind="absent"):
                    raise PublishError(
                        "Skill destination is no longer absent before rollback "
                        f"restore: {state.destination}"
                    )
                _assert_mutation_authorized(destination_root, transaction_lock)
                os.replace(state.backup, state.destination)
            elif state.installed and state.original_existed:
                raise OSError(
                    f"original destination was not backed up: {state.destination}"
                )
        except (OSError, PublishError) as error:
            errors.append(f"{state.destination}: {error}")
    return errors


def _swap_all(
    stage_root: Path,
    dest_root: Path,
    transaction_root: Path,
    expected_skill_digests: dict[str, str],
    expected_destination_states: dict[str, DestinationState],
    transaction_lock: TransactionLock,
    destination_root: DestinationRootIdentity,
) -> None:
    _assert_mutation_authorized(destination_root, transaction_lock)
    observed_destination_states = preflight_destinations(dest_root)
    changed = [
        folder
        for folder in SKILL_FOLDERS
        if observed_destination_states[folder] != expected_destination_states[folder]
    ]
    if changed:
        raise PublishError(
            "Skill destinations changed after publication preflight; refusing "
            "to swap any skill: " + ", ".join(changed)
        )

    _assert_mutation_authorized(destination_root, transaction_lock)
    backup_root = transaction_root / "backups"
    backup_root.mkdir()
    states: list[ReplacementState] = []
    try:
        for folder in SKILL_FOLDERS:
            destination = dest_root / folder
            original_state = expected_destination_states[folder]
            _assert_mutation_authorized(destination_root, transaction_lock)
            observed_original = _capture_destination_state(destination)
            if observed_original != original_state:
                prior_mutation = any(
                    state.backup_created or state.installed for state in states
                )
                raise PublishError(
                    "Skill destination changed immediately before backup; "
                    f"refusing to replace it: {destination}",
                    preserve_transaction=prior_mutation,
                )
            state = ReplacementState(
                destination=destination,
                backup=backup_root / folder,
                staged=stage_root / folder,
                original_existed=original_state.kind != "absent",
                original_state=original_state,
            )
            states.append(state)
            if state.original_existed:
                _assert_mutation_authorized(destination_root, transaction_lock)
                os.replace(destination, state.backup)
                state.backup_created = True
            _assert_mutation_authorized(destination_root, transaction_lock)
            observed_destination = _capture_destination_state(destination)
            if observed_destination != DestinationState(kind="absent"):
                raise PublishError(
                    "Skill destination is not absent immediately before install; "
                    f"refusing to overwrite it: {destination}",
                    preserve_transaction=True,
                )
            state.staged_state = _capture_destination_state(state.staged)
            if state.staged_state.kind != "directory":
                raise PublishError(
                    f"Staged skill is not a directory: {state.staged}",
                    preserve_transaction=state.backup_created,
                )
            _assert_mutation_authorized(destination_root, transaction_lock)
            os.replace(state.staged, destination)
            state.installed = True
            state.installed_state = state.staged_state
        mismatched = [
            (folder, state)
            for folder, state in zip(SKILL_FOLDERS, states, strict=True)
            if (
                state.installed_state is None
                or _capture_destination_state(dest_root / folder)
                != state.installed_state
                or tree_digest(dest_root / folder)
                != expected_skill_digests[folder]
            )
        ]
        if mismatched:
            raise OSError(
                "installed skills differ from their staged bytes: "
                + ", ".join(folder for folder, _ in mismatched)
            )
    except BaseException as error:
        rollback_errors = _rollback(
            states,
            destination_root,
            transaction_lock,
        )
        preserve_conflict = (
            isinstance(error, PublishError) and error.preserve_transaction
        )
        if rollback_errors or preserve_conflict:
            details = (
                "; ".join(rollback_errors)
                if rollback_errors
                else str(error)
            )
            raise PublishError(
                "Publication failed and rollback was incomplete. Recovery files "
                f"remain at {transaction_root}: {details}",
                preserve_transaction=True,
            ) from error
        raise PublishError(
            f"Publication failed and all destinations were rolled back: {error}"
        ) from error


def publish_skills(
    *,
    source_root: Path | None = None,
    dest_root: Path | None = None,
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
    now: datetime | None = None,
) -> dict:
    """Publish all skills as one rollback-capable filesystem transaction."""

    requested_source = (
        Path(source_root).expanduser() if source_root is not None else BUILD_ROOT
    )
    requested_destination = (
        Path(dest_root).expanduser() if dest_root is not None else default_skills_dir()
    )
    receipt_path = Path(receipt_path).expanduser()
    source_root, destination_root = preflight_publication_paths(
        requested_source,
        requested_destination,
    )

    try:
        preflight_skill_tree(source_root)
        receipt = verify_validation_receipt(
            build_root=source_root,
            receipt_path=receipt_path,
        )
    except PublishError:
        raise
    except (OSError, ValueError) as error:
        raise PublishError(str(error)) from error
    require_fresh_receipt(receipt, now=now)

    transaction_lock: TransactionLock | None = None
    transaction_root: Path | None = None
    destination_identity: DestinationRootIdentity | None = None
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        destination_identity = _capture_destination_root_identity(
            destination_root,
            destination_root,
        )
        _assert_destination_root(destination_identity)
        transaction_lock = _acquire_transaction_lock(destination_identity)
        destination_states = preflight_destinations(destination_root)
        _assert_mutation_authorized(destination_identity, transaction_lock)
        transaction_root = Path(
            tempfile.mkdtemp(prefix=TRANSACTION_PREFIX, dir=destination_root)
        )
        _assert_mutation_authorized(destination_identity, transaction_lock)
        stage_root = _stage_all(source_root, transaction_root)
        preflight_skill_tree(stage_root)

        # Bind publication to the copied tree, not merely to the source tree
        # that existed before staging.
        if tree_digest(stage_root) != receipt.get("tree_sha256"):
            raise PublishError(
                "Staged skill tree differs from the validated generated tree."
            )
        try:
            staged_receipt = verify_validation_receipt(
                build_root=stage_root,
                receipt_path=receipt_path,
            )
        except ValueError as error:
            raise PublishError(str(error)) from error
        require_fresh_receipt(staged_receipt, now=now)

        expected_skill_digests = {
            folder: tree_digest(stage_root / folder)
            for folder in SKILL_FOLDERS
        }
        _swap_all(
            stage_root,
            destination_root,
            transaction_root,
            expected_skill_digests,
            destination_states,
            transaction_lock,
            destination_identity,
        )
    except BaseException as error:
        preserve_transaction = (
            isinstance(error, PublishError) and error.preserve_transaction
        )
        if (
            not preserve_transaction
            and transaction_root is not None
        ):
            try:
                if destination_identity is None or transaction_lock is None:
                    raise PublishError(
                        "Publication destination authorization context is missing"
                    )
                _assert_mutation_authorized(
                    destination_identity,
                    transaction_lock,
                )
                if transaction_root.exists():
                    _assert_mutation_authorized(
                        destination_identity,
                        transaction_lock,
                    )
                    shutil.rmtree(transaction_root)
            except (OSError, PublishError) as cleanup_error:
                raise PublishError(
                    "Publication failed and transaction cleanup also failed. "
                    f"Recovery files and the transaction lock remain at "
                    f"{transaction_root}: {cleanup_error}",
                    preserve_transaction=True,
                ) from error
        if not preserve_transaction and transaction_lock is not None:
            try:
                if destination_identity is None:
                    raise PublishError(
                        "Publication destination authorization context is missing"
                    )
                _release_transaction_lock(
                    transaction_lock,
                    destination_identity,
                )
            except (OSError, PublishError) as cleanup_error:
                raise PublishError(
                    "Publication failed and transaction-lock cleanup also "
                    f"failed; the lock remains at {transaction_lock.path}: "
                    f"{cleanup_error}",
                    preserve_transaction=True,
                ) from error
        if isinstance(error, PublishError):
            raise
        if isinstance(error, OSError):
            raise PublishError(f"Publication staging failed: {error}") from error
        raise
    else:
        if transaction_root is not None:
            try:
                if destination_identity is None or transaction_lock is None:
                    raise PublishError(
                        "Publication destination authorization context is missing"
                    )
                _assert_mutation_authorized(
                    destination_identity,
                    transaction_lock,
                )
                if transaction_root.exists():
                    _assert_mutation_authorized(
                        destination_identity,
                        transaction_lock,
                    )
                    shutil.rmtree(transaction_root)
            except (OSError, PublishError) as cleanup_error:
                print(
                    "WARNING: publication committed and verified, but obsolete "
                    "transaction backup cleanup failed; recovery files remain at "
                    f"{transaction_root}: {cleanup_error}",
                    file=sys.stderr,
                )
        if transaction_lock is not None:
            try:
                if destination_identity is None:
                    raise PublishError(
                        "Publication destination authorization context is missing"
                    )
                _release_transaction_lock(
                    transaction_lock,
                    destination_identity,
                )
            except (OSError, PublishError) as cleanup_error:
                print(
                    "WARNING: publication committed and verified, but the "
                    "transaction lock could not be removed; the lock remains at "
                    f"{transaction_lock.path}: {cleanup_error}",
                    file=sys.stderr,
                )

    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dest",
        help=(
            "External target skills directory "
            "(default: $CODEX_HOME/skills or ~/.codex/skills)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        publish_skills(
            dest_root=Path(args.dest) if args.dest else None,
            receipt_path=BUILD_ROOT.parent / VALIDATION_RECEIPT_PATH.name,
        )
    except (OSError, PublishError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    destination = Path(args.dest).expanduser() if args.dest else default_skills_dir()
    print(
        "Published validated Stata skills with rollback protection to "
        f"{destination}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
