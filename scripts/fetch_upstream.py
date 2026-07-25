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
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LOCAL_GIT_TIMEOUT_SECONDS = 30
NETWORK_GIT_TIMEOUT_SECONDS = 120
POST_KILL_REAP_TIMEOUT_SECONDS = 5
UPSTREAM_CHECKOUT_RELATIVE = Path("upstream") / "stata-skill"
CHECKOUT_OWNER_MARKER = "stata-codex-skills-owner"
CHECKOUT_OWNER_CONTENT = "stata-codex-skills upstream checkout v1\n"
CHECKOUT_OWNER_BYTES = CHECKOUT_OWNER_CONTENT.encode("utf-8")
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


@dataclass(frozen=True)
class PreservedReport:
    path: Path
    state: TemporaryFileState


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


def assert_safe_git_directory_tree(
    parent_fd: int,
    directory_name: str,
    *,
    expected_device: int,
    display_path: str | None = None,
) -> None:
    """Reject redirecting or special entries below a Git write namespace."""

    display_path = display_path or directory_name
    try:
        named_metadata = os.stat(
            directory_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError(
            f"Could not inspect raw upstream Git {display_path}"
        ) from error
    if (
        not stat.S_ISDIR(named_metadata.st_mode)
        or named_metadata.st_dev != expected_device
    ):
        raise RuntimeError(
            f"Raw upstream Git {display_path} must be a local, real directory"
        )
    try:
        directory_fd = os.open(
            directory_name,
            DIRECTORY_OPEN_FLAGS,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise RuntimeError(
            f"Raw upstream Git {display_path} must be a local, real directory"
        ) from error
    try:
        opened_metadata = os.fstat(directory_fd)
        if (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
        ) != (
            named_metadata.st_dev,
            named_metadata.st_ino,
        ):
            raise RuntimeError(
                f"Raw upstream Git {display_path} changed during safety inspection"
            )
        try:
            entry_names = sorted(os.listdir(directory_fd))
        except OSError as error:
            raise RuntimeError(
                f"Could not inspect raw upstream Git {display_path}"
            ) from error
        for entry_name in entry_names:
            relative_name = f"{display_path}/{entry_name}"
            try:
                entry_metadata = os.stat(
                    entry_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise RuntimeError(
                    f"Raw upstream Git {relative_name} changed during safety inspection"
                ) from error
            if stat.S_ISREG(entry_metadata.st_mode):
                if (
                    entry_metadata.st_dev != expected_device
                    or entry_metadata.st_nlink != 1
                ):
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} must be a same-device, "
                        "single-link regular file"
                    )
                try:
                    entry_fd = os.open(
                        entry_name,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_NONBLOCK", 0),
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} changed during "
                        "safety inspection"
                    ) from error
                try:
                    opened_entry = os.fstat(entry_fd)
                finally:
                    os.close(entry_fd)
                if (
                    opened_entry.st_dev,
                    opened_entry.st_ino,
                    stat.S_IFMT(opened_entry.st_mode),
                    opened_entry.st_nlink,
                ) != (
                    entry_metadata.st_dev,
                    entry_metadata.st_ino,
                    stat.S_IFMT(entry_metadata.st_mode),
                    entry_metadata.st_nlink,
                ):
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} changed during "
                        "safety inspection"
                    )
                try:
                    final_entry = os.stat(
                        entry_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} changed during "
                        "safety inspection"
                    ) from error
                if (
                    final_entry.st_dev,
                    final_entry.st_ino,
                    stat.S_IFMT(final_entry.st_mode),
                    final_entry.st_nlink,
                ) != (
                    entry_metadata.st_dev,
                    entry_metadata.st_ino,
                    stat.S_IFMT(entry_metadata.st_mode),
                    entry_metadata.st_nlink,
                ):
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} changed during "
                        "safety inspection"
                    )
                continue
            if stat.S_ISDIR(entry_metadata.st_mode):
                assert_safe_git_directory_tree(
                    directory_fd,
                    entry_name,
                    expected_device=expected_device,
                    display_path=relative_name,
                )
                try:
                    final_entry = os.stat(
                        entry_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} changed during safety inspection"
                    ) from error
                if (
                    final_entry.st_dev,
                    final_entry.st_ino,
                    stat.S_IFMT(final_entry.st_mode),
                ) != (
                    entry_metadata.st_dev,
                    entry_metadata.st_ino,
                    stat.S_IFMT(entry_metadata.st_mode),
                ):
                    raise RuntimeError(
                        f"Raw upstream Git {relative_name} changed during safety inspection"
                    )
                continue
            raise RuntimeError(
                f"Raw upstream Git {relative_name} must be a regular file "
                "or a local, real directory"
            )
        final_opened = os.fstat(directory_fd)
        if (
            final_opened.st_dev,
            final_opened.st_ino,
        ) != (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
        ):
            raise RuntimeError(
                f"Raw upstream Git {display_path} changed during safety inspection"
            )
    finally:
        os.close(directory_fd)
    try:
        final_named = os.stat(
            directory_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise RuntimeError(
            f"Raw upstream Git {display_path} changed during safety inspection"
        ) from error
    if (
        final_named.st_dev,
        final_named.st_ino,
        stat.S_IFMT(final_named.st_mode),
    ) != (
        named_metadata.st_dev,
        named_metadata.st_ino,
        stat.S_IFMT(named_metadata.st_mode),
    ):
        raise RuntimeError(
            f"Raw upstream Git {display_path} changed during safety inspection"
        )


def assert_dedicated_git_layout(context: GitMetadataContext) -> None:
    for prohibited_name, error_message in (
        (
            "commondir",
            "Raw upstream checkout must not redirect Git common metadata",
        ),
        (
            "config.worktree",
            "Raw upstream checkout must not use worktree-specific Git config",
        ),
    ):
        try:
            os.stat(
                prohibited_name,
                dir_fd=context.git_dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        raise RuntimeError(error_message)

    checkout_device = os.fstat(context.checkout_fd).st_dev
    git_device = os.fstat(context.git_dir_fd).st_dev
    if git_device != checkout_device:
        raise RuntimeError(
            "Raw upstream .git metadata must be on the same device as the "
            "dedicated checkout"
        )
    # This full check runs immediately before every Git launch. POSIX cannot
    # prevent a deliberate same-UID replacement after the final boundary.
    assert_safe_git_directory_tree(
        context.checkout_fd,
        ".git",
        expected_device=checkout_device,
        display_path=".git",
    )

    objects_fd: int | None = None
    try:
        try:
            objects_fd = os.open(
                "objects",
                DIRECTORY_OPEN_FLAGS,
                dir_fd=context.git_dir_fd,
            )
        except FileNotFoundError:
            objects_fd = None
        except OSError as error:
            raise RuntimeError(
                "Raw upstream Git objects must be a real directory"
            ) from error
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
        if objects_fd is not None:
            os.close(objects_fd)

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
        r"^\s*(?:fsmonitor|hookspath|sshcommand|uploadpack|worktree(?:config)?)\s*=",
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
    try:
        payload = read_bounded_regular_file(
            context.git_dir_fd,
            CHECKOUT_OWNER_MARKER,
            maximum_size=len(CHECKOUT_OWNER_BYTES),
            label="Raw upstream checkout owner marker",
        )
        if payload is None:
            if allow_missing:
                return False
            raise RuntimeError(
                "Existing raw upstream checkout is not owned by this repository"
            )
        if payload != CHECKOUT_OWNER_BYTES:
            raise RuntimeError("Raw upstream checkout owner marker is invalid")
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(
            "Could not verify ownership of the raw upstream checkout"
        ) from error
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
        if variable.startswith("GIT_") or variable in {
            "SSH_ASKPASS",
            "SSH_ASKPASS_REQUIRE",
        }:
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


def merge_process_output(
    first: str | bytes | None,
    second: str | bytes | None,
    *,
    binary: bool,
) -> str | bytes:
    empty: str | bytes = b"" if binary else ""

    def normalize(value: str | bytes | None) -> str | bytes:
        if value is None:
            return empty
        if binary:
            return value if isinstance(value, bytes) else value.encode("utf-8")
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

    first_value = normalize(first)
    second_value = normalize(second)
    if not first_value:
        return second_value
    if not second_value:
        return first_value
    if second_value.startswith(first_value):
        return second_value
    if first_value.startswith(second_value):
        return first_value
    separator: str | bytes = b"\n" if binary else "\n"
    return first_value + separator + second_value


def bounded_communicate_after_kill(
    process: subprocess.Popen,
    initial_timeout: subprocess.TimeoutExpired | None,
    *,
    binary: bool,
) -> tuple[str | bytes, str | bytes, str | bytes]:
    """Collect diagnostics without waiting indefinitely on inherited pipes."""

    initial_stdout = initial_timeout.output if initial_timeout is not None else None
    initial_stderr = initial_timeout.stderr if initial_timeout is not None else None
    note: str | bytes = b"" if binary else ""
    try:
        stdout, stderr = process.communicate(
            timeout=POST_KILL_REAP_TIMEOUT_SECONDS
        )
        return (
            merge_process_output(initial_stdout, stdout, binary=binary),
            merge_process_output(initial_stderr, stderr, binary=binary),
            note,
        )
    except subprocess.TimeoutExpired as error:
        terminate_process_group(process)
        stdout = merge_process_output(
            initial_stdout,
            error.output,
            binary=binary,
        )
        stderr = merge_process_output(
            initial_stderr,
            error.stderr,
            binary=binary,
        )
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        message = (
            "Post-kill pipe closure timed out; closed local pipes and used a "
            "bounded process reap."
        )
        note = message.encode("utf-8") if binary else message
        try:
            process.wait(timeout=POST_KILL_REAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            terminate_process_group(process)
            suffix = (
                " Could not confirm process reap within the bounded cleanup window."
            )
            note = (
                note + suffix.encode("utf-8")
                if binary
                else note + suffix
            )
        return stdout, stderr, note
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        try:
            process.wait(timeout=POST_KILL_REAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            terminate_process_group(process)
        message = f"Post-kill diagnostic collection failed: {error}"
        note = message.encode("utf-8") if binary else message
        return (
            merge_process_output(initial_stdout, None, binary=binary),
            merge_process_output(initial_stderr, None, binary=binary),
            note,
        )


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
    except subprocess.TimeoutExpired as timeout_error:
        terminate_process_group(process)
        stdout, stderr, cleanup_note = bounded_communicate_after_kill(
            process,
            timeout_error,
            binary=False,
        )
        timeout_message = f"Command timed out after {timeout_seconds} seconds."
        result = subprocess.CompletedProcess(
            command,
            124,
            stdout,
            "\n".join(
                part
                for part in (
                    str(stderr).strip(),
                    timeout_message,
                    str(cleanup_note).strip(),
                )
                if part
            ),
        )
    except BaseException:
        terminate_process_group(process)
        try:
            bounded_communicate_after_kill(
                process,
                None,
                binary=False,
            )
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
    except subprocess.TimeoutExpired as timeout_error:
        terminate_process_group(process)
        stdout, stderr, cleanup_note = bounded_communicate_after_kill(
            process,
            timeout_error,
            binary=True,
        )
        timeout_message = (
            f"Command timed out after {timeout_seconds} seconds.".encode("utf-8")
        )
        result = subprocess.CompletedProcess(
            command,
            124,
            stdout,
            b"\n".join(
                part
                for part in (stderr, timeout_message, cleanup_note)
                if part
            ),
        )
    except BaseException:
        terminate_process_group(process)
        try:
            bounded_communicate_after_kill(
                process,
                None,
                binary=True,
            )
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
                "--no-overwrite-ignore",
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


def validate_reviewed_upstream_lock(reviewed_lock: object) -> dict:
    if not isinstance(reviewed_lock, dict):
        raise RuntimeError("Reviewed upstream lock must be a mapping")
    if reviewed_lock.get("schema_version") != 1:
        raise RuntimeError("Reviewed upstream lock schema_version must be 1")
    reviewed_repository = reviewed_lock.get("repository")
    if not isinstance(reviewed_repository, dict):
        raise RuntimeError("Reviewed upstream lock repository must be a mapping")
    if reviewed_repository.get("url") != UPSTREAM_REPO_URL:
        raise RuntimeError(
            "Reviewed upstream lock repository.url does not exactly match the "
            "configured upstream repository"
        )
    commit = reviewed_repository.get("commit")
    expected_commit = reviewed_repository.get("expected_commit")
    for field, value in (
        ("commit", commit),
        ("expected_commit", expected_commit),
    ):
        if (
            not isinstance(value, str)
            or not COMMIT_PATTERN.fullmatch(value)
            or value != value.lower()
        ):
            raise RuntimeError(
                f"Reviewed upstream lock repository.{field} must be a lowercase "
                "full Git SHA"
            )
    if expected_commit != commit:
        raise RuntimeError(
            "Reviewed upstream lock repository commit drift requires explicit review"
        )
    files = reviewed_lock.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("Reviewed upstream lock files must be a mapping")
    for file_path, metadata in files.items():
        if (
            not isinstance(file_path, str)
            or not file_path
            or Path(file_path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(file_path).parts)
        ):
            raise RuntimeError(
                "Reviewed upstream lock file paths must be safe relative paths"
            )
        if (
            not isinstance(metadata, dict)
            or not SHA256_PATTERN.fullmatch(str(metadata.get("sha256", "")))
        ):
            raise RuntimeError(
                f"Reviewed upstream lock file {file_path!r} requires a SHA-256 hash"
            )
    return reviewed_lock


def read_reviewed_upstream_lock() -> dict:
    try:
        reviewed_lock = read_yaml(UPSTREAM_LOCK_PATH)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise RuntimeError(
            f"Could not read reviewed upstream lock {UPSTREAM_LOCK_PATH}: {error}"
        ) from error
    return validate_reviewed_upstream_lock(reviewed_lock)


def build_comparison_report(
    inventory: dict,
    upstream_ref: str,
    *,
    reviewed_lock: dict | None = None,
) -> dict:
    if inventory.get("repository") != UPSTREAM_REPO_URL:
        raise RuntimeError(
            "Candidate inventory repository does not exactly match the configured "
            "upstream repository"
        )
    if reviewed_lock is None:
        reviewed_lock = read_reviewed_upstream_lock()
    else:
        reviewed_lock = validate_reviewed_upstream_lock(reviewed_lock)
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


def remove_stale_report(target: ReportTarget) -> PreservedReport | None:
    """Remove only a report whose ownership survives an atomic quarantine move."""

    report_descriptor: int | None = None
    quarantine_name: str | None = None
    expected: TemporaryFileState | None = None
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
            return None
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
                raise RuntimeError(
                    "Existing comparison report changed during quarantine; "
                    f"{describe_expected_report_location(target, expected)}"
                ) from verification_error
            except FileExistsError as restore_error:
                raise RuntimeError(
                    "Quarantined foreign comparison report could not be restored; "
                    f"{describe_held_report_entry(target, quarantine_name, 'the quarantine entry')}; "
                    f"{describe_expected_report_location(target, expected)}"
                ) from restore_error
            raise RuntimeError(
                "Existing comparison report changed during quarantine; "
                f"{describe_held_report_entry(target, target.name, 'the restored quarantine entry')}; "
                f"{describe_expected_report_location(target, expected)}"
            ) from verification_error

        # There is no portable descriptor-relative unlink-by-inode operation.
        # Keep the verified, uniquely named owned quarantine rather than
        # reintroducing a check-then-unlink substitution window.
        os.fsync(target.parent_fd)
        return PreservedReport(
            path=target.path.parent / quarantine_name,
            state=expected,
        )
    except OSError as error:
        recovery = ""
        if quarantine_name is not None and expected is not None:
            recovery = (
                "; "
                + describe_preserved_report(
                    target,
                    PreservedReport(
                        path=target.path.parent / quarantine_name,
                        state=expected,
                    ),
                )
            )
        raise RuntimeError(
            f"Could not remove stale comparison report {target.path}: "
            f"{error}{recovery}"
        ) from error
    finally:
        if report_descriptor is not None:
            os.close(report_descriptor)


def report_parent_is_current(target: ReportTarget) -> bool:
    try:
        assert_report_parent_identity(target)
    except (OSError, RuntimeError):
        return False
    return True


def describe_report_entry_location(
    target: ReportTarget,
    entry_name: str,
    *,
    state: TemporaryFileState | None = None,
    descriptor: int | None = None,
    entry_identity: tuple[int, int, int] | None = None,
) -> str:
    """Name an entry exactly only while its parent and inode retain public names."""

    device: int | None = None
    inode: int | None = None
    file_type: int | None = None
    if state is not None:
        device = state.device
        inode = state.inode
        file_type = stat.S_IFMT(state.mode)
    elif entry_identity is not None:
        device, inode, file_type = entry_identity
    elif descriptor is not None:
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            pass
        else:
            device = metadata.st_dev
            inode = metadata.st_ino
            file_type = stat.S_IFMT(metadata.st_mode)
    parent_is_current = report_parent_is_current(target)
    if (
        parent_is_current
        and device is not None
        and inode is not None
        and file_type is not None
    ):
        try:
            named_metadata = os.stat(
                entry_name,
                dir_fd=target.parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            pass
        else:
            if (
                named_metadata.st_dev,
                named_metadata.st_ino,
                stat.S_IFMT(named_metadata.st_mode),
            ) == (device, inode, file_type) and report_parent_is_current(target):
                try:
                    final_named = os.stat(
                        entry_name,
                        dir_fd=target.parent_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    pass
                else:
                    if (
                        (
                            final_named.st_dev,
                            final_named.st_ino,
                            stat.S_IFMT(final_named.st_mode),
                        )
                        == (device, inode, file_type)
                        and report_parent_is_current(target)
                    ):
                        return f"at {target.path.parent / entry_name}"
    if device is None or inode is None:
        identity_text = "identity unavailable"
    else:
        identity_text = f"device={device}, inode={inode}"
    if parent_is_current:
        return (
            f"under basename {entry_name!r} ({identity_text}) in the "
            "descriptor-held report directory; the directory retains its public "
            "identity, but this held entry has no verified current pathname "
            "(current pathname unknown)"
        )
    return (
        f"under basename {entry_name!r} ({identity_text}) in the descriptor-held "
        "displaced report directory; it has no verified current pathname "
        "(current pathname unknown)"
    )


def describe_held_report_entry(
    target: ReportTarget,
    entry_name: str,
    label: str,
) -> str:
    try:
        metadata = os.stat(
            entry_name,
            dir_fd=target.parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        return (
            f"{label} has no verified surviving entry "
            + describe_report_entry_location(target, entry_name)
        )
    if stat.S_ISREG(metadata.st_mode):
        state = "an unverified regular-file state"
    elif stat.S_ISDIR(metadata.st_mode):
        state = "an unverified directory state"
    elif stat.S_ISLNK(metadata.st_mode):
        state = "an unverified symlink state"
    else:
        state = "an unverified special-entry state"
    return (
        f"{label} survives "
        + describe_report_entry_location(
            target,
            entry_name,
            entry_identity=(
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
            ),
        )
        + f" with {state}"
    )


def describe_preserved_report(
    target: ReportTarget,
    preserved: PreservedReport,
) -> str:
    """Describe the exact last-known quarantine state without following links."""

    if preserved.path.parent != target.path.parent:
        return (
            "the recorded comparison-report quarantine basename "
            f"{preserved.path.name!r} is outside the dedicated report directory; "
            f"current pathname unknown (device={preserved.state.device}, "
            f"inode={preserved.state.inode})"
        )
    try:
        descriptor = os.open(
            preserved.path.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=target.parent_fd,
        )
    except FileNotFoundError:
        return (
            "the previous comparison report no longer has its verified quarantine "
            + describe_report_entry_location(
                target,
                preserved.path.name,
                state=preserved.state,
            )
            + "; a concurrent same-UID actor may have moved it"
        )
    except OSError:
        return (
            "the comparison-report quarantine "
            + describe_report_entry_location(
                target,
                preserved.path.name,
                state=preserved.state,
            )
            + " now has an unsupported state; a concurrent same-UID actor may "
            "have replaced it"
        )
    try:
        observed = temporary_file_state(
            descriptor,
            maximum_size=MAX_REPORT_BYTES,
        )
    except (OSError, RuntimeError):
        return (
            "the comparison-report quarantine "
            + describe_report_entry_location(
                target,
                preserved.path.name,
                state=preserved.state,
            )
            + " now has a different state; a concurrent same-UID actor may "
            "have replaced it"
        )
    finally:
        os.close(descriptor)
    if observed == preserved.state:
        return (
            "the previous comparison report survives unchanged "
            + describe_report_entry_location(
                target,
                preserved.path.name,
                state=preserved.state,
            )
        )
    return (
        "the comparison-report quarantine "
        + describe_report_entry_location(
            target,
            preserved.path.name,
            state=preserved.state,
        )
        + " now has a different state; a concurrent same-UID actor may have "
        "replaced it"
    )


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


def find_report_state_entry_name(
    target: ReportTarget,
    expected: TemporaryFileState,
) -> str | None:
    """Find the held-directory entry still naming an expected report."""

    try:
        entry_names = sorted(os.listdir(target.parent_fd))
    except OSError:
        return None
    for entry_name in entry_names:
        try:
            descriptor = os.open(
                entry_name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=target.parent_fd,
            )
        except OSError:
            continue
        try:
            try:
                observed = temporary_file_state(
                    descriptor,
                    maximum_size=MAX_REPORT_BYTES,
                )
            except (OSError, RuntimeError):
                continue
        finally:
            os.close(descriptor)
        if observed == expected:
            return entry_name
    return None


def find_report_state_path(
    target: ReportTarget,
    expected: TemporaryFileState,
) -> Path | None:
    if not report_parent_is_current(target):
        return None
    entry_name = find_report_state_entry_name(target, expected)
    if entry_name is None:
        return None
    return target.path.parent / entry_name


def describe_expected_report_location(
    target: ReportTarget,
    expected: TemporaryFileState,
) -> str:
    entry_name = find_report_state_entry_name(target, expected)
    if entry_name is None:
        return (
            "the originally opened report has no verified surviving pathname "
            "after a concurrent same-UID mutation"
        )
    return (
        "the originally opened report survives unchanged "
        + describe_report_entry_location(
            target,
            entry_name,
            state=expected,
        )
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


def describe_candidate_survival(
    target: ReportTarget,
    entry_name: str,
    owner_descriptor: int,
    expected: TemporaryFileState | None,
) -> str:
    if expected is None:
        return (
            "candidate state could not be fully verified "
            + describe_report_entry_location(
                target,
                entry_name,
                descriptor=owner_descriptor,
            )
            + "; the entry was not removed"
        )
    try:
        owner_state = temporary_file_state(
            owner_descriptor,
            maximum_size=expected.size,
        )
    except (OSError, RuntimeError):
        return (
            "the open generated candidate changed and has no verified surviving "
            "path "
            + describe_report_entry_location(
                target,
                entry_name,
                state=expected,
            )
        )
    try:
        observed_descriptor = os.open(
            entry_name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=target.parent_fd,
        )
    except FileNotFoundError:
        return (
            "the generated candidate has no verified surviving entry "
            + describe_report_entry_location(
                target,
                entry_name,
                state=expected,
            )
            + " after a concurrent same-UID mutation"
        )
    except OSError:
        return (
            "the generated candidate "
            + describe_report_entry_location(
                target,
                entry_name,
                state=expected,
            )
            + " now has an unsupported state and no verified surviving path "
            "after a concurrent same-UID mutation"
        )
    try:
        observed_state = temporary_file_state(
            observed_descriptor,
            maximum_size=expected.size,
        )
    except (OSError, RuntimeError):
        return (
            "the generated candidate "
            + describe_report_entry_location(
                target,
                entry_name,
                state=expected,
            )
            + " now has a different state and no verified surviving path after "
            "a concurrent same-UID mutation"
        )
    finally:
        os.close(observed_descriptor)
    if owner_state == expected and observed_state == expected:
        return (
            "generated candidate survives unchanged "
            + describe_report_entry_location(
                target,
                entry_name,
                state=expected,
            )
        )
    return (
        "the generated candidate "
        + describe_report_entry_location(
            target,
            entry_name,
            state=expected,
        )
        + " now has a different state and no verified surviving path after a "
        "concurrent same-UID mutation"
    )


def write_report_atomically(target: ReportTarget, report: dict) -> None:
    """Expose the candidate report only after its complete YAML is on disk."""

    temporary_name: str | None = None
    surviving_name: str | None = None
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
                surviving_name = candidate
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
        temporary_name = None
        surviving_name = target.name
        verify_owned_temporary_entry(
            target,
            target.name,
            file_descriptor,
            expected_state,
        )
        os.fsync(target.parent_fd)
        surviving_name = None
    except Exception as error:
        preserved = ""
        if (
            surviving_name is not None
            and file_descriptor is not None
        ):
            preserved = (
                "; "
                + describe_candidate_survival(
                    target,
                    surviving_name,
                    file_descriptor,
                    expected_state,
                )
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
    reviewed_lock: dict | None = None
    preserved_report: PreservedReport | None = None
    try:
        report_path = validate_report_path(args.report)
        reviewed_lock = read_reviewed_upstream_lock()
        target = open_report_target(report_path)
        preserved_report = remove_stale_report(target)
        refresh_upstream_repo(args.upstream_ref, offline=args.offline)
        inventory = build_inventory()
        if inventory["commit"] != args.upstream_ref:
            raise RuntimeError("Candidate inventory does not match the requested commit")
        report = build_comparison_report(
            inventory,
            args.upstream_ref,
            reviewed_lock=reviewed_lock,
        )
        write_report_atomically(target, report)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        if target is not None and preserved_report is not None:
            print(
                "RECOVERY: "
                f"{describe_preserved_report(target, preserved_report)}"
            )
        return 1
    finally:
        if target is not None:
            target.close()
    print(f"Wrote ignored upstream comparison {report_path}")
    print("No curated content, lock, or manifest files were changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
