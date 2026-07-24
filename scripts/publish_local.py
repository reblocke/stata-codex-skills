#!/usr/bin/env python3
"""Transactionally publish a freshly validated generated skill tree."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sys
import tempfile

from libskillpack import BUILD_ROOT
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
    backup_created: bool = False
    installed: bool = False


def default_skills_dir() -> Path:
    """Resolve the destination at call time so ``CODEX_HOME`` is honored."""

    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    return codex_home / "skills"


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


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


def preflight_destinations(dest_root: Path) -> None:
    for folder in SKILL_FOLDERS:
        destination = dest_root / folder
        if destination.is_symlink():
            raise PublishError(
                f"Refusing to replace symlinked skill destination: {destination}"
            )
        if destination.exists() and not destination.is_dir():
            raise PublishError(
                f"Skill destination exists but is not a directory: {destination}"
            )


def _stage_all(source_root: Path, transaction_root: Path) -> Path:
    stage_root = transaction_root / "stage"
    stage_root.mkdir()
    for folder in SKILL_FOLDERS:
        shutil.copytree(source_root / folder, stage_root / folder)
    return stage_root


def _rollback(states: list[ReplacementState]) -> list[str]:
    errors: list[str] = []
    for state in reversed(states):
        try:
            if state.installed and _path_exists(state.destination):
                _remove_path(state.destination)
            if state.backup_created:
                if _path_exists(state.destination):
                    raise OSError(
                        f"cannot restore over existing path {state.destination}"
                    )
                os.replace(state.backup, state.destination)
            elif state.installed and state.original_existed:
                raise OSError(
                    f"original destination was not backed up: {state.destination}"
                )
        except OSError as error:
            errors.append(f"{state.destination}: {error}")
    return errors


def _swap_all(
    stage_root: Path,
    dest_root: Path,
    transaction_root: Path,
    expected_skill_digests: dict[str, str],
) -> None:
    backup_root = transaction_root / "backups"
    backup_root.mkdir()
    states: list[ReplacementState] = []
    try:
        for folder in SKILL_FOLDERS:
            destination = dest_root / folder
            state = ReplacementState(
                destination=destination,
                backup=backup_root / folder,
                staged=stage_root / folder,
                original_existed=_path_exists(destination),
            )
            states.append(state)
            if state.original_existed:
                os.replace(destination, state.backup)
                state.backup_created = True
            os.replace(state.staged, destination)
            state.installed = True
        mismatched = [
            folder
            for folder in SKILL_FOLDERS
            if tree_digest(dest_root / folder) != expected_skill_digests[folder]
        ]
        if mismatched:
            raise OSError(
                "installed skills differ from their staged bytes: "
                + ", ".join(mismatched)
            )
    except BaseException as error:
        rollback_errors = _rollback(states)
        if rollback_errors:
            raise PublishError(
                "Publication failed and rollback was incomplete. Recovery files "
                f"remain at {transaction_root}: {'; '.join(rollback_errors)}",
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

    source_root = (
        Path(source_root).expanduser() if source_root is not None else BUILD_ROOT
    )
    destination_root = (
        Path(dest_root).expanduser() if dest_root is not None else default_skills_dir()
    )
    receipt_path = Path(receipt_path).expanduser()

    preflight_skill_tree(source_root)
    try:
        receipt = verify_validation_receipt(
            build_root=source_root,
            receipt_path=receipt_path,
        )
    except ValueError as error:
        raise PublishError(str(error)) from error
    require_fresh_receipt(receipt, now=now)

    destination_root.mkdir(parents=True, exist_ok=True)
    preflight_destinations(destination_root)
    transaction_root = Path(
        tempfile.mkdtemp(prefix=TRANSACTION_PREFIX, dir=destination_root)
    )
    try:
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
        )
    except BaseException as error:
        preserve_transaction = (
            isinstance(error, PublishError) and error.preserve_transaction
        )
        if not preserve_transaction and transaction_root.exists():
            try:
                shutil.rmtree(transaction_root)
            except OSError as cleanup_error:
                raise PublishError(
                    "Publication failed and transaction cleanup also failed. "
                    f"Recovery files remain at {transaction_root}: {cleanup_error}",
                    preserve_transaction=True,
                ) from error
        raise
    else:
        if transaction_root.exists():
            try:
                shutil.rmtree(transaction_root)
            except OSError as cleanup_error:
                print(
                    "WARNING: publication committed and verified, but obsolete "
                    "transaction backup cleanup failed; recovery files remain at "
                    f"{transaction_root}: {cleanup_error}",
                    file=sys.stderr,
                )

    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dest",
        help="Target skills directory (default: $CODEX_HOME/skills or ~/.codex/skills).",
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
