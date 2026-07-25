#!/usr/bin/env python3
"""Digest and receipt helpers for validated builds and local publishing."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from libskillpack import BUILD_ROOT, REPO_ROOT


VALIDATION_RECEIPT_PATH = BUILD_ROOT.parent / "validation-receipt.json"
SKILL_FOLDERS = ("stata-core", "stata-packages", "stata-c-plugins")
GIT_INVENTORY_TIMEOUT_SECONDS = 30
DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _hash_records(records: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, payload in records:
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _file_records(root: Path, paths: list[Path]) -> list[tuple[str, bytes]]:
    records: list[tuple[str, bytes]] = []
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError(f"Refusing to hash symlink: {path}")
        if not path.is_file():
            continue
        records.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return records


def tree_digest(root: Path) -> str:
    """Hash every file path and byte in a generated or installed tree."""

    if not root.is_dir():
        raise ValueError(f"Tree does not exist: {root}")
    paths = [path for path in root.rglob("*") if path.is_file()]
    return _hash_records(_file_records(root, paths))


def _tracked_file_record(root_fd: int, relative: Path) -> tuple[str, bytes]:
    """Read one tracked file through no-follow descriptor-relative components."""

    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError(f"Unsafe tracked source path: {relative}")
    current_fd = os.dup(root_fd)
    file_fd: int | None = None
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    DIRECTORY_OPEN_FLAGS,
                    dir_fd=current_fd,
                )
            except OSError as error:
                raise ValueError(
                    f"Tracked source path has an unsafe ancestor: {relative}"
                ) from error
            os.close(current_fd)
            current_fd = next_fd
        try:
            file_fd = os.open(
                relative.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_fd,
            )
        except OSError as error:
            raise ValueError(
                f"Tracked source file is missing or unsafe: {relative}"
            ) from error
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Tracked source file is non-regular: {relative}")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            payload = handle.read()
        return relative.as_posix(), payload
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def _assert_repository_identity(repo_root: Path, repository_fd: int) -> None:
    try:
        observed_fd = os.open(repo_root, DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise ValueError("Repository root changed during source hashing") from error
    try:
        held = os.fstat(repository_fd)
        observed = os.fstat(observed_fd)
        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
            raise ValueError("Repository root changed during source hashing")
    finally:
        os.close(observed_fd)


def _tracked_inventory(repository_fd: int) -> bytes:
    def enter_repository() -> None:
        os.fchdir(repository_fd)

    environment = os.environ.copy()
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        environment.pop(variable, None)
    try:
        inventory = subprocess.run(
            ["git", "ls-files", "--cached", "-z"],
            check=False,
            capture_output=True,
            timeout=GIT_INVENTORY_TIMEOUT_SECONDS,
            pass_fds=(repository_fd,),
            preexec_fn=enter_repository,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Could not inventory tracked source files: {error}") from error
    if inventory.returncode != 0:
        detail = inventory.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"Could not inventory tracked source files: {detail or 'git ls-files failed'}"
        )
    return inventory.stdout


def source_digest(repo_root: Path = REPO_ROOT) -> str:
    """Hash every Git-tracked working-tree file and no untracked runtime files."""

    try:
        repository_fd = os.open(repo_root, DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise ValueError(
            f"Repository root must be a real, non-symlink directory: {error}"
        ) from error
    try:
        _assert_repository_identity(repo_root, repository_fd)
        inventory = _tracked_inventory(repository_fd)
        _assert_repository_identity(repo_root, repository_fd)
        records: list[tuple[str, bytes]] = []
        for encoded_relative in inventory.split(b"\0"):
            if not encoded_relative:
                continue
            try:
                relative = Path(encoded_relative.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise ValueError("Tracked source path is not valid UTF-8") from error
            records.append(_tracked_file_record(repository_fd, relative))
        _assert_repository_identity(repo_root, repository_fd)
        return _hash_records(sorted(records))
    finally:
        os.close(repository_fd)


def expected_skill_folders(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path.name for path in root.iterdir() if path.is_dir()))


def validate_complete_skill_tree(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return [f"missing skill tree: {root}"]
    observed_entries = tuple(sorted(path.name for path in root.iterdir()))
    observed = expected_skill_folders(root)
    expected = tuple(sorted(SKILL_FOLDERS))
    if observed_entries != expected:
        errors.append(
            "top-level entries differ: "
            f"expected {', '.join(expected)}; "
            f"observed {', '.join(observed_entries)}"
        )
    if observed != expected:
        errors.append(
            "skill folders differ: "
            f"expected {', '.join(expected)}; observed {', '.join(observed)}"
        )
    for folder in SKILL_FOLDERS:
        skill_root = root / folder
        if not (skill_root / "SKILL.md").is_file():
            errors.append(f"{folder}: missing SKILL.md")
        if not (skill_root / "PROVENANCE.md").is_file():
            errors.append(f"{folder}: missing PROVENANCE.md")
        if not (skill_root / "agents" / "openai.yaml").is_file():
            errors.append(f"{folder}: missing agents/openai.yaml")
    return errors


def validation_state(build_root: Path = BUILD_ROOT) -> dict[str, str]:
    errors = validate_complete_skill_tree(build_root)
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "source_sha256": source_digest(),
        "tree_sha256": tree_digest(build_root),
    }


def write_validation_receipt(
    build_root: Path = BUILD_ROOT,
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
    expected_state: dict[str, str] | None = None,
) -> dict:
    current_state = validation_state(build_root)
    if expected_state is not None and current_state != expected_state:
        raise ValueError(
            "Source or generated bytes changed during validation; "
            "run make validate again."
        )
    payload = {
        "schema_version": 1,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        **current_state,
        "skill_folders": list(SKILL_FOLDERS),
        "suites": [
            "static",
            "core",
            "packages",
            "plugin-compile",
        ],
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{receipt_path.name}.",
        suffix=".tmp",
        dir=receipt_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(receipt_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return payload


def read_validation_receipt(
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
) -> dict:
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(
            f"Validation receipt is missing: {receipt_path}. Run make validate."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Validation receipt is unreadable: {receipt_path}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"Validation receipt has an unsupported schema: {receipt_path}")
    return payload


def verify_validation_receipt(
    build_root: Path = BUILD_ROOT,
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
) -> dict:
    payload = read_validation_receipt(receipt_path)
    errors = validate_complete_skill_tree(build_root)
    if errors:
        raise ValueError("; ".join(errors))
    if payload.get("skill_folders") != list(SKILL_FOLDERS):
        raise ValueError("Validation receipt does not cover all three skills.")
    if payload.get("suites") != [
        "static",
        "core",
        "packages",
        "plugin-compile",
    ]:
        raise ValueError("Validation receipt does not cover the default gate.")
    actual_source = source_digest()
    if payload.get("source_sha256") != actual_source:
        raise ValueError(
            "Validation receipt is stale for the current source state. "
            "Run make validate."
        )
    actual_tree = tree_digest(build_root)
    if payload.get("tree_sha256") != actual_tree:
        raise ValueError(
            "Validation receipt is stale for build/generated. Run make validate."
        )
    return payload
