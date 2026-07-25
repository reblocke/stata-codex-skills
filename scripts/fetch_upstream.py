#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import ctypes
import hashlib
import os
import re
import secrets
import stat
import subprocess
import sys
import yaml

from libskillpack import (
    LOCK_ROOT,
    RAW_ROOT,
    REPO_ROOT,
    UPSTREAM_REPO_DIR,
    UPSTREAM_REPO_URL,
    read_yaml,
)


UPSTREAM_ROOTS = {
    "core": Path("plugins/stata/skills/stata/references"),
    "packages": Path("plugins/stata/skills/stata/packages"),
    "plugins": Path("plugins/stata-c-plugins/skills/stata-c-plugins/references"),
}
CANDIDATE_REPORT = RAW_ROOT / "candidates" / "upstream-comparison.yaml"
UPSTREAM_LOCK_PATH = LOCK_ROOT / "upstream.yaml"
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
LOCAL_GIT_TIMEOUT_SECONDS = 30
NETWORK_GIT_TIMEOUT_SECONDS = 120
UPSTREAM_CHECKOUT_RELATIVE = Path("upstream") / "stata-skill"
CHECKOUT_OWNER_MARKER = "stata-codex-skills-owner"
CHECKOUT_OWNER_CONTENT = "stata-codex-skills upstream checkout v1\n"
DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass
class ReportTarget:
    path: Path
    parent_fd: int

    @property
    def name(self) -> str:
        return self.path.name

    def close(self) -> None:
        os.close(self.parent_fd)


@dataclass(frozen=True)
class TemporaryFileState:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    sha256: str


def absolute_without_resolving(path: Path) -> Path:
    """Return a normalized absolute path without following any symlink."""

    return Path(os.path.abspath(os.fspath(path)))


def validate_repository_layout() -> tuple[Path, Path]:
    """Require the configured raw paths to have their exact repository identity."""

    repository_root = absolute_without_resolving(REPO_ROOT)
    raw_root = absolute_without_resolving(RAW_ROOT)
    checkout = absolute_without_resolving(UPSTREAM_REPO_DIR)
    if raw_root != repository_root / "raw":
        raise RuntimeError("Raw root is not the repository's dedicated raw/ directory")
    if checkout != raw_root / UPSTREAM_CHECKOUT_RELATIVE:
        raise RuntimeError(
            "Raw upstream checkout is not the dedicated raw/upstream/stata-skill path"
        )
    try:
        repository_fd = os.open(repository_root, DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise RuntimeError(
            f"Repository root must be a real, non-symlink directory: {error}"
        ) from error
    os.close(repository_fd)
    return repository_root, raw_root


def open_directory_chain(
    repository_root: Path,
    relative_path: Path,
    *,
    create: bool,
) -> int:
    """Open a repository-relative directory without following any component."""

    if relative_path.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        raise RuntimeError(f"Unsafe repository-relative directory: {relative_path}")
    try:
        current_fd = os.open(repository_root, DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise RuntimeError(
            f"Repository root must be a real, non-symlink directory: {error}"
        ) from error
    try:
        for part in relative_path.parts:
            try:
                next_fd = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    next_fd = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
                except OSError as error:
                    raise RuntimeError(
                        f"Could not create safe directory {relative_path}: {error}"
                    ) from error
            except OSError as error:
                raise RuntimeError(
                    f"Directory path must not contain symlinks: {relative_path}: {error}"
                ) from error
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def assert_checkout_identity(checkout_fd: int) -> None:
    """Confirm the public checkout path still names the held directory."""

    repository_root, _ = validate_repository_layout()
    try:
        observed_fd = open_directory_chain(
            repository_root,
            Path("raw") / UPSTREAM_CHECKOUT_RELATIVE,
            create=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Raw upstream checkout disappeared") from error
    try:
        held = os.fstat(checkout_fd)
        observed = os.fstat(observed_fd)
        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
            raise RuntimeError("Raw upstream checkout changed during refresh")
    finally:
        os.close(observed_fd)


def open_git_directory(checkout_fd: int) -> int:
    try:
        return os.open(".git", DIRECTORY_OPEN_FLAGS, dir_fd=checkout_fd)
    except OSError as error:
        raise RuntimeError(
            "Raw upstream checkout must have a real, dedicated .git directory"
        ) from error


def write_checkout_owner_marker(checkout_fd: int) -> None:
    git_fd = open_git_directory(checkout_fd)
    marker_fd: int | None = None
    try:
        marker_fd = os.open(
            CHECKOUT_OWNER_MARKER,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=git_fd,
        )
        with os.fdopen(marker_fd, "w", encoding="utf-8") as handle:
            marker_fd = None
            handle.write(CHECKOUT_OWNER_CONTENT)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(git_fd)
    except OSError as error:
        raise RuntimeError(
            "Could not establish ownership of the raw upstream checkout"
        ) from error
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        os.close(git_fd)


def verify_checkout_owner_marker(
    checkout_fd: int,
    *,
    allow_missing: bool = False,
) -> bool:
    git_fd = open_git_directory(checkout_fd)
    marker_fd: int | None = None
    try:
        marker_fd = os.open(
            CHECKOUT_OWNER_MARKER,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=git_fd,
        )
        metadata = os.fstat(marker_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("Raw upstream checkout owner marker is not a regular file")
        with os.fdopen(marker_fd, "r", encoding="utf-8") as handle:
            marker_fd = None
            content = handle.read()
        if content != CHECKOUT_OWNER_CONTENT:
            raise RuntimeError("Raw upstream checkout owner marker is invalid")
    except FileNotFoundError as error:
        if allow_missing:
            return False
        raise RuntimeError(
            "Existing raw upstream checkout is not owned by this repository"
        ) from error
    except OSError as error:
        raise RuntimeError(
            "Could not verify ownership of the raw upstream checkout"
        ) from error
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        os.close(git_fd)
    return True


def exact_commit(value: str) -> str:
    """Accept only an explicit full Git object ID.

    Branches, tags, abbreviated hashes, and expressions such as ``HEAD~1`` are
    deliberately rejected so a refresh cannot silently move to a newer
    revision.
    """

    if not COMMIT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "--upstream-ref must be an exact 40-character hexadecimal commit"
        )
    return value.lower()


def run_anchored_git(
    arguments: list[str],
    checkout_fd: int,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run Git with its working directory fixed to a held directory descriptor."""

    def enter_checkout() -> None:
        os.fchdir(checkout_fd)

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
        return subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            pass_fds=(checkout_fd,),
            preexec_fn=enter_checkout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode("utf-8", "ignore")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            error.stderr.decode("utf-8", "ignore")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        timeout_message = f"Command timed out after {timeout_seconds} seconds."
        return subprocess.CompletedProcess(
            arguments,
            124,
            stdout,
            f"{stderr}\n{timeout_message}".strip(),
        )


def run_anchored_git_bytes(
    arguments: list[str],
    checkout_fd: int,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    """Run anchored Git while preserving exact binary stdout."""

    def enter_checkout() -> None:
        os.fchdir(checkout_fd)

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
        return subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            pass_fds=(checkout_fd,),
            preexec_fn=enter_checkout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        timeout_message = (
            f"Command timed out after {timeout_seconds} seconds.".encode("utf-8")
        )
        return subprocess.CompletedProcess(
            arguments,
            124,
            stdout,
            b"\n".join(part for part in (stderr, timeout_message) if part),
        )


def checked_checkout_git(
    arguments: list[str],
    checkout_fd: int,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
    action: str,
) -> str:
    result = run_anchored_git(
        arguments,
        checkout_fd,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            raise RuntimeError(f"{action}: {detail}")
        raise RuntimeError(action)
    return result.stdout.strip()


def checked_checkout_git_bytes(
    arguments: list[str],
    checkout_fd: int,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
    action: str,
) -> bytes:
    result = run_anchored_git_bytes(
        arguments,
        checkout_fd,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(
            "utf-8",
            errors="replace",
        ).strip()
        if detail:
            raise RuntimeError(f"{action}: {detail}")
        raise RuntimeError(action)
    return result.stdout


def initialize_upstream_repo(*, allow_create: bool) -> int:
    """Open only this repository's owned, dedicated upstream checkout."""

    repository_root, _ = validate_repository_layout()
    checkout_relative = Path("raw") / UPSTREAM_CHECKOUT_RELATIVE
    created = False
    try:
        checkout_fd = open_directory_chain(
            repository_root,
            checkout_relative,
            create=False,
        )
    except FileNotFoundError:
        if not allow_create:
            raise RuntimeError(
                "Raw upstream checkout is missing; offline refresh cannot continue"
            )
        checkout_fd = open_directory_chain(
            repository_root,
            checkout_relative,
            create=True,
        )
        created = True

    try:
        assert_checkout_identity(checkout_fd)
        if created:
            if os.listdir(checkout_fd):
                raise RuntimeError(
                    "New raw upstream checkout directory is unexpectedly nonempty"
                )
            checked_checkout_git(
                ["git", "init", "."],
                checkout_fd,
                action="Could not initialize raw upstream checkout",
            )
            assert_checkout_identity(checkout_fd)
            marker_present = False
        else:
            marker_present = verify_checkout_owner_marker(
                checkout_fd,
                allow_missing=True,
            )

        top_level = Path(
            checked_checkout_git(
                [
                    "git",
                    "rev-parse",
                    "--show-toplevel",
                ],
                checkout_fd,
                action="Raw upstream checkout is not a Git repository",
            )
        )
        try:
            top_level_fd = os.open(top_level, DIRECTORY_OPEN_FLAGS)
        except OSError as error:
            raise RuntimeError(
                "Raw upstream checkout top level is not a real directory"
            ) from error
        try:
            checkout_metadata = os.fstat(checkout_fd)
            top_level_metadata = os.fstat(top_level_fd)
            if (
                checkout_metadata.st_dev,
                checkout_metadata.st_ino,
            ) != (
                top_level_metadata.st_dev,
                top_level_metadata.st_ino,
            ):
                raise RuntimeError(
                    "Raw upstream checkout is not the top level of its Git repository"
                )
        finally:
            os.close(top_level_fd)
        assert_checkout_identity(checkout_fd)

        remote = run_anchored_git(
            ["git", "remote", "get-url", "origin"],
            checkout_fd,
            timeout_seconds=LOCAL_GIT_TIMEOUT_SECONDS,
        )
        if remote.returncode != 0:
            if not created:
                raise RuntimeError(
                    "Existing raw upstream checkout must already have the exact "
                    "configured origin"
                )
            checked_checkout_git(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    UPSTREAM_REPO_URL,
                ],
                checkout_fd,
                action="Could not configure the upstream remote",
            )
        elif remote.stdout.strip() != UPSTREAM_REPO_URL:
            raise RuntimeError(
                "Raw upstream checkout origin does not match the configured repository"
            )

        if not marker_present:
            require_clean_upstream_repo(checkout_fd)
            assert_checkout_identity(checkout_fd)
            write_checkout_owner_marker(checkout_fd)
    except BaseException:
        os.close(checkout_fd)
        raise
    return checkout_fd


def require_clean_upstream_repo(checkout_fd: int) -> None:
    status = checked_checkout_git(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        checkout_fd,
        action="Could not inspect raw upstream checkout state",
    )
    if status:
        raise RuntimeError(
            "Raw upstream checkout has tracked, untracked, or ignored changes; "
            "refusing to replace local work"
        )


def refresh_upstream_repo(upstream_ref: str, *, offline: bool = False) -> None:
    """Check out exactly ``upstream_ref`` in detached-HEAD state.

    Online refreshes fetch only the requested object. Offline refreshes require
    the exact object to be present already. Neither mode resolves a branch or
    pulls a moving default branch.
    """

    checkout_fd = initialize_upstream_repo(allow_create=not offline)
    try:
        require_clean_upstream_repo(checkout_fd)
        assert_checkout_identity(checkout_fd)
        if not offline:
            checked_checkout_git(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "--force",
                    "origin",
                    upstream_ref,
                ],
                checkout_fd,
                timeout_seconds=NETWORK_GIT_TIMEOUT_SECONDS,
                action=f"Could not fetch requested upstream commit {upstream_ref}",
            )

        assert_checkout_identity(checkout_fd)
        resolved = checked_checkout_git(
            [
                "git",
                "rev-parse",
                "--verify",
                f"{upstream_ref}^{{commit}}",
            ],
            checkout_fd,
            action=f"Requested upstream commit is unavailable: {upstream_ref}",
        ).lower()
        if resolved != upstream_ref:
            raise RuntimeError(
                f"Requested upstream commit resolved unexpectedly: {resolved}"
            )

        require_clean_upstream_repo(checkout_fd)
        assert_checkout_identity(checkout_fd)
        checked_checkout_git(
            [
                "git",
                "checkout",
                "--detach",
                upstream_ref,
            ],
            checkout_fd,
            action=f"Could not check out requested upstream commit {upstream_ref}",
        )
        assert_checkout_identity(checkout_fd)
        if upstream_commit(checkout_fd) != upstream_ref:
            raise RuntimeError(
                "Raw upstream checkout did not land on the requested commit"
            )

        symbolic_head = run_anchored_git(
            ["git", "symbolic-ref", "-q", "HEAD"],
            checkout_fd,
            timeout_seconds=LOCAL_GIT_TIMEOUT_SECONDS,
        )
        if symbolic_head.returncode == 0:
            raise RuntimeError("Raw upstream checkout is not detached")
        if symbolic_head.returncode != 1:
            raise RuntimeError(
                symbolic_head.stderr.strip()
                or "Could not verify detached upstream checkout"
            )
    finally:
        os.close(checkout_fd)


def upstream_commit(checkout_fd: int) -> str:
    return checked_checkout_git(
        ["git", "rev-parse", "HEAD"],
        checkout_fd,
        action="Could not read upstream commit",
    ).lower()


def tracked_markdown_inventory(
    relative_root: Path,
    checkout_fd: int,
) -> list[dict[str, str]]:
    output = checked_checkout_git_bytes(
        [
            "git",
            "ls-tree",
            "-r",
            "-z",
            "HEAD",
            "--",
            relative_root.as_posix(),
        ],
        checkout_fd,
        action=f"Could not inventory upstream path {relative_root}",
    )
    entries: list[dict[str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split()
            relative_path = Path(encoded_path.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("Could not parse tracked upstream tree entry") from error
        if (
            relative_path.is_relative_to(relative_root)
            and relative_path.suffix == ".md"
            and relative_path.name != "filing-issues.md"
        ):
            if mode not in {b"100644", b"100755"} or object_type != b"blob":
                raise RuntimeError(
                    f"Tracked upstream Markdown is not a regular file: {relative_path}"
                )
            blob = checked_checkout_git_bytes(
                ["git", "cat-file", "blob", object_id.decode("ascii")],
                checkout_fd,
                action=f"Could not read tracked upstream blob {relative_path}",
            )
            entries.append(
                {
                    "path": relative_path.as_posix(),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
    return sorted(entries, key=lambda item: item["path"])


def build_inventory() -> dict:
    checkout_fd = initialize_upstream_repo(allow_create=False)
    try:
        inventory: dict[str, list[dict]] = {}
        for skill_key, relative_root in UPSTREAM_ROOTS.items():
            inventory[skill_key] = tracked_markdown_inventory(
                relative_root,
                checkout_fd,
            )
        result = {
            "repository": UPSTREAM_REPO_URL,
            "commit": upstream_commit(checkout_fd),
            "inventory": inventory,
        }
        assert_checkout_identity(checkout_fd)
        return result
    finally:
        os.close(checkout_fd)


def inventory_by_path(inventory: dict) -> dict[str, str]:
    return {
        item["path"]: item["sha256"]
        for items in inventory["inventory"].values()
        for item in items
    }


def build_comparison_report(inventory: dict, upstream_ref: str) -> dict:
    reviewed_lock = read_yaml(UPSTREAM_LOCK_PATH)
    reviewed_files = {
        path: metadata["sha256"]
        for path, metadata in reviewed_lock.get("files", {}).items()
    }
    candidate_files = inventory_by_path(inventory)

    reviewed_paths = set(reviewed_files)
    candidate_paths = set(candidate_files)
    added_paths = sorted(candidate_paths - reviewed_paths)
    removed_paths = sorted(reviewed_paths - candidate_paths)
    changed_paths = sorted(
        path
        for path in reviewed_paths & candidate_paths
        if reviewed_files[path] != candidate_files[path]
    )
    unchanged_paths = sorted(
        path
        for path in reviewed_paths & candidate_paths
        if reviewed_files[path] == candidate_files[path]
    )

    reviewed_repository = reviewed_lock.get("repository", {})
    return {
        "schema_version": 1,
        "report_type": "upstream-comparison",
        "repository": {
            "url": UPSTREAM_REPO_URL,
            "requested_commit": upstream_ref,
            "resolved_commit": inventory["commit"],
            "reviewed_commit": reviewed_repository.get("commit"),
        },
        "summary": {
            "added": len(added_paths),
            "removed": len(removed_paths),
            "changed": len(changed_paths),
            "unchanged": len(unchanged_paths),
        },
        "changes": {
            "added": [
                {"path": path, "candidate_sha256": candidate_files[path]}
                for path in added_paths
            ],
            "removed": [
                {"path": path, "reviewed_sha256": reviewed_files[path]}
                for path in removed_paths
            ],
            "changed": [
                {
                    "path": path,
                    "reviewed_sha256": reviewed_files[path],
                    "candidate_sha256": candidate_files[path],
                }
                for path in changed_paths
            ],
        },
        "candidate_inventory": inventory["inventory"],
        "promotion": {
            "performed": False,
            "note": (
                "Review this ignored report, then promote content and lock changes "
                "in a separate intentional edit."
            ),
        },
    }


def validate_report_path(report: Path) -> Path:
    _, raw_root = validate_repository_layout()
    report_path = absolute_without_resolving(report)
    try:
        relative_report = report_path.relative_to(raw_root)
    except ValueError as error:
        raise RuntimeError(
            "Comparison report must stay under the ignored raw/ directory"
        ) from error
    if not relative_report.parts or report_path == raw_root:
        raise RuntimeError("Comparison report must name a file under raw/")
    return report_path


def open_report_target(report_path: Path) -> ReportTarget:
    repository_root, _ = validate_repository_layout()
    relative_parent = report_path.parent.relative_to(repository_root)
    parent_fd = open_directory_chain(
        repository_root,
        relative_parent,
        create=True,
    )
    return ReportTarget(path=report_path, parent_fd=parent_fd)


def assert_report_parent_identity(target: ReportTarget) -> None:
    repository_root, _ = validate_repository_layout()
    observed_fd = open_directory_chain(
        repository_root,
        target.path.parent.relative_to(repository_root),
        create=False,
    )
    try:
        held = os.fstat(target.parent_fd)
        observed = os.fstat(observed_fd)
        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
            raise RuntimeError("Comparison report directory changed during refresh")
    finally:
        os.close(observed_fd)


def atomic_rename_at_no_replace(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically rename one descriptor-relative entry only if target is absent."""

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename_exclusive = libc.renameatx_np
        except AttributeError as error:
            raise RuntimeError(
                "This macOS runtime lacks atomic no-replace rename support"
            ) from error
        flag = 0x00000004  # RENAME_EXCL from <sys/stdio.h>
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = libc.renameat2
        except AttributeError as error:
            raise RuntimeError(
                "This Linux runtime lacks atomic no-replace rename support"
            ) from error
        flag = 1  # RENAME_NOREPLACE from <linux/fs.h>
    else:
        raise RuntimeError(
            "Atomic comparison-report publication is supported only on macOS and Linux"
        )
    rename_exclusive.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exclusive.restype = ctypes.c_int
    result = rename_exclusive(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )


def remove_stale_report(target: ReportTarget) -> None:
    """Ensure a failed refresh cannot leave a prior report at the target path."""

    try:
        assert_report_parent_identity(target)
        try:
            os.unlink(target.name, dir_fd=target.parent_fd)
            os.fsync(target.parent_fd)
        except FileNotFoundError:
            pass
    except OSError as error:
        raise RuntimeError(
            f"Could not remove stale comparison report {target.path}: {error}"
        ) from error


def temporary_file_state(file_descriptor: int) -> TemporaryFileState:
    metadata = os.fstat(file_descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("Temporary comparison report is not an owned regular file")
    position = os.lseek(file_descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    try:
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.lseek(file_descriptor, position, os.SEEK_SET)
    return TemporaryFileState(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        link_count=metadata.st_nlink,
        size=metadata.st_size,
        sha256=digest.hexdigest(),
    )


def verify_owned_temporary_entry(
    target: ReportTarget,
    entry_name: str,
    owner_descriptor: int,
    expected: TemporaryFileState,
) -> None:
    owner_state = temporary_file_state(owner_descriptor)
    try:
        observed_descriptor = os.open(
            entry_name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=target.parent_fd,
        )
    except OSError as error:
        raise RuntimeError(
            f"Temporary comparison report changed; preserving {entry_name}"
        ) from error
    try:
        observed_state = temporary_file_state(observed_descriptor)
    finally:
        os.close(observed_descriptor)
    if owner_state != expected or observed_state != expected:
        raise RuntimeError(
            f"Temporary comparison report changed; preserving {entry_name}"
        )


def write_report_atomically(target: ReportTarget, report: dict) -> None:
    """Expose the candidate report only after its complete YAML is on disk."""

    temporary_name: str | None = None
    file_descriptor: int | None = None
    expected_state: TemporaryFileState | None = None
    try:
        assert_report_parent_identity(target)
        for _ in range(128):
            candidate = f".{target.name}.{secrets.token_hex(12)}.tmp"
            try:
                file_descriptor = os.open(
                    candidate,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=target.parent_fd,
                )
                temporary_name = candidate
                expected_state = temporary_file_state(file_descriptor)
                break
            except FileExistsError:
                continue
        if file_descriptor is None or temporary_name is None:
            raise OSError("could not allocate a unique temporary report")

        payload = yaml.safe_dump(
            report,
            sort_keys=False,
            allow_unicode=False,
            width=100,
        )
        with os.fdopen(os.dup(file_descriptor), "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        expected_state = temporary_file_state(file_descriptor)
        assert_report_parent_identity(target)
        verify_owned_temporary_entry(
            target,
            temporary_name,
            file_descriptor,
            expected_state,
        )
        atomic_rename_at_no_replace(
            target.parent_fd,
            temporary_name,
            target.parent_fd,
            target.name,
        )
        verify_owned_temporary_entry(
            target,
            target.name,
            file_descriptor,
            expected_state,
        )
        temporary_name = None
        os.fsync(target.parent_fd)
    except Exception as error:
        raise RuntimeError(
            f"Could not write comparison report {target.path}: {error}"
        ) from error
    finally:
        if file_descriptor is not None:
            try:
                if temporary_name is not None and expected_state is not None:
                    try:
                        verify_owned_temporary_entry(
                            target,
                            temporary_name,
                            file_descriptor,
                            expected_state,
                        )
                    except RuntimeError:
                        try:
                            os.stat(
                                temporary_name,
                                dir_fd=target.parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            pass
                        else:
                            raise
                    else:
                        os.unlink(temporary_name, dir_fd=target.parent_fd)
            finally:
                os.close(file_descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--upstream-ref",
        required=True,
        type=exact_commit,
        help="Exact 40-character upstream commit to fetch and compare.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require the exact commit from the existing raw checkout without fetching.",
    )
    parser.add_argument("--report", type=Path, default=CANDIDATE_REPORT)
    args = parser.parse_args(argv)
    target: ReportTarget | None = None
    try:
        report_path = validate_report_path(args.report)
        target = open_report_target(report_path)
        remove_stale_report(target)
        refresh_upstream_repo(args.upstream_ref, offline=args.offline)
        inventory = build_inventory()
        if inventory["commit"] != args.upstream_ref:
            raise RuntimeError("Candidate inventory does not match the requested commit")
        report = build_comparison_report(inventory, args.upstream_ref)
        write_report_atomically(target, report)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        return 1
    finally:
        if target is not None:
            target.close()
    print(f"Wrote ignored upstream comparison {report_path}")
    print("No curated content, lock, or manifest files were changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
