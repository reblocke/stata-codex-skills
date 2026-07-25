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
import signal
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
REPORT_OWNER = "stata-codex-skills upstream comparison v1"
MAX_REPORT_BYTES = 4 * 1024 * 1024
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


@dataclass
class GitMetadataContext:
    checkout_fd: int
    git_dir_fd: int

    def close(self) -> None:
        os.close(self.git_dir_fd)
        os.close(self.checkout_fd)


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


def open_git_metadata(checkout_fd: int) -> GitMetadataContext:
    try:
        git_dir_fd = os.open(".git", DIRECTORY_OPEN_FLAGS, dir_fd=checkout_fd)
    except OSError as error:
        raise RuntimeError(
            "Raw upstream refresh requires a real, dedicated .git directory; "
            "linked worktrees are not supported"
        ) from error
    return GitMetadataContext(checkout_fd=checkout_fd, git_dir_fd=git_dir_fd)


def read_bounded_regular_file(
    parent_fd: int,
    name: str,
    *,
    maximum_size: int,
    label: str,
) -> bytes | None:
    try:
        file_descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(f"{label} is not a safe regular file") from error
    try:
        metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum_size
        ):
            raise RuntimeError(f"{label} is not a safe regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                file_descriptor,
                min(1024 * 1024, maximum_size + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_size:
                raise RuntimeError(f"{label} is unexpectedly large")
        payload = b"".join(chunks)
        final_metadata = os.fstat(file_descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
        ) != (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_nlink,
            final_metadata.st_size,
        ):
            raise RuntimeError(f"{label} changed while it was read")
        if len(payload) != metadata.st_size:
            raise RuntimeError(f"{label} changed while it was read")
        return payload
    finally:
        os.close(file_descriptor)


def assert_dedicated_git_layout(context: GitMetadataContext) -> None:
    try:
        os.stat(
            "commondir",
            dir_fd=context.git_dir_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError(
            "Raw upstream checkout must not redirect Git common metadata"
        )

    directory_fds: dict[str, int] = {}
    try:
        for name in ("objects", "refs"):
            try:
                directory_fds[name] = os.open(
                    name,
                    DIRECTORY_OPEN_FLAGS,
                    dir_fd=context.git_dir_fd,
                )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise RuntimeError(
                    f"Raw upstream Git {name} must be a real directory"
                ) from error

        objects_fd = directory_fds.get("objects")
        if objects_fd is not None:
            try:
                info_fd = os.open(
                    "info",
                    DIRECTORY_OPEN_FLAGS,
                    dir_fd=objects_fd,
                )
            except FileNotFoundError:
                info_fd = None
            except OSError as error:
                raise RuntimeError(
                    "Raw upstream Git objects/info must be a real directory"
                ) from error
            if info_fd is not None:
                try:
                    try:
                        os.stat(
                            "alternates",
                            dir_fd=info_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise RuntimeError(
                            "Raw upstream checkout must not use alternate object stores"
                        )
                finally:
                    os.close(info_fd)
    finally:
        for directory_fd in directory_fds.values():
            os.close(directory_fd)

    config_payload = read_bounded_regular_file(
        context.git_dir_fd,
        "config",
        maximum_size=1024 * 1024,
        label="Raw upstream Git config",
    )
    if config_payload is None:
        return
    try:
        config_text = config_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("Raw upstream Git config is not UTF-8") from error
    if re.search(
        r"^\s*\[\s*(?:alias|credential|filter|include(?:if)?|protocol|url)\b",
        config_text,
        flags=re.IGNORECASE | re.MULTILINE,
    ) or re.search(
        r"^\s*(?:fsmonitor|hookspath|sshcommand|uploadpack|worktree)\s*=",
        config_text,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        raise RuntimeError("Raw upstream Git config contains unsafe redirection")


def assert_git_metadata_identity(context: GitMetadataContext) -> None:
    assert_checkout_identity(context.checkout_fd)
    try:
        observed_entry_fd = os.open(
            ".git",
            DIRECTORY_OPEN_FLAGS,
            dir_fd=context.checkout_fd,
        )
    except OSError as error:
        raise RuntimeError("Checkout .git metadata changed during refresh") from error
    try:
        held = os.fstat(context.git_dir_fd)
        observed = os.fstat(observed_entry_fd)
        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
            raise RuntimeError("Checkout .git metadata changed during refresh")
    finally:
        os.close(observed_entry_fd)

    try:
        parent_fd = os.open("..", DIRECTORY_OPEN_FLAGS, dir_fd=context.git_dir_fd)
    except OSError as error:
        raise RuntimeError("Git directory moved during refresh") from error
    try:
        held_checkout = os.fstat(context.checkout_fd)
        observed_parent = os.fstat(parent_fd)
        if (
            held_checkout.st_dev,
            held_checkout.st_ino,
        ) != (
            observed_parent.st_dev,
            observed_parent.st_ino,
        ):
            raise RuntimeError("Git directory moved during refresh")
    finally:
        os.close(parent_fd)
    assert_dedicated_git_layout(context)


def write_checkout_owner_marker(context: GitMetadataContext) -> None:
    marker_fd: int | None = None
    try:
        marker_fd = os.open(
            CHECKOUT_OWNER_MARKER,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=context.git_dir_fd,
        )
        with os.fdopen(marker_fd, "w", encoding="utf-8") as handle:
            marker_fd = None
            handle.write(CHECKOUT_OWNER_CONTENT)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(context.git_dir_fd)
    except OSError as error:
        raise RuntimeError(
            "Could not establish ownership of the raw upstream checkout"
        ) from error
    finally:
        if marker_fd is not None:
            os.close(marker_fd)


def verify_checkout_owner_marker(
    context: GitMetadataContext,
    *,
    allow_missing: bool = False,
) -> bool:
    marker_fd: int | None = None
    try:
        marker_fd = os.open(
            CHECKOUT_OWNER_MARKER,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=context.git_dir_fd,
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


def sanitized_git_environment() -> dict[str, str]:
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
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_anchored_git(
    arguments: list[str],
    checkout: int | GitMetadataContext,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run Git with its working directory fixed to a held directory descriptor."""

    if isinstance(checkout, GitMetadataContext):
        assert_git_metadata_identity(checkout)
        checkout_fd = checkout.checkout_fd
        working_directory_fd = checkout.git_dir_fd
        command = [
            arguments[0],
            "-c",
            f"core.hooksPath={os.devnull}",
            "--git-dir=.",
            "--work-tree=..",
            *arguments[1:],
        ]
        inherited_fds = (checkout.checkout_fd, checkout.git_dir_fd)
    else:
        checkout_fd = checkout
        working_directory_fd = checkout_fd
        command = [
            arguments[0],
            "-c",
            f"core.hooksPath={os.devnull}",
            *arguments[1:],
        ]
        inherited_fds = (checkout_fd,)

    def enter_working_directory() -> None:
        os.fchdir(working_directory_fd)
        if isinstance(checkout, GitMetadataContext):
            parent_metadata = os.stat("..", follow_symlinks=False)
            checkout_metadata = os.fstat(checkout_fd)
            if (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            ) != (
                checkout_metadata.st_dev,
                checkout_metadata.st_ino,
            ):
                raise OSError("Git directory moved before command execution")

    environment = sanitized_git_environment()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=inherited_fds,
            preexec_fn=enter_working_directory,
            env=environment,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"Could not start descriptor-bound Git command: {error}"
        ) from error
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        result = subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        stdout, stderr = process.communicate()
        timeout_message = f"Command timed out after {timeout_seconds} seconds."
        result = subprocess.CompletedProcess(
            command,
            124,
            stdout,
            f"{stderr}\n{timeout_message}".strip(),
        )
    except BaseException:
        terminate_process_group(process)
        try:
            process.communicate()
        except BaseException:
            pass
        raise
    if isinstance(checkout, GitMetadataContext):
        assert_git_metadata_identity(checkout)
    return result


def run_anchored_git_bytes(
    arguments: list[str],
    checkout: int | GitMetadataContext,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    """Run anchored Git while preserving exact binary stdout."""

    if isinstance(checkout, GitMetadataContext):
        assert_git_metadata_identity(checkout)
        checkout_fd = checkout.checkout_fd
        working_directory_fd = checkout.git_dir_fd
        command = [
            arguments[0],
            "-c",
            f"core.hooksPath={os.devnull}",
            "--git-dir=.",
            "--work-tree=..",
            *arguments[1:],
        ]
        inherited_fds = (checkout.checkout_fd, checkout.git_dir_fd)
    else:
        checkout_fd = checkout
        working_directory_fd = checkout_fd
        command = [
            arguments[0],
            "-c",
            f"core.hooksPath={os.devnull}",
            *arguments[1:],
        ]
        inherited_fds = (checkout_fd,)

    def enter_working_directory() -> None:
        os.fchdir(working_directory_fd)
        if isinstance(checkout, GitMetadataContext):
            parent_metadata = os.stat("..", follow_symlinks=False)
            checkout_metadata = os.fstat(checkout_fd)
            if (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            ) != (
                checkout_metadata.st_dev,
                checkout_metadata.st_ino,
            ):
                raise OSError("Git directory moved before command execution")

    environment = sanitized_git_environment()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=inherited_fds,
            preexec_fn=enter_working_directory,
            env=environment,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"Could not start descriptor-bound Git command: {error}"
        ) from error
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        result = subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        stdout, stderr = process.communicate()
        timeout_message = (
            f"Command timed out after {timeout_seconds} seconds.".encode("utf-8")
        )
        result = subprocess.CompletedProcess(
            command,
            124,
            stdout,
            b"\n".join(part for part in (stderr, timeout_message) if part),
        )
    except BaseException:
        terminate_process_group(process)
        try:
            process.communicate()
        except BaseException:
            pass
        raise
    if isinstance(checkout, GitMetadataContext):
        assert_git_metadata_identity(checkout)
    return result


def checked_checkout_git(
    arguments: list[str],
    checkout: int | GitMetadataContext,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
    action: str,
) -> str:
    result = run_anchored_git(
        arguments,
        checkout,
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
    checkout: int | GitMetadataContext,
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
    action: str,
) -> bytes:
    result = run_anchored_git_bytes(
        arguments,
        checkout,
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


def initialize_upstream_repo(*, allow_create: bool) -> GitMetadataContext:
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

    context: GitMetadataContext | None = None
    try:
        assert_checkout_identity(checkout_fd)
        if created:
            if os.listdir(checkout_fd):
                raise RuntimeError(
                    "New raw upstream checkout directory is unexpectedly nonempty"
                )
            try:
                os.mkdir(".git", mode=0o700, dir_fd=checkout_fd)
            except OSError as error:
                raise RuntimeError(
                    "Could not create dedicated Git metadata directory"
                ) from error
            context = open_git_metadata(checkout_fd)
            checked_checkout_git(
                ["git", "init"],
                context,
                action="Could not initialize raw upstream checkout",
            )
            assert_git_metadata_identity(context)
        else:
            context = open_git_metadata(checkout_fd)
        marker_present = verify_checkout_owner_marker(
            context,
            allow_missing=True,
        )

        inside_work_tree = checked_checkout_git(
            ["git", "rev-parse", "--is-inside-work-tree"],
            context,
            action="Raw upstream checkout is not a Git repository",
        )
        if inside_work_tree != "true":
            raise RuntimeError("Raw upstream checkout is not a Git work tree")
        assert_git_metadata_identity(context)

        remote = run_anchored_git(
            ["git", "remote", "get-url", "origin"],
            context,
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
                context,
                action="Could not configure the upstream remote",
            )
        elif remote.stdout.strip() != UPSTREAM_REPO_URL:
            raise RuntimeError(
                "Raw upstream checkout origin does not match the configured repository"
            )

        if not marker_present:
            require_clean_upstream_repo(context)
            assert_git_metadata_identity(context)
            write_checkout_owner_marker(context)
    except BaseException:
        if context is not None:
            context.close()
        else:
            os.close(checkout_fd)
        raise
    return context


def require_clean_upstream_repo(context: GitMetadataContext) -> None:
    status = checked_checkout_git(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        context,
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

    context = initialize_upstream_repo(allow_create=not offline)
    try:
        require_clean_upstream_repo(context)
        assert_git_metadata_identity(context)
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
                context,
                timeout_seconds=NETWORK_GIT_TIMEOUT_SECONDS,
                action=f"Could not fetch requested upstream commit {upstream_ref}",
            )

        assert_git_metadata_identity(context)
        resolved = checked_checkout_git(
            [
                "git",
                "rev-parse",
                "--verify",
                f"{upstream_ref}^{{commit}}",
            ],
            context,
            action=f"Requested upstream commit is unavailable: {upstream_ref}",
        ).lower()
        if resolved != upstream_ref:
            raise RuntimeError(
                f"Requested upstream commit resolved unexpectedly: {resolved}"
            )

        require_clean_upstream_repo(context)
        assert_git_metadata_identity(context)
        checked_checkout_git(
            [
                "git",
                "checkout",
                "--detach",
                upstream_ref,
            ],
            context,
            action=f"Could not check out requested upstream commit {upstream_ref}",
        )
        assert_git_metadata_identity(context)
        if upstream_commit(context) != upstream_ref:
            raise RuntimeError(
                "Raw upstream checkout did not land on the requested commit"
            )

        symbolic_head = run_anchored_git(
            ["git", "symbolic-ref", "-q", "HEAD"],
            context,
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
        context.close()


def upstream_commit(context: GitMetadataContext) -> str:
    return checked_checkout_git(
        ["git", "rev-parse", "HEAD"],
        context,
        action="Could not read upstream commit",
    ).lower()


def tracked_markdown_inventory(
    relative_root: Path,
    context: GitMetadataContext,
) -> list[dict[str, str]]:
    output = checked_checkout_git_bytes(
        [
            "git",
            "ls-tree",
            "--full-tree",
            "-r",
            "-z",
            "HEAD",
            "--",
            relative_root.as_posix(),
        ],
        context,
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
                context,
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
    context = initialize_upstream_repo(allow_create=False)
    try:
        inventory: dict[str, list[dict]] = {}
        for skill_key, relative_root in UPSTREAM_ROOTS.items():
            inventory[skill_key] = tracked_markdown_inventory(
                relative_root,
                context,
            )
        result = {
            "repository": UPSTREAM_REPO_URL,
            "commit": upstream_commit(context),
            "inventory": inventory,
        }
        assert_git_metadata_identity(context)
        return result
    finally:
        context.close()


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
        "report_owner": REPORT_OWNER,
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
    expected = raw_root / "candidates" / "upstream-comparison.yaml"
    if report_path != expected:
        raise RuntimeError(
            "Comparison report must use the dedicated "
            "raw/candidates/upstream-comparison.yaml location"
        )
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
    """Remove only a report whose ownership survives an atomic quarantine move."""

    report_descriptor: int | None = None
    quarantine_name: str | None = None
    try:
        assert_report_parent_identity(target)
        try:
            report_descriptor = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=target.parent_fd,
            )
        except FileNotFoundError:
            return
        expected = temporary_file_state(
            report_descriptor,
            maximum_size=MAX_REPORT_BYTES,
        )
        os.lseek(report_descriptor, 0, os.SEEK_SET)
        payload = os.read(report_descriptor, MAX_REPORT_BYTES + 1)
        if len(payload) > MAX_REPORT_BYTES:
            raise RuntimeError("Existing comparison report is too large to be owned")
        try:
            parsed = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise RuntimeError(
                "Existing comparison report is foreign; preserving it"
            ) from error
        if (
            isinstance(parsed, dict)
            and parsed.get("report_type") == "upstream-comparison"
            and "report_owner" not in parsed
        ):
            raise RuntimeError(
                "Legacy comparison report lacks report_owner; review it, then "
                "move or delete raw/candidates/upstream-comparison.yaml explicitly"
            )
        if (
            not isinstance(parsed, dict)
            or parsed.get("schema_version") != 1
            or parsed.get("report_type") != "upstream-comparison"
            or parsed.get("report_owner") != REPORT_OWNER
        ):
            raise RuntimeError(
                "Existing comparison report is foreign; preserving it"
            )

        for _ in range(128):
            candidate = f".{target.name}.{secrets.token_hex(12)}.stale"
            try:
                atomic_rename_at_no_replace(
                    target.parent_fd,
                    target.name,
                    target.parent_fd,
                    candidate,
                )
                quarantine_name = candidate
                break
            except FileExistsError:
                continue
        if quarantine_name is None:
            raise RuntimeError("Could not allocate stale-report quarantine")

        try:
            verify_owned_temporary_entry(
                target,
                quarantine_name,
                report_descriptor,
                expected,
            )
        except RuntimeError as verification_error:
            try:
                atomic_rename_at_no_replace(
                    target.parent_fd,
                    quarantine_name,
                    target.parent_fd,
                    target.name,
                )
            except FileNotFoundError:
                pass
            except FileExistsError as restore_error:
                raise RuntimeError(
                    "Quarantined foreign comparison report could not be restored; "
                    f"preserving it as {quarantine_name}"
                ) from restore_error
            raise RuntimeError(
                "Existing comparison report changed during quarantine; preserving it"
            ) from verification_error

        # There is no portable descriptor-relative unlink-by-inode operation.
        # Keep the verified, uniquely named owned quarantine rather than
        # reintroducing a check-then-unlink substitution window.
        os.fsync(target.parent_fd)
    except OSError as error:
        raise RuntimeError(
            f"Could not remove stale comparison report {target.path}: {error}"
        ) from error
    finally:
        if report_descriptor is not None:
            os.close(report_descriptor)


def temporary_file_state(
    file_descriptor: int,
    *,
    maximum_size: int | None = None,
) -> TemporaryFileState:
    metadata = os.fstat(file_descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("Temporary comparison report is not an owned regular file")
    if maximum_size is not None and metadata.st_size > maximum_size:
        raise RuntimeError("Temporary comparison report exceeds its allowed size")
    position = os.lseek(file_descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        while True:
            read_size = 1024 * 1024
            if maximum_size is not None:
                read_size = min(read_size, maximum_size + 1 - bytes_read)
                if read_size <= 0:
                    raise RuntimeError(
                        "Temporary comparison report exceeds its allowed size"
                    )
            chunk = os.read(file_descriptor, read_size)
            if not chunk:
                break
            bytes_read += len(chunk)
            if maximum_size is not None and bytes_read > maximum_size:
                raise RuntimeError(
                    "Temporary comparison report exceeds its allowed size"
                )
            digest.update(chunk)
    finally:
        os.lseek(file_descriptor, position, os.SEEK_SET)
    final_metadata = os.fstat(file_descriptor)
    if (
        final_metadata.st_dev,
        final_metadata.st_ino,
        final_metadata.st_mode,
        final_metadata.st_nlink,
        final_metadata.st_size,
    ) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
    ):
        raise RuntimeError("Temporary comparison report changed while it was read")
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
    owner_state = temporary_file_state(
        owner_descriptor,
        maximum_size=expected.size,
    )
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
        observed_state = temporary_file_state(
            observed_descriptor,
            maximum_size=expected.size,
        )
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
        preserved = (
            f"; preserved failed candidate as {temporary_name}"
            if temporary_name is not None
            else ""
        )
        raise RuntimeError(
            f"Could not write comparison report {target.path}: {error}{preserved}"
        ) from error
    finally:
        if file_descriptor is not None:
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
