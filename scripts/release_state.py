#!/usr/bin/env python3
"""Digest and receipt helpers for validated builds and local publishing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import uuid

from libskillpack import (
    BUILD_ROOT,
    REPO_ROOT,
    atomic_rename_at_no_replace,
)
from process_guard.authorization import allow_detached_process


VALIDATION_RECEIPT_PATH = BUILD_ROOT.parent / "validation-receipt.json"
VALIDATION_RECEIPT_SCHEMA_VERSION = 3
TREE_DIGEST_DOMAIN = b"stata-codex-skill-tree-v3\0"
CANONICAL_DIRECTORY_MODE = 0o755
CANONICAL_FILE_MODE = 0o644
SKILL_FOLDERS = ("stata-core", "stata-packages", "stata-c-plugins")
GIT_INVENTORY_TIMEOUT_SECONDS = 30
GATE_INPUT_DIRECTORY_ROOTS = (
    Path(".github"),
    Path("config"),
    Path("content"),
    Path("locks"),
    Path("manifests"),
    Path("scripts"),
    Path("templates"),
    Path("tests"),
)
GATE_INPUT_ROOT_FILES = (
    Path(".gitignore"),
    Path("Makefile"),
    Path("pyproject.toml"),
    Path("uv.lock"),
)
GATE_RUNTIME_DIRECTORY_NAMES = {
    "__pycache__",
}
GATE_RUNTIME_PATHS = {
    Path("tests/tmp"),
}
DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True)
class _GitFileSnapshot:
    name: str
    metadata: tuple[int, int, int, int, int, int, int]
    payload: bytes


@dataclass
class _GitIndexBinding:
    git_entry_kind: str
    git_entry_metadata: tuple[int, int, int, int, int, int, int]
    gitfile_payload: bytes | None
    git_directory_path: Path | None
    git_directory_fd: int
    git_directory_identity: tuple[int, int, int]
    index: _GitFileSnapshot | None
    shared_indexes: tuple[_GitFileSnapshot, ...]


@dataclass(frozen=True)
class SourcePathInventory:
    """Paths observed against one stable, descriptor-anchored Git index."""

    tracked: tuple[Path, ...]
    untracked: tuple[Path, ...]
    untracked_gate_inputs: tuple[Path, ...]


@dataclass(frozen=True)
class _SourceSnapshot:
    inventory: SourcePathInventory
    digest: str | None


class ReceiptTransactionError(RuntimeError):
    """Receipt invalidation or publication could not be completed safely."""


@dataclass
class ValidationReceiptTransaction:
    """Descriptor-retained receipt parent and its preserved transaction state."""

    receipt_path: Path
    parent_path: Path
    parent_descriptor: int
    parent_identity: tuple[int, int]
    prior_descriptor: int | None = None
    prior_identity: tuple[int, int] | None = None
    prior_backup_name: str | None = None
    temporary_descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    temporary_name: str | None = None
    temporary_size: int | None = None
    temporary_sha256: str | None = None
    published_identity: tuple[int, int] | None = None
    retained_extra_identities: list[tuple[int, int]] = field(
        default_factory=list
    )
    closed: bool = False


def _hash_records(records: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, payload in records:
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def tree_digest_records(
    records: Iterable[tuple[str, bytes, bytes]],
) -> str:
    """Hash canonical-mode, ordered, explicitly typed tree entries."""

    digest = hashlib.sha256()
    digest.update(TREE_DIGEST_DOMAIN)
    for relative, kind, payload in records:
        for field in (relative.encode("utf-8"), kind, payload):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest()


def _read_tree_file(
    parent_fd: int,
    name: str,
    display_path: Path,
    expected: os.stat_result,
) -> bytes:
    file_fd: int | None = None
    try:
        file_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(file_fd)
        if (
            _metadata_fingerprint(opened) != _metadata_fingerprint(expected)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise ValueError(f"Tree file changed while opening: {display_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        named_after = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _metadata_fingerprint(after) != _metadata_fingerprint(opened)
            or _metadata_fingerprint(named_after)
            != _metadata_fingerprint(opened)
        ):
            raise ValueError(f"Tree file changed while reading: {display_path}")
        return b"".join(chunks)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"Could not read tree file: {display_path}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _walk_tree_directory(
    directory_fd: int,
    display_path: Path,
    *,
    prefix: str = "",
) -> list[tuple[str, bytes, bytes]]:
    try:
        before = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise ValueError(f"Could not list tree directory: {display_path}") from error
    records: list[tuple[str, bytes, bytes]] = []
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        child_path = display_path / name
        try:
            metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError(f"Could not inspect tree entry: {child_path}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"Refusing to hash symlink: {child_path}")
        if stat.S_ISDIR(metadata.st_mode):
            observed_mode = stat.S_IMODE(metadata.st_mode)
            if observed_mode != CANONICAL_DIRECTORY_MODE:
                raise ValueError(
                    "Tree directory has noncanonical permissions "
                    f"{observed_mode:04o}; expected "
                    f"{CANONICAL_DIRECTORY_MODE:04o}: {child_path}"
                )
            child_fd: int | None = None
            try:
                child_fd = os.open(
                    name,
                    DIRECTORY_OPEN_FLAGS,
                    dir_fd=directory_fd,
                )
                opened = os.fstat(child_fd)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                ):
                    raise ValueError(
                        f"Tree directory changed while opening: {child_path}"
                    )
                records.append((relative, b"directory", b""))
                records.extend(
                    _walk_tree_directory(
                        child_fd,
                        child_path,
                        prefix=relative,
                    )
                )
            except ValueError:
                raise
            except OSError as error:
                raise ValueError(
                    f"Could not open tree directory: {child_path}"
                ) from error
            finally:
                if child_fd is not None:
                    os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                f"Refusing to hash unsupported tree entry: {child_path}"
            )
        observed_mode = stat.S_IMODE(metadata.st_mode)
        if observed_mode != CANONICAL_FILE_MODE:
            raise ValueError(
                "Tree file has noncanonical permissions "
                f"{observed_mode:04o}; expected "
                f"{CANONICAL_FILE_MODE:04o}: {child_path}"
            )
        records.append(
            (
                relative,
                b"file",
                _read_tree_file(
                    directory_fd,
                    name,
                    child_path,
                    metadata,
                ),
            )
        )
    try:
        after_names = sorted(os.listdir(directory_fd))
        after = os.fstat(directory_fd)
    except OSError as error:
        raise ValueError(
            f"Could not revalidate tree directory: {display_path}"
        ) from error
    if (
        names != after_names
        or _metadata_fingerprint(before) != _metadata_fingerprint(after)
    ):
        raise ValueError(
            f"Tree directory changed while hashing: {display_path}"
        )
    return records


def tree_digest(root: Path) -> str:
    """Hash canonical-mode entry paths, types, and file bytes in a skill tree."""

    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise ValueError(f"Tree does not exist: {root}") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise ValueError(f"Tree does not exist: {root}")
    root_fd: int | None = None
    try:
        root_fd = os.open(root, DIRECTORY_OPEN_FLAGS)
        opened_root = os.fstat(root_fd)
        if (
            opened_root.st_dev,
            opened_root.st_ino,
            opened_root.st_mode,
        ) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
            root_metadata.st_mode,
        ):
            raise ValueError(f"Tree changed while opening: {root}")
        records = _walk_tree_directory(root_fd, root)
        root_after = root.lstat()
        if (
            root_after.st_dev,
            root_after.st_ino,
            root_after.st_mode,
        ) != (
            opened_root.st_dev,
            opened_root.st_ino,
            opened_root.st_mode,
        ):
            raise ValueError(f"Tree root changed while hashing: {root}")
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"Could not open tree: {root}") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return tree_digest_records(records)


def _metadata_fingerprint(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_git_file_snapshot(
    directory_fd: int,
    name: str,
    display_path: Path,
) -> _GitFileSnapshot:
    try:
        named_metadata = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ValueError(
            f"Could not inspect Git metadata file: {display_path}"
        ) from error
    if not stat.S_ISREG(named_metadata.st_mode) or named_metadata.st_nlink != 1:
        raise ValueError(
            f"Git metadata file must be a singly linked regular file: {display_path}"
        )
    file_fd: int | None = None
    try:
        file_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        if _metadata_fingerprint(opened) != _metadata_fingerprint(named_metadata):
            raise ValueError(f"Git metadata file changed while opening: {display_path}")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            payload = handle.read()
            after = os.fstat(handle.fileno())
        if _metadata_fingerprint(after) != _metadata_fingerprint(opened):
            raise ValueError(f"Git metadata file changed while reading: {display_path}")
        named_after = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if _metadata_fingerprint(named_after) != _metadata_fingerprint(opened):
            raise ValueError(f"Git metadata file changed while reading: {display_path}")
        return _GitFileSnapshot(
            name=name,
            metadata=_metadata_fingerprint(opened),
            payload=payload,
        )
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"Could not read Git metadata file: {display_path}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _open_resolved_directory(path: Path) -> tuple[int, Path]:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Could not resolve Git metadata directory: {path}") from error
    if not resolved.is_absolute():
        raise ValueError(f"Git metadata directory is not absolute: {path}")
    current_fd = os.open(Path(resolved.anchor), DIRECTORY_OPEN_FLAGS)
    try:
        for part in resolved.parts[1:]:
            next_fd = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        metadata = os.fstat(current_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Git metadata path is not a directory: {resolved}")
        return current_fd, resolved
    except BaseException:
        os.close(current_fd)
        raise


def _is_shared_index_name(name: str) -> bool:
    prefix = "sharedindex."
    if not name.startswith(prefix):
        return False
    suffix = name[len(prefix) :]
    return len(suffix) in {40, 64} and all(
        character in "0123456789abcdefABCDEF" for character in suffix
    )


def _capture_git_index_binding(
    repo_root: Path,
    repository_fd: int,
) -> _GitIndexBinding:
    try:
        git_entry = os.stat(
            ".git",
            dir_fd=repository_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ValueError("Could not inspect repository Git metadata") from error

    gitfile_payload: bytes | None = None
    git_directory_path: Path | None = None
    if stat.S_ISDIR(git_entry.st_mode):
        try:
            git_directory_fd = os.open(
                ".git",
                DIRECTORY_OPEN_FLAGS,
                dir_fd=repository_fd,
            )
        except OSError as error:
            raise ValueError("Could not anchor repository Git metadata") from error
        opened_git_directory = os.fstat(git_directory_fd)
        if (
            opened_git_directory.st_dev,
            opened_git_directory.st_ino,
            opened_git_directory.st_mode,
        ) != (
            git_entry.st_dev,
            git_entry.st_ino,
            git_entry.st_mode,
        ):
            os.close(git_directory_fd)
            raise ValueError("Repository Git metadata changed while opening")
        git_entry_kind = "directory"
    elif stat.S_ISREG(git_entry.st_mode):
        gitfile = _read_git_file_snapshot(
            repository_fd,
            ".git",
            repo_root / ".git",
        )
        gitfile_payload = gitfile.payload
        try:
            text = gitfile_payload.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ValueError("Repository .git file is not valid UTF-8") from error
        prefix = "gitdir: "
        if not text.startswith(prefix) or "\n" in text:
            raise ValueError("Repository .git file has an unsupported format")
        configured = Path(text[len(prefix) :])
        if not configured.is_absolute():
            configured = repo_root / configured
        git_directory_fd, git_directory_path = _open_resolved_directory(configured)
        git_entry_kind = "file"
    else:
        raise ValueError("Repository .git entry is not a directory or gitfile")

    try:
        git_directory_metadata = os.fstat(git_directory_fd)
        git_directory_identity = (
            git_directory_metadata.st_dev,
            git_directory_metadata.st_ino,
            git_directory_metadata.st_mode,
        )
        try:
            index = _read_git_file_snapshot(
                git_directory_fd,
                "index",
                (git_directory_path or repo_root / ".git") / "index",
            )
        except ValueError as error:
            try:
                os.stat(
                    "index",
                    dir_fd=git_directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                index = None
            else:
                raise error

        try:
            shared_names = tuple(
                sorted(
                    name
                    for name in os.listdir(git_directory_fd)
                    if _is_shared_index_name(name)
                )
            )
        except OSError as error:
            raise ValueError("Could not inventory Git split-index metadata") from error
        shared_indexes = tuple(
            _read_git_file_snapshot(
                git_directory_fd,
                name,
                (git_directory_path or repo_root / ".git") / name,
            )
            for name in shared_names
        )
        return _GitIndexBinding(
            git_entry_kind=git_entry_kind,
            git_entry_metadata=_metadata_fingerprint(git_entry),
            gitfile_payload=gitfile_payload,
            git_directory_path=git_directory_path,
            git_directory_fd=git_directory_fd,
            git_directory_identity=git_directory_identity,
            index=index,
            shared_indexes=shared_indexes,
        )
    except BaseException:
        os.close(git_directory_fd)
        raise


def _assert_git_file_snapshot(
    directory_fd: int,
    expected: _GitFileSnapshot,
    display_root: Path,
) -> None:
    observed = _read_git_file_snapshot(
        directory_fd,
        expected.name,
        display_root / expected.name,
    )
    if observed != expected:
        raise ValueError(
            "Git metadata changed during source hashing: "
            f"{display_root / expected.name}"
        )


def _assert_git_index_binding(
    repo_root: Path,
    repository_fd: int,
    binding: _GitIndexBinding,
) -> None:
    try:
        git_entry = os.stat(
            ".git",
            dir_fd=repository_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ValueError("Git metadata changed during source hashing") from error
    observed_entry = _metadata_fingerprint(git_entry)
    expected_entry = binding.git_entry_metadata
    if (
        observed_entry != expected_entry
        if binding.git_entry_kind == "file"
        else observed_entry[:3] != expected_entry[:3]
    ):
        raise ValueError("Git metadata changed during source hashing")
    if binding.git_entry_kind == "file":
        gitfile = _read_git_file_snapshot(
            repository_fd,
            ".git",
            repo_root / ".git",
        )
        if gitfile.payload != binding.gitfile_payload:
            raise ValueError("Git metadata changed during source hashing")
        assert binding.git_directory_path is not None
        observed_fd, _ = _open_resolved_directory(binding.git_directory_path)
        try:
            observed_metadata = os.fstat(observed_fd)
            observed_identity = (
                observed_metadata.st_dev,
                observed_metadata.st_ino,
                observed_metadata.st_mode,
            )
            if observed_identity != binding.git_directory_identity:
                raise ValueError("Git metadata changed during source hashing")
        finally:
            os.close(observed_fd)
    else:
        observed_metadata = os.fstat(binding.git_directory_fd)
        observed_identity = (
            observed_metadata.st_dev,
            observed_metadata.st_ino,
            observed_metadata.st_mode,
        )
        if (
            observed_identity != binding.git_directory_identity
            or observed_identity != observed_entry[:3]
        ):
            raise ValueError("Git metadata changed during source hashing")

    display_root = binding.git_directory_path or repo_root / ".git"
    if binding.index is None:
        try:
            os.stat(
                "index",
                dir_fd=binding.git_directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ValueError("Could not revalidate Git index") from error
        else:
            raise ValueError("Git index changed during source hashing")
    else:
        _assert_git_file_snapshot(
            binding.git_directory_fd,
            binding.index,
            display_root,
        )

    expected_shared = tuple(snapshot.name for snapshot in binding.shared_indexes)
    try:
        observed_shared = tuple(
            sorted(
                name
                for name in os.listdir(binding.git_directory_fd)
                if _is_shared_index_name(name)
            )
        )
    except OSError as error:
        raise ValueError("Could not revalidate Git split-index metadata") from error
    if observed_shared != expected_shared:
        raise ValueError("Git split-index metadata changed during source hashing")
    for snapshot in binding.shared_indexes:
        _assert_git_file_snapshot(
            binding.git_directory_fd,
            snapshot,
            display_root,
        )


def _read_descriptor_bytes(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


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
            named_metadata = os.stat(
                relative.name,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
            file_fd = os.open(
                relative.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_fd,
            )
        except OSError as error:
            raise ValueError(
                f"Tracked source file is missing or unsafe: {relative}"
            ) from error
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(named_metadata.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _metadata_fingerprint(opened)
            != _metadata_fingerprint(named_metadata)
        ):
            raise ValueError(f"Tracked source file is non-regular: {relative}")
        payload = _read_descriptor_bytes(file_fd)
        after = os.fstat(file_fd)
        try:
            named_after = os.stat(
                relative.name,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError(
                f"Tracked source file changed while reading: {relative}"
            ) from error
        if (
            _metadata_fingerprint(after) != _metadata_fingerprint(opened)
            or _metadata_fingerprint(named_after)
            != _metadata_fingerprint(opened)
        ):
            raise ValueError(
                f"Tracked source file changed while reading: {relative}"
            )
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


def _is_gate_runtime_path(relative: Path) -> bool:
    return (
        relative in GATE_RUNTIME_PATHS
        or any(runtime_path in relative.parents for runtime_path in GATE_RUNTIME_PATHS)
        or any(part in GATE_RUNTIME_DIRECTORY_NAMES for part in relative.parts)
    )


def _walk_gate_input_entry(
    parent_fd: int,
    name: str,
    relative: Path,
    *,
    expected_file: bool = False,
) -> list[Path]:
    """Inventory one gate-input entry without following symbolic links."""

    if _is_gate_runtime_path(relative):
        return []
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ValueError(f"Could not inspect gate input: {relative}") from error
    if expected_file or not stat.S_ISDIR(named.st_mode):
        return [relative]

    child_fd: int | None = None
    try:
        child_fd = os.open(name, DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        if _metadata_fingerprint(opened) != _metadata_fingerprint(named):
            raise ValueError(f"Gate input changed while opening: {relative}")
        before_names = tuple(sorted(os.listdir(child_fd)))
        observed: list[Path] = []
        for child_name in before_names:
            observed.extend(
                _walk_gate_input_entry(
                    child_fd,
                    child_name,
                    relative / child_name,
                )
            )
        after_names = tuple(sorted(os.listdir(child_fd)))
        after = os.fstat(child_fd)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            before_names != after_names
            or _metadata_fingerprint(after) != _metadata_fingerprint(opened)
            or _metadata_fingerprint(named_after)
            != _metadata_fingerprint(opened)
        ):
            raise ValueError(f"Gate input membership changed: {relative}")
        return observed
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"Could not inventory gate input: {relative}") from error
    finally:
        if child_fd is not None:
            os.close(child_fd)


def _gate_input_inventory(repository_fd: int) -> tuple[Path, ...]:
    paths: list[Path] = []
    for relative in GATE_INPUT_DIRECTORY_ROOTS:
        paths.extend(
            _walk_gate_input_entry(
                repository_fd,
                relative.as_posix(),
                relative,
            )
        )
    for relative in GATE_INPUT_ROOT_FILES:
        paths.extend(
            _walk_gate_input_entry(
                repository_fd,
                relative.as_posix(),
                relative,
                expected_file=True,
            )
        )
    if len(paths) != len(set(paths)):
        raise ValueError("Gate input inventory returned duplicate paths")
    return tuple(sorted(paths, key=Path.as_posix))


def _parse_staged_inventory(payload: bytes) -> bytes:
    paths: list[bytes] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            header, relative = record.split(b"\t", 1)
            mode, _object_id, stage = header.split()
        except ValueError as error:
            raise ValueError("Git returned an invalid staged-file inventory") from error
        if stage != b"0":
            raise ValueError(
                "Git index contains unresolved merge stages. Resolve them "
                "before validation."
            )
        if mode == b"040000":
            raise ValueError(
                "Sparse Git indexes are not supported for source receipts. "
                "Expand the index before validation."
            )
        paths.append(relative)
    if len(paths) != len(set(paths)):
        raise ValueError("Git index returned duplicate tracked source paths")
    return b"\0".join(paths) + (b"\0" if paths else b"")


def _git_index_object_format(index: _GitFileSnapshot | None) -> str:
    if index is None:
        return "sha1"
    payload = index.payload
    candidates: list[str] = []
    if (
        len(payload) >= hashlib.sha1().digest_size
        and hashlib.sha1(payload[: -hashlib.sha1().digest_size]).digest()
        == payload[-hashlib.sha1().digest_size :]
    ):
        candidates.append("sha1")
    if (
        len(payload) >= hashlib.sha256().digest_size
        and hashlib.sha256(payload[: -hashlib.sha256().digest_size]).digest()
        == payload[-hashlib.sha256().digest_size :]
    ):
        candidates.append("sha256")
    if len(candidates) != 1:
        raise ValueError(
            "Git index checksum is invalid or uses an unsupported object format"
        )
    return candidates[0]


def _run_private_inventory(
    binding: _GitIndexBinding,
    object_format: str,
    *,
    repository_fd: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    for variable in tuple(environment):
        if variable.startswith("GIT_"):
            environment.pop(variable, None)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    with tempfile.TemporaryDirectory(
        prefix="stata-codex-git-inventory-",
    ) as temporary_root:
        private_git = Path(temporary_root)
        (private_git / "HEAD").write_text(
            "ref: refs/heads/source-digest\n",
            encoding="utf-8",
        )
        (private_git / "objects").mkdir()
        (private_git / "refs").mkdir()
        config_lines = [
            "[core]",
            (
                "\trepositoryformatversion = 1"
                if object_format == "sha256"
                else "\trepositoryformatversion = 0"
            ),
            f"\tbare = {'false' if repository_fd is not None else 'true'}",
            f"\texcludesFile = {os.devnull}",
            "\tfsmonitor = false",
            f"\thooksPath = {os.devnull}",
            "\tuntrackedCache = false",
            "\tsparseCheckout = true",
            "\tsparseCheckoutCone = true",
            "[index]",
            "\tsparse = true",
        ]
        if object_format == "sha256":
            config_lines.extend(
                [
                    "[extensions]",
                    "\tobjectFormat = sha256",
                ]
            )
        (private_git / "config").write_text(
            "\n".join(config_lines) + "\n",
            encoding="utf-8",
        )
        if binding.index is not None:
            (private_git / "index").write_bytes(binding.index.payload)
        for shared_index in binding.shared_indexes:
            (private_git / shared_index.name).write_bytes(shared_index.payload)
        private_git_fd = os.open(private_git, DIRECTORY_OPEN_FLAGS)

        if repository_fd is None:

            def enter_anchored_directory() -> None:
                os.fchdir(private_git_fd)

            command = [
                "git",
                "ls-files",
                "--cached",
                "--stage",
                "--sparse",
                "-z",
            ]
            environment["GIT_DIR"] = "."
            environment["GIT_INDEX_FILE"] = "index"
            pass_fds = (private_git_fd,)
        else:

            def enter_anchored_directory() -> None:
                os.fchdir(repository_fd)

            command = [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ]
            environment["GIT_DIR"] = str(private_git)
            environment["GIT_INDEX_FILE"] = str(private_git / "index")
            environment["GIT_WORK_TREE"] = "."
            pass_fds = (repository_fd,)
        try:
            try:
                with allow_detached_process():
                    return subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        timeout=GIT_INVENTORY_TIMEOUT_SECONDS,
                        pass_fds=pass_fds,
                        preexec_fn=enter_anchored_directory,
                        env=environment,
                    )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ValueError(
                    f"Could not inventory tracked source files: {error}"
                ) from error
        finally:
            os.close(private_git_fd)
    raise AssertionError("temporary Git inventory exited without a result")


def _tracked_inventory(binding: _GitIndexBinding) -> bytes:
    object_format = _git_index_object_format(binding.index)
    inventory = _run_private_inventory(binding, object_format)
    detail = inventory.stderr.decode("utf-8", errors="replace").strip()
    if inventory.returncode != 0 or detail:
        if "sparse index" in detail.lower():
            raise ValueError(
                "Sparse Git indexes are not supported for source receipts. "
                "Expand the index before validation."
            )
        raise ValueError(
            f"Could not inventory tracked source files: "
            f"{detail or 'git ls-files failed'}"
        )
    return _parse_staged_inventory(inventory.stdout)


def _untracked_inventory(
    binding: _GitIndexBinding,
    repository_fd: int,
) -> bytes:
    object_format = _git_index_object_format(binding.index)
    inventory = _run_private_inventory(
        binding,
        object_format,
        repository_fd=repository_fd,
    )
    detail = inventory.stderr.decode("utf-8", errors="replace").strip()
    if inventory.returncode != 0 or detail:
        raise ValueError(
            "Could not inventory untracked source files: "
            f"{detail or 'git ls-files failed'}"
        )
    return inventory.stdout


def _decode_tracked_paths(inventory: bytes) -> tuple[Path, ...]:
    paths: list[Path] = []
    for encoded_relative in inventory.split(b"\0"):
        if not encoded_relative:
            continue
        try:
            relative = Path(encoded_relative.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ValueError("Tracked source path is not valid UTF-8") from error
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError(f"Unsafe tracked source path: {relative}")
        paths.append(relative)
    return tuple(sorted(paths, key=Path.as_posix))


def _decode_untracked_paths(inventory: bytes) -> tuple[Path, ...]:
    paths: list[Path] = []
    for encoded_relative in inventory.split(b"\0"):
        if not encoded_relative:
            continue
        try:
            relative = Path(encoded_relative.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ValueError("Untracked source path is not valid UTF-8") from error
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError(f"Unsafe untracked source path: {relative}")
        paths.append(relative)
    if len(paths) != len(set(paths)):
        raise ValueError("Git returned duplicate untracked source paths")
    return tuple(sorted(paths, key=Path.as_posix))


def _capture_source_snapshot(
    repo_root: Path,
    *,
    include_digest: bool,
) -> _SourceSnapshot:
    """Bind tracked, untracked, and gate-input paths to one Git-index snapshot."""

    try:
        repository_fd = os.open(repo_root, DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise ValueError(
            f"Repository root must be a real, non-symlink directory: {error}"
        ) from error
    try:
        _assert_repository_identity(repo_root, repository_fd)
        binding = _capture_git_index_binding(repo_root, repository_fd)
        try:
            tracked = _decode_tracked_paths(_tracked_inventory(binding))
            untracked_before = _decode_untracked_paths(
                _untracked_inventory(binding, repository_fd)
            )
            gate_inputs_before = _gate_input_inventory(repository_fd)
            _assert_git_index_binding(repo_root, repository_fd, binding)
            _assert_repository_identity(repo_root, repository_fd)

            records: list[tuple[str, bytes]] = []
            if include_digest:
                for relative in tracked:
                    records.append(_tracked_file_record(repository_fd, relative))

            untracked_after = _decode_untracked_paths(
                _untracked_inventory(binding, repository_fd)
            )
            gate_inputs_after = _gate_input_inventory(repository_fd)
            _assert_git_index_binding(repo_root, repository_fd, binding)
            _assert_repository_identity(repo_root, repository_fd)
        finally:
            os.close(binding.git_directory_fd)
    finally:
        os.close(repository_fd)

    if untracked_before != untracked_after:
        raise ValueError(
            "Untracked source membership changed during inventory. "
            "Retry from a stable working tree."
        )
    if gate_inputs_before != gate_inputs_after:
        raise ValueError(
            "Gate input membership changed during inventory. "
            "Retry from a stable working tree."
        )
    tracked_set = set(tracked)
    inventory = SourcePathInventory(
        tracked=tracked,
        untracked=untracked_before,
        untracked_gate_inputs=tuple(
            path for path in gate_inputs_before if path not in tracked_set
        ),
    )
    return _SourceSnapshot(
        inventory=inventory,
        digest=_hash_records(sorted(records)) if include_digest else None,
    )


def source_path_inventory(
    repo_root: Path = REPO_ROOT,
) -> SourcePathInventory:
    """Return one stable tracked/untracked inventory for repository checks."""

    return _capture_source_snapshot(
        repo_root,
        include_digest=False,
    ).inventory


def untracked_source_paths(
    repo_root: Path = REPO_ROOT,
) -> tuple[Path, ...]:
    """Inventory nonignored worktree files absent from a stable Git index."""

    return source_path_inventory(repo_root).untracked


def _assert_no_untracked_source_files(
    repo_root: Path = REPO_ROOT,
    *,
    inventory: SourcePathInventory | None = None,
) -> None:
    """Reject unreviewed worktree files that can affect validation."""

    observed = inventory or source_path_inventory(repo_root)
    untracked = observed.untracked
    if untracked:
        displayed = ", ".join(
            json.dumps(path.as_posix(), ensure_ascii=True)
            for path in untracked[:10]
        )
        if len(untracked) > 10:
            displayed += f", and {len(untracked) - 10} more"
        raise ValueError(
            "Untracked, nonignored working-tree files prevent "
            f"receipt-bearing validation: {displayed}. Add, ignore, or "
            "remove them, then rerun validation."
        )
    untracked_set = set(observed.untracked)
    ignored_gate_inputs = tuple(
        path
        for path in observed.untracked_gate_inputs
        if path not in untracked_set
    )
    if ignored_gate_inputs:
        displayed = ", ".join(
            json.dumps(path.as_posix(), ensure_ascii=True)
            for path in ignored_gate_inputs[:10]
        )
        if len(ignored_gate_inputs) > 10:
            displayed += f", and {len(ignored_gate_inputs) - 10} more"
        raise ValueError(
            "Ignored, untracked validation inputs prevent receipt-bearing "
            f"validation: {displayed}. Build configuration, lock files, "
            "manifests, scripts, templates, tests, and CI inputs must be tracked."
        )


def tracked_source_paths(repo_root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """Inventory index paths without consulting ambient Git configuration."""

    return source_path_inventory(repo_root).tracked


def source_digest(repo_root: Path = REPO_ROOT) -> str:
    """Hash every Git-tracked working-tree file and no untracked runtime files."""

    snapshot = _capture_source_snapshot(repo_root, include_digest=True)
    assert snapshot.digest is not None
    return snapshot.digest


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


def validation_state(
    build_root: Path = BUILD_ROOT,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, str]:
    source_before = _capture_source_snapshot(repo_root, include_digest=True)
    _assert_no_untracked_source_files(
        repo_root,
        inventory=source_before.inventory,
    )
    errors = validate_complete_skill_tree(build_root)
    if errors:
        raise ValueError("; ".join(errors))
    tree_before = tree_digest(build_root)
    source_after = _capture_source_snapshot(repo_root, include_digest=True)
    _assert_no_untracked_source_files(
        repo_root,
        inventory=source_after.inventory,
    )
    tree_after = tree_digest(build_root)
    if source_before != source_after or tree_before != tree_after:
        raise ValueError(
            "Source or generated bytes changed while computing validation "
            "state; run make validate again."
        )
    assert source_after.digest is not None
    return {
        "source_sha256": source_after.digest,
        "tree_sha256": tree_after,
    }


def _receipt_entry_metadata(
    parent_descriptor: int,
    name: str,
) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _receipt_metadata_signature(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _receipt_descriptor_digest(
    descriptor: int,
    expected_size: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    offset = 0
    limit = expected_size + 1
    while offset < limit:
        chunk = os.pread(
            descriptor,
            min(1024 * 1024, limit - offset),
            offset,
        )
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return offset, digest.hexdigest()


def _verify_receipt_temporary_state(
    transaction: ValidationReceiptTransaction,
    public_name: str,
    *,
    message: str,
) -> None:
    if (
        transaction.temporary_descriptor is None
        or transaction.temporary_identity is None
        or transaction.temporary_size is None
        or transaction.temporary_sha256 is None
    ):
        raise ReceiptTransactionError(
            "validation receipt temporary has no complete expected state"
        )
    before = os.fstat(transaction.temporary_descriptor)
    observed_size, observed_digest = _receipt_descriptor_digest(
        transaction.temporary_descriptor,
        transaction.temporary_size,
    )
    after = os.fstat(transaction.temporary_descriptor)
    public = _receipt_entry_metadata(
        transaction.parent_descriptor,
        public_name,
    )
    if public is not None:
        public_identity = (public.st_dev, public.st_ino)
        if public_identity != transaction.temporary_identity:
            transaction.retained_extra_identities.append(public_identity)
    expected_signature = _receipt_metadata_signature(after)
    if (
        public is None
        or _receipt_metadata_signature(before) != expected_signature
        or _receipt_metadata_signature(public) != expected_signature
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (after.st_dev, after.st_ino) != transaction.temporary_identity
        or stat.S_IMODE(after.st_mode) != CANONICAL_FILE_MODE
        or observed_size != transaction.temporary_size
        or after.st_size != transaction.temporary_size
        or observed_digest != transaction.temporary_sha256
    ):
        raise ReceiptTransactionError(message)


def _verified_receipt_parent_path(
    transaction: ValidationReceiptTransaction,
) -> Path | None:
    try:
        candidates = [
            transaction.parent_path,
            *transaction.parent_path.parent.iterdir(),
        ]
    except BaseException:
        candidates = [transaction.parent_path]
    matches: list[Path] = []
    checked: set[Path] = set()
    for candidate in candidates:
        if candidate in checked:
            continue
        checked.add(candidate)
        try:
            metadata = candidate.lstat()
        except BaseException:
            continue
        if (
            stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino)
            == transaction.parent_identity
        ):
            matches.append(candidate)
    if len(matches) != 1:
        return None
    try:
        confirmed = matches[0].lstat()
    except BaseException:
        return None
    if (
        not stat.S_ISDIR(confirmed.st_mode)
        or (confirmed.st_dev, confirmed.st_ino)
        != transaction.parent_identity
    ):
        return None
    return matches[0]


def _receipt_identity_location(
    transaction: ValidationReceiptTransaction,
    identity: tuple[int, int] | None,
) -> str:
    if identity is None:
        return "unknown pathname (receipt identity unavailable)"
    try:
        names = [
            name
            for name in os.listdir(transaction.parent_descriptor)
            if (
                (metadata := _receipt_entry_metadata(
                    transaction.parent_descriptor,
                    name,
                ))
                is not None
                and (metadata.st_dev, metadata.st_ino) == identity
            )
        ]
        parent_path = _verified_receipt_parent_path(transaction)
        if len(names) == 1 and parent_path is not None:
            candidate = parent_path / names[0]
            confirmed = candidate.lstat()
            if (confirmed.st_dev, confirmed.st_ino) == identity:
                return str(candidate)
    except BaseException:
        pass
    return (
        "unknown pathname beneath the retained receipt parent "
        f"(device={identity[0]}, inode={identity[1]})"
    )


def retained_validation_receipt_locations(
    transaction: ValidationReceiptTransaction,
) -> tuple[str, ...]:
    """Return reverified locations for receipt state retained by a transaction."""

    identities = [
        transaction.prior_identity,
        transaction.temporary_identity,
        transaction.published_identity,
        *transaction.retained_extra_identities,
    ]
    locations: list[str] = []
    observed: set[tuple[int, int]] = set()
    for identity in identities:
        if identity is None or identity in observed:
            continue
        observed.add(identity)
        locations.append(_receipt_identity_location(transaction, identity))
    return tuple(locations)


def _assert_receipt_parent_current(
    transaction: ValidationReceiptTransaction,
) -> None:
    try:
        public = transaction.parent_path.lstat()
        held = os.fstat(transaction.parent_descriptor)
    except OSError as error:
        raise ReceiptTransactionError(
            "validation receipt parent changed during the transaction; "
            "retained parent location: "
            f"{_verified_receipt_parent_path(transaction) or 'unknown pathname'}"
        ) from error
    if (
        not stat.S_ISDIR(public.st_mode)
        or not stat.S_ISDIR(held.st_mode)
        or (public.st_dev, public.st_ino) != transaction.parent_identity
        or (held.st_dev, held.st_ino) != transaction.parent_identity
    ):
        recovered = _verified_receipt_parent_path(transaction)
        location = (
            str(recovered)
            if recovered is not None
            else (
                "unknown pathname "
                f"(device={transaction.parent_identity[0]}, "
                f"inode={transaction.parent_identity[1]})"
            )
        )
        raise ReceiptTransactionError(
            "validation receipt parent changed during the transaction; "
            f"retained parent location: {location}"
        )


def _close_receipt_transaction(
    transaction: ValidationReceiptTransaction,
    primary_error: BaseException | None = None,
    primary_traceback: object | None = None,
) -> None:
    if transaction.closed:
        if primary_error is not None:
            raise primary_error.with_traceback(primary_traceback)
        return
    transaction.closed = True
    close_errors: list[BaseException] = []
    attempted: set[int] = set()
    for descriptor in (
        transaction.temporary_descriptor,
        transaction.prior_descriptor,
        transaction.parent_descriptor,
    ):
        if descriptor is None or descriptor in attempted:
            continue
        attempted.add(descriptor)
        try:
            os.close(descriptor)
        except BaseException as error:
            close_errors.append(error)
    if primary_error is not None:
        if close_errors:
            try:
                primary_error.add_note(
                    "validation receipt descriptor finalization also "
                    "encountered: "
                    + ", ".join(
                        type(error).__name__ for error in close_errors
                    )
                )
            except BaseException:
                pass
        raise primary_error.with_traceback(primary_traceback)
    if close_errors:
        first_error = close_errors[0]
        if len(close_errors) > 1:
            try:
                first_error.add_note(
                    "additional validation receipt descriptor finalization "
                    "failures: "
                    + ", ".join(
                        type(error).__name__ for error in close_errors[1:]
                    )
                )
            except BaseException:
                pass
        raise first_error


def close_validation_receipt_transaction(
    transaction: ValidationReceiptTransaction,
) -> None:
    """Close every descriptor owned by a live receipt transaction."""

    _close_receipt_transaction(transaction)


def _open_observed_receipt(
    transaction: ValidationReceiptTransaction,
    metadata: os.stat_result,
) -> tuple[int, tuple[int, int]]:
    if not stat.S_ISREG(metadata.st_mode):
        raise ReceiptTransactionError(
            "existing validation receipt is not an ordinary file"
        )
    descriptor = os.open(
        transaction.receipt_path.name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=transaction.parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise ReceiptTransactionError(
                "existing validation receipt changed while opening it"
            )
    except BaseException as error:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            try:
                error.add_note(
                    "receipt-open descriptor finalization also encountered "
                    f"{type(close_error).__name__}"
                )
            except BaseException:
                pass
        raise
    return descriptor, identity


def begin_validation_receipt_transaction(
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
) -> ValidationReceiptTransaction:
    """Bind the receipt parent and preserve any prior receipt by identity."""

    receipt_path = Path(receipt_path).expanduser().absolute()
    parent_path = receipt_path.parent
    parent_descriptor = os.open(
        parent_path,
        DIRECTORY_OPEN_FLAGS | getattr(os, "O_CLOEXEC", 0),
    )
    transaction: ValidationReceiptTransaction | None = None
    try:
        parent_metadata = os.fstat(parent_descriptor)
        transaction = ValidationReceiptTransaction(
            receipt_path=receipt_path,
            parent_path=parent_path,
            parent_descriptor=parent_descriptor,
            parent_identity=(
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            ),
        )
        _assert_receipt_parent_current(transaction)
        existing = _receipt_entry_metadata(
            parent_descriptor,
            receipt_path.name,
        )
        if existing is None:
            return transaction
        prior_descriptor, prior_identity = _open_observed_receipt(
            transaction,
            existing,
        )
        transaction.prior_descriptor = prior_descriptor
        transaction.prior_identity = prior_identity
        backup_name: str | None = None
        for _ in range(128):
            candidate = (
                f".{receipt_path.name}.backup-{uuid.uuid4().hex}"
            )
            try:
                atomic_rename_at_no_replace(
                    parent_descriptor,
                    receipt_path.name,
                    parent_descriptor,
                    candidate,
                )
            except FileExistsError:
                continue
            except BaseException as error:
                try:
                    moved = _receipt_entry_metadata(
                        parent_descriptor,
                        candidate,
                    )
                    if (
                        moved is not None
                        and (moved.st_dev, moved.st_ino) == prior_identity
                    ):
                        transaction.prior_backup_name = candidate
                except BaseException as inspection_error:
                    try:
                        error.add_note(
                            "post-rename receipt accounting also encountered "
                            f"{type(inspection_error).__name__}"
                        )
                    except BaseException:
                        pass
                raise
            backup_name = candidate
            break
        if backup_name is None:
            raise ReceiptTransactionError(
                "could not allocate a prior-receipt backup name"
            )
        moved = _receipt_entry_metadata(parent_descriptor, backup_name)
        moved_identity = (
            (moved.st_dev, moved.st_ino)
            if moved is not None
            else None
        )
        if moved_identity != prior_identity:
            restore_error: BaseException | None = None
            if moved_identity is not None:
                transaction.retained_extra_identities.append(
                    moved_identity
                )
            if moved is not None:
                try:
                    atomic_rename_at_no_replace(
                        parent_descriptor,
                        backup_name,
                        parent_descriptor,
                        receipt_path.name,
                    )
                except BaseException as error:
                    restore_error = error
            detail = (
                "; the changed entry could not be restored without replacing "
                f"another public name: {restore_error}"
                if restore_error is not None
                else "; the changed entry was restored"
            )
            raise ReceiptTransactionError(
                "validation receipt changed during identity-preserving "
                f"invalidation{detail}"
            )
        transaction.prior_backup_name = backup_name
        _assert_receipt_parent_current(transaction)
        if (
            _receipt_entry_metadata(parent_descriptor, receipt_path.name)
            is not None
        ):
            raise ReceiptTransactionError(
                "validation receipt public name reappeared during invalidation; "
                "the prior receipt remains at "
                f"{_receipt_identity_location(transaction, prior_identity)}"
            )
        return transaction
    except BaseException as error:
        if transaction is None:
            try:
                os.close(parent_descriptor)
            except BaseException as close_error:
                try:
                    error.add_note(
                        "receipt-parent descriptor finalization also "
                        f"encountered {type(close_error).__name__}"
                    )
                except BaseException:
                    pass
            raise
        locations = retained_validation_receipt_locations(transaction)
        if locations:
            try:
                error.add_note(
                    "retained validation receipt state: "
                    + "; ".join(locations)
                )
            except BaseException:
                pass
        _close_receipt_transaction(
            transaction,
            error,
            error.__traceback__,
        )
        raise AssertionError("unreachable")


def _create_receipt_temporary(
    transaction: ValidationReceiptTransaction,
    payload: bytes,
) -> None:
    if (
        transaction.closed
        or transaction.temporary_descriptor is not None
        or transaction.published_identity is not None
    ):
        raise ReceiptTransactionError(
            "validation receipt transaction is not available for publication"
        )
    for _ in range(128):
        temporary_name = (
            f".{transaction.receipt_path.name}.tmp-{uuid.uuid4().hex}"
        )
        try:
            descriptor = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=transaction.parent_descriptor,
            )
        except FileExistsError:
            continue
        transaction.temporary_name = temporary_name
        transaction.temporary_descriptor = descriptor
        break
    if transaction.temporary_descriptor is None:
        raise ReceiptTransactionError(
            "could not allocate a validation receipt temporary file"
        )
    descriptor = transaction.temporary_descriptor
    metadata = os.fstat(descriptor)
    transaction.temporary_identity = (metadata.st_dev, metadata.st_ino)
    transaction.temporary_size = len(payload)
    transaction.temporary_sha256 = hashlib.sha256(payload).hexdigest()
    os.fchmod(descriptor, CANONICAL_FILE_MODE)
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written == 0:
            raise OSError("short write while creating validation receipt")
        remaining = remaining[written:]
    os.fsync(descriptor)
    os.fsync(transaction.parent_descriptor)
    _verify_receipt_temporary_state(
        transaction,
        transaction.temporary_name,
        message="validation receipt temporary changed during creation",
    )


def _publish_receipt_temporary(
    transaction: ValidationReceiptTransaction,
) -> None:
    if (
        transaction.temporary_descriptor is None
        or transaction.temporary_identity is None
        or transaction.temporary_name is None
    ):
        raise ReceiptTransactionError(
            "validation receipt temporary is not ready for publication"
        )
    _assert_receipt_parent_current(transaction)
    _verify_receipt_temporary_state(
        transaction,
        transaction.temporary_name,
        message="validation receipt temporary changed before publication",
    )
    atomic_rename_at_no_replace(
        transaction.parent_descriptor,
        transaction.temporary_name,
        transaction.parent_descriptor,
        transaction.receipt_path.name,
    )
    os.fsync(transaction.parent_descriptor)
    _assert_receipt_parent_current(transaction)
    _verify_receipt_temporary_state(
        transaction,
        transaction.receipt_path.name,
        message="validation receipt changed after publication",
    )
    transaction.published_identity = transaction.temporary_identity
    transaction.temporary_name = None


def write_validation_receipt(
    build_root: Path = BUILD_ROOT,
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
    expected_state: dict[str, str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
    transaction: ValidationReceiptTransaction | None = None,
) -> dict:
    current_state = validation_state(build_root, repo_root=repo_root)
    if expected_state is not None and current_state != expected_state:
        raise ValueError(
            "Source or generated bytes changed during validation; "
            "run make validate again."
        )
    payload = {
        "schema_version": VALIDATION_RECEIPT_SCHEMA_VERSION,
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
    receipt_path = Path(receipt_path).expanduser().absolute()
    owns_transaction = transaction is None
    if transaction is None:
        transaction = begin_validation_receipt_transaction(receipt_path)
    elif transaction.receipt_path != receipt_path:
        raise ValueError(
            "live validation receipt transaction targets a different path"
        )
    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        encoded_payload = (
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _create_receipt_temporary(transaction, encoded_payload)
        final_state = validation_state(build_root, repo_root=repo_root)
        if final_state != current_state:
            raise ValueError(
                "Source or generated bytes changed before receipt publication; "
                "run make validate again."
            )
        _publish_receipt_temporary(transaction)
        if transaction.prior_backup_name is not None:
            print(
                "NOTICE: prior validation receipt retained for explicit "
                "cleanup at: "
                f"{_receipt_identity_location(transaction, transaction.prior_identity)}"
            )
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
        locations = retained_validation_receipt_locations(transaction)
        if locations:
            try:
                error.add_note(
                    "retained validation receipt state: "
                    + "; ".join(locations)
                )
            except BaseException:
                pass
    if owns_transaction:
        _close_receipt_transaction(
            transaction,
            primary_error,
            primary_traceback,
        )
    elif primary_error is not None:
        raise primary_error.with_traceback(primary_traceback)
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
    if not isinstance(payload, dict):
        raise ValueError(
            f"Validation receipt has an unsupported schema: {receipt_path}"
        )
    schema_version = payload.get("schema_version")
    if schema_version != VALIDATION_RECEIPT_SCHEMA_VERSION:
        if schema_version == 1:
            raise ValueError(
                "Validation receipt schema 1 does not bind directory membership. "
                "Run make validate to create schema 3."
            )
        if schema_version == 2:
            raise ValueError(
                "Validation receipt schema 2 does not enforce canonical file "
                "and directory permissions. Run make validate to create "
                "schema 3."
            )
        raise ValueError(
            f"Validation receipt has an unsupported schema: {receipt_path}"
        )
    return payload


def verify_validation_receipt(
    build_root: Path = BUILD_ROOT,
    receipt_path: Path = VALIDATION_RECEIPT_PATH,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict:
    payload = read_validation_receipt(receipt_path)
    if payload.get("skill_folders") != list(SKILL_FOLDERS):
        raise ValueError("Validation receipt does not cover all three skills.")
    if payload.get("suites") != [
        "static",
        "core",
        "packages",
        "plugin-compile",
    ]:
        raise ValueError("Validation receipt does not cover the default gate.")
    actual_state = validation_state(build_root, repo_root=repo_root)
    if payload.get("source_sha256") != actual_state["source_sha256"]:
        raise ValueError(
            "Validation receipt is stale for the current source state. "
            "Run make validate."
        )
    if payload.get("tree_sha256") != actual_state["tree_sha256"]:
        raise ValueError(
            "Validation receipt is stale for build/generated. Run make validate."
        )
    return payload
