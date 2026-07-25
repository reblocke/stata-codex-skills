#!/usr/bin/env python3
"""Digest and receipt helpers for validated builds and local publishing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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
VALIDATION_RECEIPT_SCHEMA_VERSION = 2
TREE_DIGEST_DOMAIN = b"stata-codex-skill-tree-v2\0"
SKILL_FOLDERS = ("stata-core", "stata-packages", "stata-c-plugins")
GIT_INVENTORY_TIMEOUT_SECONDS = 30
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
    """Hash ordered, explicitly typed entries under the v2 tree domain."""

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
    """Hash every entry path, type, and file byte in a skill tree."""

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
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    for variable in tuple(environment):
        if variable in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        } or variable.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
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
            "\tbare = true",
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

        def enter_private_git_directory() -> None:
            os.fchdir(private_git_fd)

        environment["GIT_DIR"] = "."
        environment["GIT_INDEX_FILE"] = "index"
        try:
            try:
                return subprocess.run(
                    [
                        "git",
                        "ls-files",
                        "--cached",
                        "--stage",
                        "--sparse",
                        "-z",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=GIT_INVENTORY_TIMEOUT_SECONDS,
                    pass_fds=(private_git_fd,),
                    preexec_fn=enter_private_git_directory,
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
        binding = _capture_git_index_binding(repo_root, repository_fd)
        try:
            inventory = _tracked_inventory(binding)
            _assert_git_index_binding(repo_root, repository_fd, binding)
            _assert_repository_identity(repo_root, repository_fd)
            records: list[tuple[str, bytes]] = []
            for encoded_relative in inventory.split(b"\0"):
                if not encoded_relative:
                    continue
                try:
                    relative = Path(encoded_relative.decode("utf-8"))
                except UnicodeDecodeError as error:
                    raise ValueError(
                        "Tracked source path is not valid UTF-8"
                    ) from error
                records.append(_tracked_file_record(repository_fd, relative))
            _assert_repository_identity(repo_root, repository_fd)
            _assert_git_index_binding(repo_root, repository_fd, binding)
            return _hash_records(sorted(records))
        finally:
            os.close(binding.git_directory_fd)
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
    if not isinstance(payload, dict):
        raise ValueError(
            f"Validation receipt has an unsupported schema: {receipt_path}"
        )
    schema_version = payload.get("schema_version")
    if schema_version != VALIDATION_RECEIPT_SCHEMA_VERSION:
        if schema_version == 1:
            raise ValueError(
                "Validation receipt schema 1 does not bind directory membership. "
                "Run make validate to create schema 2."
            )
        raise ValueError(
            f"Validation receipt has an unsupported schema: {receipt_path}"
        )
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
