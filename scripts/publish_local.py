#!/usr/bin/env python3
"""Transactionally publish a freshly validated generated skill tree."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import os
from pathlib import Path
import secrets
import stat
import sys

from libskillpack import BUILD_ROOT, REPO_ROOT
from release_state import (
    CANONICAL_DIRECTORY_MODE,
    CANONICAL_FILE_MODE,
    SKILL_FOLDERS,
    VALIDATION_RECEIPT_PATH,
    tree_digest_records,
    validate_complete_skill_tree,
    verify_validation_receipt,
)


RECEIPT_MAX_AGE = timedelta(hours=1)
RECEIPT_FUTURE_TOLERANCE = timedelta(minutes=1)
TRANSACTION_PREFIX = ".stata-codex-skills-publish-"
TRANSACTION_LOCK_NAME = ".stata-codex-skills-publish.lock"
TRANSACTION_LOCK_PAYLOAD = b"stata-codex-skills-publish-lock-v1\n"
CLEANUP_QUARANTINE_PREFIX = ".stata-codex-skills-cleanup-"
CLEANUP_FILE_PREFIX = "file-"
CLEANUP_DIRECTORY_PREFIX = "directory-"


class PublishError(RuntimeError):
    """A publication preflight, transaction, or rollback failed."""

    def __init__(self, message: str, *, preserve_transaction: bool = False) -> None:
        super().__init__(message)
        self.preserve_transaction = preserve_transaction


class DestinationAuthorizationError(PublishError):
    """The approved destination path no longer names the anchored root."""


@dataclass
class ReplacementState:
    destination: Path
    backup: Path
    staged: Path
    original_existed: bool
    original_state: DestinationState
    backup_attempted: bool = False
    backup_created: bool = False
    backup_state_after_move: DestinationState | None = None
    install_attempted: bool = False
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
class SkillVerification:
    destination_state: DestinationState
    skill_sha256: str


def _same_directory_identity(
    left: DestinationState,
    right: DestinationState | None,
) -> bool:
    return (
        right is not None
        and left.kind == "directory"
        and right.kind == "directory"
        and left.device == right.device
        and left.inode == right.inode
    )


@dataclass
class TransactionLock:
    path: Path
    root_device: int
    root_inode: int
    file_descriptor: int | None
    close_errors: list[str] = field(default_factory=list)


@dataclass
class DirectoryHandle:
    name: str
    display_path: Path
    device: int
    inode: int
    file_descriptor: int | None


@dataclass(frozen=True)
class DestinationRootIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class TransactionCleanupPlan:
    stage_device: int
    stage_inode: int
    backup_device: int
    backup_inode: int
    quarantine_device: int
    quarantine_inode: int
    backup_states: tuple[tuple[str, DestinationState], ...]


def default_skills_dir() -> Path:
    """Resolve the destination at call time so ``CODEX_HOME`` is honored."""

    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    return codex_home / "skills"


def _effective_user_id() -> int:
    """Return the effective user that must own publication-controlled paths."""

    if not hasattr(os, "geteuid"):
        raise PublishError(
            "Publication destination ownership checks require a POSIX "
            "effective user ID"
        )
    return os.geteuid()


def _is_group_or_other_writable(metadata: os.stat_result) -> bool:
    return bool(metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _assert_trusted_destination_ancestor(
    metadata: os.stat_result,
    display_path: Path,
) -> bool:
    """Validate an ancestor and report whether sticky-child ownership applies."""

    if not _is_group_or_other_writable(metadata):
        return False
    if not metadata.st_mode & stat.S_ISVTX:
        raise PublishError(
            "Publication destination has an unsafe group/other-writable "
            f"ancestor without the sticky bit: {display_path}"
        )
    return True


def _assert_trusted_sticky_child(
    metadata: os.stat_result,
    display_path: Path,
) -> None:
    if metadata.st_uid != _effective_user_id():
        raise PublishError(
            "Publication destination traverses a sticky writable ancestor, "
            "but its next child is not owned by the current effective user: "
            f"{display_path}"
        )


def _assert_secure_destination_root(
    metadata: os.stat_result,
    display_path: Path,
) -> None:
    expected_owner = _effective_user_id()
    if metadata.st_uid != expected_owner:
        raise PublishError(
            "Publication destination root must be owned by the current "
            f"effective user {expected_owner}: {display_path}"
        )
    if _is_group_or_other_writable(metadata):
        raise PublishError(
            "Publication destination root must not be group/other writable: "
            f"{display_path}"
        )


def _capture_authorized_destination_path(
    destination_root: Path,
    *,
    allow_missing: bool,
) -> DestinationRootIdentity | None:
    """Traverse and authorize a destination without following path symlinks."""

    if not destination_root.is_absolute():
        raise PublishError(
            f"Publication destination must be absolute: {destination_root}"
        )
    anchor = Path(destination_root.anchor)
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(anchor, root_flags)
        root_metadata = os.fstat(root_descriptor)
    except OSError as error:
        raise PublishError(
            f"Could not anchor publication filesystem root {anchor}: {error}"
        ) from error
    current = DirectoryHandle(
        name=str(anchor),
        display_path=anchor,
        device=root_metadata.st_dev,
        inode=root_metadata.st_ino,
        file_descriptor=root_descriptor,
    )
    result: DestinationRootIdentity | None = None
    try:
        current_metadata = root_metadata
        relative_parts = destination_root.relative_to(anchor).parts
        for name in relative_parts:
            parent_descriptor = _require_directory_descriptor(current)
            parent_path = current.display_path
            sticky_child_required = _assert_trusted_destination_ancestor(
                current_metadata,
                parent_path,
            )
            child_path = parent_path / name
            try:
                child = _open_directory_handle_at(
                    parent_descriptor,
                    name,
                    child_path,
                )
            except PublishError as error:
                if not (
                    allow_missing
                    and isinstance(error.__cause__, FileNotFoundError)
                ):
                    raise
                if sticky_child_required:
                    raise PublishError(
                        "Publication destination cannot create an unowned "
                        "next child directly beneath a sticky writable "
                        f"ancestor: {child_path}"
                    ) from error
                break
            try:
                child_descriptor = _require_directory_descriptor(child)
                child_metadata = os.fstat(child_descriptor)
                parent_after = os.fstat(parent_descriptor)
            except (OSError, PublishError) as error:
                close_errors = _close_directory_handle(child)
                close_suffix = (
                    "; child descriptor cleanup also reported: "
                    + "; ".join(close_errors)
                    if close_errors
                    else ""
                )
                raise PublishError(
                    "Could not verify publication destination traversal at "
                    f"{child_path}: {error}{close_suffix}"
                ) from error
            parent_before_identity = (
                current_metadata.st_dev,
                current_metadata.st_ino,
                current_metadata.st_uid,
                current_metadata.st_mode,
            )
            parent_after_identity = (
                parent_after.st_dev,
                parent_after.st_ino,
                parent_after.st_uid,
                parent_after.st_mode,
            )
            if parent_before_identity != parent_after_identity:
                close_errors = _close_directory_handle(child)
                close_suffix = (
                    "; child descriptor cleanup also reported: "
                    + "; ".join(close_errors)
                    if close_errors
                    else ""
                )
                raise PublishError(
                    "Publication destination ancestor changed during "
                    f"authorization: {parent_path}{close_suffix}"
                )
            if sticky_child_required:
                try:
                    _assert_trusted_sticky_child(child_metadata, child_path)
                except PublishError as error:
                    close_errors = _close_directory_handle(child)
                    if close_errors:
                        raise PublishError(
                            f"{error}; child descriptor cleanup also "
                            "reported: " + "; ".join(close_errors)
                        ) from error
                    raise
            close_errors = _close_directory_handle(current)
            if close_errors:
                child_close_errors = _close_directory_handle(child)
                suffix = (
                    "; child descriptor cleanup also reported: "
                    + "; ".join(child_close_errors)
                    if child_close_errors
                    else ""
                )
                raise PublishError(
                    "Could not close an authorized publication path "
                    f"component {parent_path}: "
                    f"{'; '.join(close_errors)}{suffix}"
                )
            current = child
            current_metadata = child_metadata

        else:
            _assert_secure_destination_root(current_metadata, destination_root)
            result = DestinationRootIdentity(
                path=destination_root,
                device=current_metadata.st_dev,
                inode=current_metadata.st_ino,
            )
    except BaseException as error:
        close_errors = _close_directory_handle(current)
        if close_errors:
            raise PublishError(
                f"{error}; authorized destination descriptor cleanup also "
                "reported: " + "; ".join(close_errors),
                preserve_transaction=(
                    isinstance(error, PublishError)
                    and error.preserve_transaction
                ),
            ) from error
        raise
    else:
        close_errors = _close_directory_handle(current)
        if close_errors:
            raise PublishError(
                "Could not close authorized publication destination "
                f"{current.display_path}: {'; '.join(close_errors)}"
            )
        return result


def _capture_destination_root_identity(
    destination_root: Path,
    approved_destination: Path,
) -> DestinationRootIdentity:
    if destination_root != approved_destination:
        raise PublishError(
            "Publication destination root no longer names the approved "
            f"path: expected {approved_destination}; observed {destination_root}"
        )
    observed = _capture_authorized_destination_path(
        destination_root,
        allow_missing=False,
    )
    if observed is None:
        raise PublishError(
            f"Publication destination root does not exist: {destination_root}"
        )
    return observed


def _assert_destination_root(identity: DestinationRootIdentity) -> None:
    try:
        observed = _capture_destination_root_identity(identity.path, identity.path)
    except PublishError as error:
        raise DestinationAuthorizationError(
            "Publication destination root can no longer be authorized: "
            f"{identity.path}: {error}",
            preserve_transaction=True,
        ) from error
    if observed.device != identity.device or observed.inode != identity.inode:
        raise DestinationAuthorizationError(
            "Publication destination root identity changed; refusing to modify "
            f"it: {identity.path}",
            preserve_transaction=True,
        )


def _describe_transaction_recovery(
    transaction_root: DirectoryHandle,
    destination_root: DestinationRootIdentity,
    transaction_lock: TransactionLock,
) -> str:
    """Describe recovery state without claiming an unverified pathname."""

    anchored_identity = (
        f"device={destination_root.device}, inode={destination_root.inode}"
    )
    unverified = (
        f"transaction basename {transaction_root.name!r} under anchored "
        f"destination root identity ({anchored_identity}); "
        "no verified current pathname"
    )
    root_descriptor = transaction_lock.file_descriptor
    if root_descriptor is None:
        return unverified
    try:
        _assert_destination_root(destination_root)
        held_root = os.fstat(root_descriptor)
        observed_transaction = os.stat(
            transaction_root.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(held_root.st_mode)
            or held_root.st_dev != destination_root.device
            or held_root.st_ino != destination_root.inode
            or not stat.S_ISDIR(observed_transaction.st_mode)
            or observed_transaction.st_dev != transaction_root.device
            or observed_transaction.st_ino != transaction_root.inode
        ):
            return unverified
        # Reauthorize after the entry observation so the displayed pathname is
        # backed by a no-follow root identity check at the reporting boundary.
        _assert_destination_root(destination_root)
    except (OSError, PublishError):
        return unverified
    return (
        "verified current recovery pathname "
        f"{destination_root.path / transaction_root.name} "
        f"(anchored destination root {anchored_identity})"
    )


def _write_all(file_descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        chunk_size = os.write(file_descriptor, payload[written:])
        if chunk_size <= 0:
            raise OSError("filesystem write made no progress")
        written += chunk_size


def _ensure_transaction_sentinel(
    root_descriptor: int,
    lock_path: Path,
) -> None:
    create_flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    created = False
    sentinel_descriptor: int | None = None
    try:
        try:
            sentinel_descriptor = os.open(
                TRANSACTION_LOCK_NAME,
                create_flags,
                0o600,
                dir_fd=root_descriptor,
            )
            created = True
        except FileExistsError:
            sentinel_descriptor = os.open(
                TRANSACTION_LOCK_NAME,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )

        sentinel_stat = os.fstat(sentinel_descriptor)
        expected_owner = os.geteuid() if hasattr(os, "geteuid") else sentinel_stat.st_uid
        if (
            not stat.S_ISREG(sentinel_stat.st_mode)
            or sentinel_stat.st_nlink != 1
            or sentinel_stat.st_uid != expected_owner
        ):
            raise PublishError(
                "Publication lock sentinel must be a singly linked regular "
                f"file owned by the current user: {lock_path}"
            )
        if created:
            _write_all(sentinel_descriptor, TRANSACTION_LOCK_PAYLOAD)
            os.fsync(sentinel_descriptor)
            _fsync_directory_descriptor(
                root_descriptor,
                lock_path.parent,
            )
        else:
            observed = os.read(
                sentinel_descriptor,
                len(TRANSACTION_LOCK_PAYLOAD) + 1,
            )
            if observed != TRANSACTION_LOCK_PAYLOAD:
                raise PublishError(
                    "Publication lock sentinel is not recognized; refusing to "
                    f"replace it: {lock_path}"
                )
    except BaseException as error:
        # The sentinel is a permanent protocol marker on successful runs.
        # Preserve it on initialization failure too: unlinking its predictable
        # public name after a separate identity check could delete a concurrent
        # replacement. An incomplete marker fails closed on the next preflight
        # and remains available for explicit review.
        close_errors = (
            _close_file_descriptor(sentinel_descriptor)
            if sentinel_descriptor is not None
            else []
        )
        sentinel_descriptor = None
        if close_errors:
            raise PublishError(
                f"{error}; sentinel descriptor cleanup also reported: "
                + "; ".join(close_errors),
                preserve_transaction=(
                    isinstance(error, PublishError)
                    and error.preserve_transaction
                ),
            ) from error
        raise
    else:
        close_errors = (
            _close_file_descriptor(sentinel_descriptor)
            if sentinel_descriptor is not None
            else []
        )
        sentinel_descriptor = None
        if close_errors:
            raise PublishError(
                "Publication lock sentinel descriptor closure was "
                "indeterminate: " + "; ".join(close_errors)
            )


def _acquire_transaction_lock(
    destination_root: DestinationRootIdentity,
) -> TransactionLock:
    _assert_destination_root(destination_root)
    lock_path = destination_root.path / TRANSACTION_LOCK_NAME
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        file_descriptor = os.open(destination_root.path, root_flags)
    except OSError as error:
        raise PublishError(
            "Could not open publication destination for locking "
            f"{destination_root.path}: {error}"
        ) from error

    try:
        root_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_dev != destination_root.device
            or root_stat.st_ino != destination_root.inode
        ):
            raise PublishError(
                "Publication destination changed before its kernel lock was "
                f"acquired: {destination_root.path}"
            )
        try:
            fcntl.flock(
                file_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise PublishError(
                    "Another publication transaction is active for "
                    f"{destination_root.path}."
                ) from error
            raise
        _assert_destination_root(destination_root)
        _ensure_transaction_sentinel(file_descriptor, lock_path)
    except BaseException as error:
        close_errors = _close_file_descriptor(
            file_descriptor,
            unlock=True,
        )
        if isinstance(error, PublishError):
            if close_errors:
                raise PublishError(
                    f"{error}; lock descriptor cleanup also reported: "
                    + "; ".join(close_errors),
                    preserve_transaction=error.preserve_transaction,
                ) from error
            raise
        close_suffix = (
            f"; lock descriptor cleanup also reported: {'; '.join(close_errors)}"
            if close_errors
            else ""
        )
        raise PublishError(
            "Could not initialize publication transaction lock "
            f"{lock_path}: {error}{close_suffix}"
        ) from error
    return TransactionLock(
        path=lock_path,
        root_device=root_stat.st_dev,
        root_inode=root_stat.st_ino,
        file_descriptor=file_descriptor,
    )


def _assert_transaction_lock(
    lock: TransactionLock,
    destination_root: DestinationRootIdentity,
) -> None:
    _assert_destination_root(destination_root)
    if lock.file_descriptor is None:
        raise DestinationAuthorizationError(
            f"Publication transaction lock is no longer held: {lock.path}",
            preserve_transaction=True,
        )
    try:
        held = os.fstat(lock.file_descriptor)
    except OSError as error:
        raise DestinationAuthorizationError(
            f"Could not verify held publication transaction lock: {lock.path}",
            preserve_transaction=True,
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or held.st_dev != lock.root_device
        or held.st_ino != lock.root_inode
        or held.st_dev != destination_root.device
        or held.st_ino != destination_root.inode
    ):
        raise DestinationAuthorizationError(
            "Held publication destination lock changed: "
            f"{destination_root.path}",
            preserve_transaction=True,
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
    close_errors = _close_transaction_lock(lock)
    if close_errors:
        raise PublishError(
            "Publication kernel-lock release reported errors: "
            + "; ".join(close_errors)
        )


def _close_transaction_lock(lock: TransactionLock) -> list[str]:
    if lock.file_descriptor is None:
        return []
    file_descriptor = lock.file_descriptor
    lock.file_descriptor = None
    close_errors = _close_file_descriptor(
        file_descriptor,
        unlock=True,
    )
    lock.close_errors.extend(close_errors)
    return close_errors


def _close_file_descriptor(
    file_descriptor: int,
    *,
    unlock: bool = False,
) -> list[str]:
    errors: list[str] = []
    if unlock:
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            errors.append(f"unlock failed: {error}")
    try:
        os.close(file_descriptor)
    except BaseException as error:
        errors.append(f"close failed: {error}")
    return errors


def _fsync_directory_descriptor(
    file_descriptor: int,
    display_path: Path,
    *,
    preserve_transaction: bool = False,
) -> None:
    try:
        os.fsync(file_descriptor)
    except OSError as error:
        raise PublishError(
            f"Could not durably synchronize directory {display_path}: {error}",
            preserve_transaction=preserve_transaction,
        ) from error


def _open_directory_handle_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> DirectoryHandle:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor: int | None = None
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        file_descriptor = os.open(
            name,
            flags,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or metadata.st_dev != opened.st_dev
            or metadata.st_ino != opened.st_ino
        ):
            raise PublishError(
                f"Anchored directory changed while opening it: {display_path}"
            )
        handle = DirectoryHandle(
            name=name,
            display_path=display_path,
            device=opened.st_dev,
            inode=opened.st_ino,
            file_descriptor=file_descriptor,
        )
        file_descriptor = None
        return handle
    except BaseException as error:
        close_errors = (
            _close_file_descriptor(file_descriptor)
            if file_descriptor is not None
            else []
        )
        close_suffix = (
            f"; descriptor cleanup also reported: {'; '.join(close_errors)}"
            if close_errors
            else ""
        )
        if isinstance(error, OSError):
            raise PublishError(
                f"Could not open anchored directory {display_path}: "
                f"{error}{close_suffix}"
            ) from error
        if isinstance(error, PublishError):
            if close_errors:
                raise PublishError(
                    f"{error}{close_suffix}",
                    preserve_transaction=error.preserve_transaction,
                ) from error
            raise
        if close_errors:
            try:
                error.add_note(
                    "anchored-directory descriptor cleanup also reported: "
                    + "; ".join(close_errors)
                )
            except BaseException:
                pass
        raise


def _close_directory_handle(handle: DirectoryHandle | None) -> list[str]:
    if handle is None or handle.file_descriptor is None:
        return []
    file_descriptor = handle.file_descriptor
    handle.file_descriptor = None
    return _close_file_descriptor(file_descriptor)


def _require_directory_descriptor(handle: DirectoryHandle) -> int:
    if handle.file_descriptor is None:
        raise PublishError(
            f"Anchored directory is no longer open: {handle.display_path}"
        )
    try:
        observed = os.fstat(handle.file_descriptor)
    except OSError as error:
        raise PublishError(
            f"Could not verify anchored directory {handle.display_path}"
        ) from error
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != handle.device
        or observed.st_ino != handle.inode
    ):
        raise PublishError(
            f"Anchored directory identity changed: {handle.display_path}"
        )
    return handle.file_descriptor


def _create_directory_handle_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    *,
    preserve_on_create_failure: bool = True,
) -> DirectoryHandle:
    try:
        os.mkdir(name, 0o500, dir_fd=parent_descriptor)
    except OSError as error:
        raise PublishError(
            f"Could not create anchored directory {display_path}: {error}",
            preserve_transaction=preserve_on_create_failure,
        ) from error
    handle: DirectoryHandle | None = None
    try:
        created = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        expected_owner = (
            os.geteuid() if hasattr(os, "geteuid") else created.st_uid
        )
        if (
            not stat.S_ISDIR(created.st_mode)
            or created.st_uid != expected_owner
            or stat.S_IMODE(created.st_mode) != 0o500
        ):
            raise PublishError(
                "New transaction directory changed before identity capture: "
                f"{display_path}",
                preserve_transaction=True,
            )
        created_identity = (created.st_dev, created.st_ino)
        handle = _open_directory_handle_at(
            parent_descriptor,
            name,
            display_path,
        )
        descriptor = _require_directory_descriptor(handle)
        opened = os.fstat(descriptor)
        public = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (handle.device, handle.inode) != created_identity
            or (opened.st_dev, opened.st_ino) != created_identity
            or (public.st_dev, public.st_ino) != created_identity
            or stat.S_IMODE(opened.st_mode) != 0o500
            or stat.S_IMODE(public.st_mode) != 0o500
            or os.listdir(descriptor)
        ):
            close_errors = _close_directory_handle(handle)
            close_suffix = (
                "; descriptor cleanup also reported: "
                + "; ".join(close_errors)
                if close_errors
                else ""
            )
            raise PublishError(
                "New transaction directory changed before it could be "
                f"anchored: {display_path}{close_suffix}",
                preserve_transaction=True,
            )
        os.fchmod(descriptor, 0o700)
        opened = os.fstat(descriptor)
        public = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (opened.st_dev, opened.st_ino) != created_identity
            or (public.st_dev, public.st_ino) != created_identity
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(public.st_mode) != 0o700
            or os.listdir(descriptor)
        ):
            close_errors = _close_directory_handle(handle)
            close_suffix = (
                "; descriptor cleanup also reported: "
                + "; ".join(close_errors)
                if close_errors
                else ""
            )
            raise PublishError(
                "New transaction directory changed during initialization; "
                f"preserving it without cleanup: {display_path}{close_suffix}",
                preserve_transaction=True,
            )
        _fsync_directory_descriptor(
            descriptor,
            display_path,
            preserve_transaction=True,
        )
        _fsync_directory_descriptor(
            parent_descriptor,
            display_path.parent,
            preserve_transaction=True,
        )
        opened = os.fstat(descriptor)
        public = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (opened.st_dev, opened.st_ino) != created_identity
            or (public.st_dev, public.st_ino) != created_identity
            or not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(public.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(public.st_mode) != 0o700
            or os.listdir(descriptor)
        ):
            raise PublishError(
                "New transaction directory changed during synchronization; "
                f"preserving it without cleanup: {display_path}",
                preserve_transaction=True,
            )
        return handle
    except BaseException as error:
        close_errors = _close_directory_handle(handle)
        close_suffix = (
            "; descriptor cleanup also reported: "
            + "; ".join(close_errors)
            if close_errors
            else ""
        )
        if not isinstance(error, Exception):
            if close_errors:
                try:
                    error.add_note(
                        "new-directory descriptor cleanup also reported: "
                        + "; ".join(close_errors)
                    )
                except BaseException:
                    pass
            raise
        if isinstance(error, PublishError):
            if error.preserve_transaction:
                if close_errors:
                    raise PublishError(
                        f"{error}{close_suffix}",
                        preserve_transaction=True,
                    ) from error
                raise
            raise PublishError(
                f"{error}{close_suffix}",
                preserve_transaction=True,
            ) from error
        raise PublishError(
            f"Could not anchor newly created transaction directory "
            f"{display_path}: {error}{close_suffix}",
            preserve_transaction=True,
        ) from error


def _create_transaction_workspace(
    destination_root: DestinationRootIdentity,
    transaction_lock: TransactionLock,
) -> DirectoryHandle:
    _assert_mutation_authorized(destination_root, transaction_lock)
    root_descriptor = transaction_lock.file_descriptor
    if root_descriptor is None:
        raise PublishError("Publication destination lock descriptor is missing")
    for _ in range(100):
        name = f"{TRANSACTION_PREFIX}{secrets.token_hex(12)}"
        display_path = destination_root.path / name
        try:
            return _create_directory_handle_at(
                root_descriptor,
                name,
                display_path,
                preserve_on_create_failure=False,
            )
        except PublishError as error:
            if isinstance(error.__cause__, FileExistsError):
                continue
            raise
    raise PublishError(
        "Could not allocate a unique publication transaction directory"
    )


def _filesystem_identity(path: Path) -> tuple[int, int]:
    """Return the filesystem identity of an existing path."""

    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _read_git_control_file(path: Path, label: str) -> str:
    """Read a small Git path-control file without following its final entry."""

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    parent_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        parent_descriptor = os.open(path.parent, directory_flags)
        before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode):
            raise PublishError(f"{label} must be a regular file: {path}")
        if before.st_size > 4096:
            raise PublishError(f"{label} is unexpectedly large: {path}")
        file_descriptor = os.open(
            path.name,
            file_flags,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or before.st_dev != opened.st_dev
            or before.st_ino != opened.st_ino
        ):
            raise PublishError(f"{label} changed while opening it: {path}")
        payload = bytearray()
        while len(payload) <= 4096:
            chunk = os.read(file_descriptor, 4097 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(file_descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(payload) > 4096 or before_identity != after_identity:
            raise PublishError(f"{label} changed while reading it: {path}")
        try:
            value = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            raise PublishError(f"{label} is not valid UTF-8: {path}") from error
    except BaseException as error:
        close_errors: list[str] = []
        if file_descriptor is not None:
            close_errors.extend(_close_file_descriptor(file_descriptor))
            file_descriptor = None
        if parent_descriptor is not None:
            close_errors.extend(_close_file_descriptor(parent_descriptor))
            parent_descriptor = None
        if close_errors:
            raise PublishError(
                f"{error}; Git control descriptor cleanup also reported: "
                + "; ".join(close_errors)
            ) from error
        raise
    else:
        close_errors = []
        if file_descriptor is not None:
            close_errors.extend(_close_file_descriptor(file_descriptor))
        if parent_descriptor is not None:
            close_errors.extend(_close_file_descriptor(parent_descriptor))
        if close_errors:
            raise PublishError(
                f"Could not close {label} descriptors for {path}: "
                + "; ".join(close_errors)
            )
        return value


def _anchor_git_directory(
    candidate: Path,
    label: str,
) -> tuple[Path, tuple[int, int]]:
    """Canonicalize and identity-anchor an existing Git metadata directory."""

    try:
        canonical = candidate.resolve(strict=True)
        metadata = canonical.lstat()
    except (OSError, RuntimeError) as error:
        raise PublishError(f"Could not resolve {label}: {candidate}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PublishError(f"{label} must resolve to a directory: {candidate}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(canonical, flags)
        opened = os.fstat(file_descriptor)
    except OSError as error:
        close_errors = (
            _close_file_descriptor(file_descriptor)
            if file_descriptor is not None
            else []
        )
        close_suffix = (
            "; descriptor cleanup also reported: "
            + "; ".join(close_errors)
            if close_errors
            else ""
        )
        raise PublishError(
            f"Could not anchor {label}: {canonical}{close_suffix}"
        ) from error
    try:
        if (
            not stat.S_ISDIR(opened.st_mode)
            or metadata.st_dev != opened.st_dev
            or metadata.st_ino != opened.st_ino
        ):
            raise PublishError(f"{label} changed while anchoring it: {canonical}")
    except BaseException as error:
        close_errors = _close_file_descriptor(file_descriptor)
        if close_errors:
            raise PublishError(
                f"{error}; Git directory descriptor cleanup also reported: "
                + "; ".join(close_errors)
            ) from error
        raise
    else:
        close_errors = _close_file_descriptor(file_descriptor)
        if close_errors:
            raise PublishError(
                f"Could not close {label} descriptor for {canonical}: "
                + "; ".join(close_errors)
            )
    return canonical, (opened.st_dev, opened.st_ino)


def _git_metadata_roots(
    repository_root: Path,
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    """Resolve the per-worktree and common Git metadata directories."""

    dot_git = repository_root / ".git"
    try:
        dot_git_metadata = dot_git.lstat()
    except OSError as error:
        raise PublishError(
            f"Could not inspect repository Git metadata entry: {dot_git}"
        ) from error
    if stat.S_ISLNK(dot_git_metadata.st_mode):
        raise PublishError(
            f"Repository Git metadata entry must not be a symlink: {dot_git}"
        )
    if stat.S_ISDIR(dot_git_metadata.st_mode):
        git_directory_candidate = dot_git
    elif stat.S_ISREG(dot_git_metadata.st_mode):
        directive = _read_git_control_file(
            dot_git,
            "Repository .git control file",
        ).strip()
        prefix = "gitdir:"
        if not directive.startswith(prefix):
            raise PublishError(
                f"Repository .git control file is malformed: {dot_git}"
            )
        configured = directive[len(prefix) :].strip()
        if not configured or "\n" in configured or "\r" in configured:
            raise PublishError(
                f"Repository .git control file is malformed: {dot_git}"
            )
        git_directory_candidate = Path(configured)
        if not git_directory_candidate.is_absolute():
            git_directory_candidate = dot_git.parent / git_directory_candidate
    else:
        raise PublishError(
            f"Repository Git metadata entry is unsupported: {dot_git}"
        )

    git_directory, git_identity = _anchor_git_directory(
        git_directory_candidate,
        "per-worktree Git directory",
    )
    common_control = git_directory / "commondir"
    try:
        common_control.lstat()
    except FileNotFoundError:
        common_directory = git_directory
        common_identity = git_identity
    except OSError as error:
        raise PublishError(
            f"Could not inspect Git common-directory control: {common_control}"
        ) from error
    else:
        configured = _read_git_control_file(
            common_control,
            "Git common-directory control file",
        ).strip()
        if not configured or "\n" in configured or "\r" in configured:
            raise PublishError(
                f"Git common-directory control file is malformed: {common_control}"
            )
        common_candidate = Path(configured)
        if not common_candidate.is_absolute():
            common_candidate = git_directory / common_candidate
        common_directory, common_identity = _anchor_git_directory(
            common_candidate,
            "common Git directory",
        )

    roots = [(git_directory, git_identity)]
    if common_identity != git_identity:
        roots.append((common_directory, common_identity))
    return tuple(roots)


def _existing_ancestor_identities(
    path: Path,
) -> tuple[bool, tuple[tuple[int, int], ...]]:
    """Return whether ``path`` exists and identities through its existing root.

    Looking up the deepest existing ancestor through the filesystem, instead of
    comparing path spellings, makes containment checks honor case-insensitive
    aliases while retaining case-sensitive behavior on Linux.
    """

    current = path
    path_exists = True
    while True:
        try:
            current.stat()
            break
        except FileNotFoundError:
            path_exists = False
            parent = current.parent
            if parent == current:
                raise
            current = parent
        except NotADirectoryError as error:
            raise PublishError(
                f"Publication path has a non-directory ancestor: {path}"
            ) from error

    identities: list[tuple[int, int]] = []
    while True:
        identities.append(_filesystem_identity(current))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return path_exists, tuple(identities)


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
        source_identity = _filesystem_identity(canonical_source)
        repo_identity = _filesystem_identity(canonical_repo)
        source_exists, source_ancestors = _existing_ancestor_identities(
            canonical_source
        )
        destination_exists, destination_ancestors = (
            _existing_ancestor_identities(canonical_destination)
        )
    except (OSError, RuntimeError) as error:
        raise PublishError(f"Could not resolve publication paths: {error}") from error

    if not source_exists:
        raise PublishError(f"Publication source root does not exist: {source}")
    destination_identity = (
        destination_ancestors[0] if destination_exists else None
    )
    if (
        source_identity in destination_ancestors
        or (
            destination_identity is not None
            and destination_identity in source_ancestors
        )
    ):
        raise PublishError(
            "Publication source and destination must not be equal or contain "
            f"one another: source={canonical_source}; "
            f"destination={canonical_destination}"
        )
    if repo_identity in destination_ancestors:
        raise PublishError(
            "Publication destination must be outside the repository: "
            f"{canonical_destination}"
        )
    try:
        git_metadata_roots = _git_metadata_roots(canonical_repo)
    except (OSError, RuntimeError) as error:
        raise PublishError(
            f"Could not resolve repository Git metadata roots: {error}"
        ) from error
    for metadata_path, metadata_identity in git_metadata_roots:
        _metadata_exists, metadata_ancestors = _existing_ancestor_identities(
            metadata_path
        )
        if (
            metadata_identity in destination_ancestors
            or (
                destination_identity is not None
                and destination_identity in metadata_ancestors
            )
        ):
            raise PublishError(
                "Publication destination must be outside Git metadata "
                "directories and must not contain them: "
                f"destination={canonical_destination}; metadata={metadata_path}"
            )
    return canonical_source, canonical_destination


def _atomic_rename_no_replace(
    source: str | bytes,
    destination: str | bytes,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically rename one directory entry only if the target is absent."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        try:
            rename_exclusive = libc.renameatx_np
        except AttributeError as error:
            raise PublishError(
                "This macOS runtime lacks atomic no-replace rename support"
            ) from error
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            src_dir_fd,
            source_bytes,
            dst_dir_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL from <sys/stdio.h>
        )
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = libc.renameat2
        except AttributeError as error:
            raise PublishError(
                "This Linux runtime lacks atomic no-replace rename support"
            ) from error
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            src_dir_fd,
            source_bytes,
            dst_dir_fd,
            destination_bytes,
            1,  # RENAME_NOREPLACE from <linux/fs.h>
        )
    else:
        raise PublishError(
            "Atomic publication replacement is supported only on macOS and Linux"
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            os.fsdecode(destination_bytes),
        )


def _ensure_destination_root(destination_root: Path) -> None:
    """Create an absent destination through anchored, non-symlink components."""

    if not destination_root.is_absolute():
        raise PublishError(
            f"Publication destination must be absolute: {destination_root}"
        )
    anchor = Path(destination_root.anchor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(anchor, flags)
        root_stat = os.fstat(root_descriptor)
    except OSError as error:
        raise PublishError(
            f"Could not anchor publication filesystem root {anchor}: {error}"
        ) from error
    current = DirectoryHandle(
        name=str(anchor),
        display_path=anchor,
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
        file_descriptor=root_descriptor,
    )
    try:
        for name in destination_root.parts[1:]:
            parent_descriptor = _require_directory_descriptor(current)
            display_path = current.display_path / name
            try:
                child = _open_directory_handle_at(
                    parent_descriptor,
                    name,
                    display_path,
                )
            except PublishError as error:
                if not isinstance(error.__cause__, FileNotFoundError):
                    raise
                child = _create_directory_handle_at(
                    parent_descriptor,
                    name,
                    display_path,
                    preserve_on_create_failure=False,
                )
            close_errors = _close_directory_handle(current)
            if close_errors:
                child_close_errors = _close_directory_handle(child)
                suffix = (
                    "; child descriptor cleanup also reported: "
                    + "; ".join(child_close_errors)
                    if child_close_errors
                    else ""
                )
                raise PublishError(
                    "Could not close an anchored publication path component "
                    f"{current.display_path}: "
                    f"{'; '.join(close_errors)}{suffix}"
                )
            current = child
    except BaseException as error:
        close_errors = _close_directory_handle(current)
        if close_errors:
            raise PublishError(
                f"{error}; anchored destination descriptor cleanup also "
                "reported: " + "; ".join(close_errors)
            ) from error
        raise
    else:
        close_errors = _close_directory_handle(current)
        if close_errors:
            raise PublishError(
                "Could not close anchored publication destination "
                f"{destination_root}: {'; '.join(close_errors)}"
            )


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


def _read_all(file_descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_regular_file_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    expected: os.stat_result,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        observed = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_dev != expected.st_dev
            or observed.st_ino != expected.st_ino
            or observed.st_nlink != 1
        ):
            raise PublishError(
                f"File changed or is hard-linked while reading: {display_path}"
            )
        payload = _read_all(file_descriptor)
        after = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_dev != observed.st_dev
            or after.st_ino != observed.st_ino
            or after.st_nlink != observed.st_nlink
            or after.st_size != observed.st_size
            or after.st_mtime_ns != observed.st_mtime_ns
            or after.st_ctime_ns != observed.st_ctime_ns
        ):
            raise PublishError(
                f"File changed while reading: {display_path}"
            )
        return payload
    except PublishError:
        raise
    except OSError as error:
        raise PublishError(f"Could not read {display_path}: {error}") from error
    finally:
        if file_descriptor is not None:
            close_errors = _close_file_descriptor(file_descriptor)
            if close_errors and sys.exc_info()[0] is None:
                raise PublishError(
                    f"Could not close {display_path}: {'; '.join(close_errors)}"
                )


def _walk_directory_handle(
    handle: DirectoryHandle,
    *,
    prefix: str = "",
    require_canonical_modes: bool = False,
    require_canonical_root: bool = True,
) -> list[tuple[str, bytes, bytes]]:
    file_descriptor = _require_directory_descriptor(handle)
    records: list[tuple[str, bytes, bytes]] = []
    try:
        before = os.fstat(file_descriptor)
        if (
            require_canonical_modes
            and require_canonical_root
            and stat.S_IMODE(before.st_mode) != CANONICAL_DIRECTORY_MODE
        ):
            raise PublishError(
                "Skill directory has noncanonical permissions "
                f"{stat.S_IMODE(before.st_mode):04o}; expected "
                f"{CANONICAL_DIRECTORY_MODE:04o}: {handle.display_path}"
            )
        names = sorted(os.listdir(file_descriptor))
    except OSError as error:
        raise PublishError(
            f"Could not list anchored directory {handle.display_path}: {error}"
        ) from error
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        display_path = handle.display_path / name
        try:
            metadata = os.stat(
                name,
                dir_fd=file_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise PublishError(
                f"Could not inspect anchored entry {display_path}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PublishError(
                f"Refusing anchored tree containing a symlink: {display_path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            if (
                require_canonical_modes
                and stat.S_IMODE(metadata.st_mode) != CANONICAL_DIRECTORY_MODE
            ):
                raise PublishError(
                    "Skill directory has noncanonical permissions "
                    f"{stat.S_IMODE(metadata.st_mode):04o}; expected "
                    f"{CANONICAL_DIRECTORY_MODE:04o}: {display_path}"
                )
            child = _open_directory_handle_at(
                file_descriptor,
                name,
                display_path,
            )
            try:
                records.append((relative, b"directory", b""))
                records.extend(
                    _walk_directory_handle(
                        child,
                        prefix=relative,
                        require_canonical_modes=require_canonical_modes,
                    )
                )
            finally:
                close_errors = _close_directory_handle(child)
                if close_errors and sys.exc_info()[0] is None:
                    raise PublishError(
                        f"Could not close {display_path}: "
                        + "; ".join(close_errors)
                    )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PublishError(
                f"Refusing unsupported anchored entry: {display_path}"
            )
        if (
            require_canonical_modes
            and stat.S_IMODE(metadata.st_mode) != CANONICAL_FILE_MODE
        ):
            raise PublishError(
                "Skill file has noncanonical permissions "
                f"{stat.S_IMODE(metadata.st_mode):04o}; expected "
                f"{CANONICAL_FILE_MODE:04o}: {display_path}"
            )
        records.append(
            (
                relative,
                b"file",
                _read_regular_file_at(
                    file_descriptor,
                    name,
                    display_path,
                    metadata,
                ),
            )
        )
    try:
        after_names = sorted(os.listdir(file_descriptor))
        after = os.fstat(file_descriptor)
    except OSError as error:
        raise PublishError(
            "Could not revalidate anchored directory "
            f"{handle.display_path}: {error}"
        ) from error
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if names != after_names or before_identity != after_identity:
        raise PublishError(
            "Anchored directory changed while its contents were being "
            f"validated: {handle.display_path}"
        )
    return records


def _destination_tree_digest_records(
    records: tuple[tuple[str, bytes, bytes], ...]
    | list[tuple[str, bytes, bytes]],
) -> str:
    return tree_digest_records(records)


def _destination_tree_digest_handle(handle: DirectoryHandle) -> str:
    return _destination_tree_digest_records(
        _walk_directory_handle(handle, require_canonical_modes=True)
    )


def _skill_tree_digest_records(
    records: tuple[tuple[str, bytes, bytes], ...]
    | list[tuple[str, bytes, bytes]],
) -> str:
    return _destination_tree_digest_records(records)


def _skill_tree_digest_handle(handle: DirectoryHandle) -> str:
    return _skill_tree_digest_records(
        _walk_directory_handle(handle, require_canonical_modes=True)
    )


def _skill_digest_from_tree_records(
    records: tuple[tuple[str, bytes, bytes], ...]
    | list[tuple[str, bytes, bytes]],
    folder: str,
) -> str:
    prefix = f"{folder}/"
    folder_records = [
        (relative.removeprefix(prefix), kind, payload)
        for relative, kind, payload in records
        if relative.startswith(prefix)
    ]
    return _skill_tree_digest_records(folder_records)


def _capture_destination_state_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> DestinationState:
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return DestinationState(kind="absent")
    except OSError as error:
        raise PublishError(
            f"Could not inspect skill destination {display_path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PublishError(
            f"Refusing to replace symlinked skill destination: {display_path}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublishError(
            f"Skill destination exists but is not a directory: {display_path}"
        )
    handle = _open_directory_handle_at(
        parent_descriptor,
        name,
        display_path,
    )
    try:
        return DestinationState(
            kind="directory",
            device=handle.device,
            inode=handle.inode,
            tree_sha256=_destination_tree_digest_handle(handle),
        )
    finally:
        close_errors = _close_directory_handle(handle)
        if close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                f"Could not close {display_path}: {'; '.join(close_errors)}"
            )


def preflight_destinations(
    dest_root: Path,
    root_descriptor: int,
) -> dict[str, DestinationState]:
    return {
        folder: _capture_destination_state_at(
            root_descriptor,
            folder,
            dest_root / folder,
        )
        for folder in SKILL_FOLDERS
    }


def _assert_no_recovery_transactions(
    destination_root: DestinationRootIdentity,
    transaction_lock: TransactionLock,
    *,
    allowed_directories: tuple[tuple[str, int, int], ...] = (),
) -> None:
    _assert_mutation_authorized(destination_root, transaction_lock)
    root_descriptor = transaction_lock.file_descriptor
    if root_descriptor is None:
        raise PublishError("Publication destination lock descriptor is missing")
    try:
        names = set(os.listdir(root_descriptor))
        allowed_by_name = {
            name: (device, inode)
            for name, device, inode in allowed_directories
        }
        recovery_names: list[str] = []
        for name in sorted(names):
            if not (
                name.startswith(TRANSACTION_PREFIX)
                or name.startswith(CLEANUP_QUARANTINE_PREFIX)
            ):
                continue
            expected_identity = allowed_by_name.get(name)
            if expected_identity is None:
                recovery_names.append(name)
                continue
            observed = os.stat(
                name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != expected_identity
            ):
                recovery_names.append(name)
        recovery_names.extend(
            name
            for name in sorted(allowed_by_name)
            if name not in names
        )
    except OSError as error:
        raise PublishError(
            "Could not inspect publication destination for recovery "
            f"transactions: {destination_root.path}",
            preserve_transaction=True,
        ) from error
    _assert_mutation_authorized(destination_root, transaction_lock)
    if recovery_names:
        raise PublishError(
            "Unresolved publication recovery transaction blocks publication: "
            + ", ".join(
                str(destination_root.path / name)
                for name in recovery_names
            ),
            preserve_transaction=True,
        )


def _copy_source_directory(
    source: Path,
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> None:
    try:
        source_metadata = source.lstat()
    except OSError as error:
        raise PublishError(f"Could not inspect source {source}: {error}") from error
    if source.is_symlink() or not stat.S_ISDIR(source_metadata.st_mode):
        raise PublishError(f"Source directory changed during staging: {source}")
    source_mode = stat.S_IMODE(source_metadata.st_mode)
    if source_mode != CANONICAL_DIRECTORY_MODE:
        raise PublishError(
            "Source skill directory has noncanonical permissions "
            f"{source_mode:04o}; expected {CANONICAL_DIRECTORY_MODE:04o}: "
            f"{source}"
        )
    destination = _create_directory_handle_at(
        parent_descriptor,
        name,
        display_path,
    )
    destination_descriptor = _require_directory_descriptor(destination)
    try:
        for child in sorted(source.iterdir()):
            child_metadata = child.lstat()
            child_display = display_path / child.name
            if child.is_symlink():
                raise PublishError(
                    f"Source symlink appeared during staging: {child}"
                )
            if stat.S_ISDIR(child_metadata.st_mode):
                _copy_source_directory(
                    child,
                    destination_descriptor,
                    child.name,
                    child_display,
                )
                continue
            if not stat.S_ISREG(child_metadata.st_mode):
                raise PublishError(
                    f"Unsupported source entry during staging: {child}"
                )
            child_mode = stat.S_IMODE(child_metadata.st_mode)
            if child_mode != CANONICAL_FILE_MODE:
                raise PublishError(
                    "Source skill file has noncanonical permissions "
                    f"{child_mode:04o}; expected {CANONICAL_FILE_MODE:04o}: "
                    f"{child}"
                )
            source_descriptor = os.open(
                child,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            target_descriptor: int | None = None
            try:
                opened_source = os.fstat(source_descriptor)
                if (
                    not stat.S_ISREG(opened_source.st_mode)
                    or opened_source.st_dev != child_metadata.st_dev
                    or opened_source.st_ino != child_metadata.st_ino
                    or opened_source.st_mode != child_metadata.st_mode
                ):
                    raise PublishError(
                        f"Source file changed during staging: {child}"
                    )
                target_descriptor = os.open(
                    child.name,
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=destination_descriptor,
                )
                _write_all(target_descriptor, _read_all(source_descriptor))
                os.fchmod(
                    target_descriptor,
                    CANONICAL_FILE_MODE,
                )
                os.fsync(target_descriptor)
            finally:
                source_close_errors = _close_file_descriptor(source_descriptor)
                target_close_errors = (
                    _close_file_descriptor(target_descriptor)
                    if target_descriptor is not None
                    else []
                )
                if (
                    (source_close_errors or target_close_errors)
                    and sys.exc_info()[0] is None
                ):
                    raise PublishError(
                        f"Could not close staged file {child_display}: "
                        + "; ".join(source_close_errors + target_close_errors)
                    )
        os.fchmod(
            destination_descriptor,
            CANONICAL_DIRECTORY_MODE,
        )
        _fsync_directory_descriptor(
            destination_descriptor,
            display_path,
            preserve_transaction=True,
        )
    finally:
        close_errors = _close_directory_handle(destination)
        if close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                f"Could not close staged directory {display_path}: "
                + "; ".join(close_errors)
            )


def _stage_all(
    source_root: Path,
    transaction_root: DirectoryHandle,
) -> DirectoryHandle:
    transaction_descriptor = _require_directory_descriptor(transaction_root)
    stage_root = _create_directory_handle_at(
        transaction_descriptor,
        "stage",
        transaction_root.display_path / "stage",
    )
    stage_descriptor = _require_directory_descriptor(stage_root)
    try:
        for folder in SKILL_FOLDERS:
            _copy_source_directory(
                source_root / folder,
                stage_descriptor,
                folder,
                stage_root.display_path / folder,
            )
    except BaseException as error:
        close_errors = _close_directory_handle(stage_root)
        if close_errors:
            raise PublishError(
                f"{error}; staged-root descriptor cleanup also reported: "
                + "; ".join(close_errors),
                preserve_transaction=True,
            ) from error
        raise
    _fsync_directory_descriptor(
        stage_descriptor,
        stage_root.display_path,
        preserve_transaction=True,
    )
    return stage_root


def _preflight_skill_tree_handle(root: DirectoryHandle) -> None:
    root_descriptor = _require_directory_descriptor(root)
    try:
        observed = tuple(sorted(os.listdir(root_descriptor)))
    except OSError as error:
        raise PublishError(
            f"Could not inspect staged skill tree {root.display_path}: {error}"
        ) from error
    expected = tuple(sorted(SKILL_FOLDERS))
    if observed != expected:
        raise PublishError(
            "Staged skill folders differ: "
            f"expected {', '.join(expected)}; observed {', '.join(observed)}"
        )
    for folder in SKILL_FOLDERS:
        state = _capture_destination_state_at(
            root_descriptor,
            folder,
            root.display_path / folder,
        )
        if state.kind != "directory":
            raise PublishError(
                f"Staged skill is not a directory: {root.display_path / folder}"
            )


def _skill_digest_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> str:
    handle = _open_directory_handle_at(
        parent_descriptor,
        name,
        display_path,
    )
    try:
        return _skill_tree_digest_handle(handle)
    finally:
        close_errors = _close_directory_handle(handle)
        if close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                f"Could not close {display_path}: {'; '.join(close_errors)}"
            )


def _capture_skill_verification_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> SkillVerification:
    handle = _open_directory_handle_at(
        parent_descriptor,
        name,
        display_path,
    )
    try:
        records = _walk_directory_handle(
            handle,
            require_canonical_modes=True,
        )
        return SkillVerification(
            destination_state=DestinationState(
                kind="directory",
                device=handle.device,
                inode=handle.inode,
                tree_sha256=_destination_tree_digest_records(records),
            ),
            skill_sha256=_skill_tree_digest_records(records),
        )
    finally:
        close_errors = _close_directory_handle(handle)
        if close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                f"Could not close {display_path}: {'; '.join(close_errors)}"
            )


def _capture_installed_skill_set(
    root_descriptor: int,
    destination_root: Path,
    folders: tuple[str, ...],
) -> dict[str, SkillVerification]:
    return {
        folder: _capture_skill_verification_at(
            root_descriptor,
            folder,
            destination_root / folder,
        )
        for folder in folders
    }


def _verify_installed_skill_set(
    root_descriptor: int,
    destination_root: Path,
    states: list[ReplacementState],
    expected_skill_digests: dict[str, str],
) -> None:
    """Require two complete, consistent observations before commit."""

    first = _capture_installed_skill_set(
        root_descriptor,
        destination_root,
        tuple(SKILL_FOLDERS),
    )
    second = _capture_installed_skill_set(
        root_descriptor,
        destination_root,
        tuple(reversed(SKILL_FOLDERS)),
    )
    if first != second:
        raise PublishError(
            "Installed skill tree changed across final verification passes.",
            preserve_transaction=True,
        )
    mismatched: list[str] = []
    for folder, state in zip(SKILL_FOLDERS, states, strict=True):
        observed = second[folder]
        if (
            state.installed_state is None
            or observed.destination_state != state.installed_state
            or observed.skill_sha256 != expected_skill_digests[folder]
        ):
            mismatched.append(folder)
    if mismatched:
        raise PublishError(
            "Installed skill verification failed for: "
            + ", ".join(mismatched),
            preserve_transaction=True,
        )


def _remove_verified_file_via_quarantine(
    source_descriptor: int,
    source_name: str,
    source_path: Path,
    expected_metadata: os.stat_result,
    expected_payload: bytes,
    cleanup_quarantine: DirectoryHandle,
) -> None:
    """Move, re-verify, and remove one file without unlinking its source name."""

    quarantine_descriptor = _require_directory_descriptor(cleanup_quarantine)
    quarantine_name = f"{CLEANUP_FILE_PREFIX}{secrets.token_hex(16)}"
    quarantine_path = cleanup_quarantine.display_path / quarantine_name
    try:
        _atomic_rename_no_replace(
            source_name,
            quarantine_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=quarantine_descriptor,
        )
        _fsync_directory_descriptor(
            quarantine_descriptor,
            cleanup_quarantine.display_path,
            preserve_transaction=True,
        )
        _fsync_directory_descriptor(
            source_descriptor,
            source_path.parent,
            preserve_transaction=True,
        )
    except BaseException as error:
        raise PublishError(
            "Could not durably move a verified cleanup file into its private "
            f"quarantine; preserving transaction state for {source_path}: "
            f"{error}",
            preserve_transaction=True,
        ) from error

    mismatch: str | None = None
    try:
        moved_metadata = os.stat(
            quarantine_name,
            dir_fd=quarantine_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(moved_metadata.st_mode)
            or moved_metadata.st_dev != expected_metadata.st_dev
            or moved_metadata.st_ino != expected_metadata.st_ino
        ):
            mismatch = (
                "cleanup quarantine received a different file identity"
            )
        else:
            moved_payload = _read_regular_file_at(
                quarantine_descriptor,
                quarantine_name,
                quarantine_path,
                moved_metadata,
            )
            if moved_payload != expected_payload:
                mismatch = "cleanup quarantine received different file bytes"
            else:
                revalidated_metadata = os.stat(
                    quarantine_name,
                    dir_fd=quarantine_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(revalidated_metadata.st_mode)
                    or revalidated_metadata.st_dev != expected_metadata.st_dev
                    or revalidated_metadata.st_ino != expected_metadata.st_ino
                ):
                    mismatch = (
                        "cleanup quarantine file identity changed before "
                        "deletion"
                    )
                else:
                    revalidated_payload = _read_regular_file_at(
                        quarantine_descriptor,
                        quarantine_name,
                        quarantine_path,
                        revalidated_metadata,
                    )
                    if revalidated_payload != expected_payload:
                        mismatch = (
                            "cleanup quarantine file bytes changed before "
                            "deletion"
                        )
                    else:
                        final_metadata = os.stat(
                            quarantine_name,
                            dir_fd=quarantine_descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(final_metadata.st_mode)
                            or final_metadata.st_dev
                            != revalidated_metadata.st_dev
                            or final_metadata.st_ino
                            != revalidated_metadata.st_ino
                            or final_metadata.st_nlink
                            != revalidated_metadata.st_nlink
                            or final_metadata.st_size
                            != revalidated_metadata.st_size
                            or final_metadata.st_mtime_ns
                            != revalidated_metadata.st_mtime_ns
                            or final_metadata.st_ctime_ns
                            != revalidated_metadata.st_ctime_ns
                        ):
                            mismatch = (
                                "cleanup quarantine file changed immediately "
                                "before deletion"
                            )
    except BaseException as error:
        mismatch = f"cleanup quarantine verification failed: {error}"

    if mismatch is not None:
        restoration = "the moved entry remains in the cleanup quarantine"
        try:
            os.stat(
                source_name,
                dir_fd=source_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                _atomic_rename_no_replace(
                    quarantine_name,
                    source_name,
                    src_dir_fd=quarantine_descriptor,
                    dst_dir_fd=source_descriptor,
                )
                _fsync_directory_descriptor(
                    source_descriptor,
                    source_path.parent,
                    preserve_transaction=True,
                )
                _fsync_directory_descriptor(
                    quarantine_descriptor,
                    cleanup_quarantine.display_path,
                    preserve_transaction=True,
                )
                restoration = "the moved entry was restored to its source name"
            except BaseException as restore_error:
                restoration = (
                    "restoration was not safe and the recovery entry was "
                    f"preserved: {restore_error}"
                )
        except OSError as source_error:
            restoration = (
                "the source name could not be checked, so the recovery entry "
                f"was preserved: {source_error}"
            )
        raise PublishError(
            "Transaction cleanup detected a per-file replacement after "
            f"verification at {source_path}: {mismatch}; {restoration}",
            preserve_transaction=True,
        )

    # POSIX has no inode-conditional unlink. The name is now inside a freshly
    # created 0700 random quarantine and has passed two identity/content reads
    # plus a final metadata check; mutation after this point requires a
    # same-user process deliberately racing the private quarantine.
    try:
        os.unlink(
            quarantine_name,
            dir_fd=quarantine_descriptor,
        )
        _fsync_directory_descriptor(
            quarantine_descriptor,
            cleanup_quarantine.display_path,
            preserve_transaction=True,
        )
    except BaseException as error:
        raise PublishError(
            "Could not remove a post-verified private cleanup entry; "
            f"preserving transaction state at {quarantine_path}: {error}",
            preserve_transaction=True,
        ) from error


def _expected_subtree_records(
    expected_records: tuple[tuple[str, bytes, bytes], ...],
    prefix: str,
) -> tuple[tuple[str, bytes, bytes], ...]:
    prefix_with_separator = f"{prefix}/"
    return tuple(
        (
            relative[len(prefix_with_separator) :],
            kind,
            payload,
        )
        for relative, kind, payload in expected_records
        if relative.startswith(prefix_with_separator)
    )


def _quarantine_open_verified_directory(
    source_descriptor: int,
    source_name: str,
    source_path: Path,
    source_handle: DirectoryHandle,
    expected_records: tuple[tuple[str, bytes, bytes], ...],
    cleanup_quarantine: DirectoryHandle,
) -> tuple[str, Path]:
    """Move an open directory into quarantine and verify it before recursion."""

    quarantine_descriptor = _require_directory_descriptor(cleanup_quarantine)
    quarantine_name = f"{CLEANUP_DIRECTORY_PREFIX}{secrets.token_hex(16)}"
    quarantine_path = cleanup_quarantine.display_path / quarantine_name
    try:
        _atomic_rename_no_replace(
            source_name,
            quarantine_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=quarantine_descriptor,
        )
        _fsync_directory_descriptor(
            quarantine_descriptor,
            cleanup_quarantine.display_path,
            preserve_transaction=True,
        )
        _fsync_directory_descriptor(
            source_descriptor,
            source_path.parent,
            preserve_transaction=True,
        )
    except BaseException as error:
        raise PublishError(
            "Could not durably move a verified cleanup directory into its "
            f"private quarantine; preserving transaction state for "
            f"{source_path}: {error}",
            preserve_transaction=True,
        ) from error

    mismatch: str | None = None
    try:
        moved_metadata = os.stat(
            quarantine_name,
            dir_fd=quarantine_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(moved_metadata.st_mode)
            or moved_metadata.st_dev != source_handle.device
            or moved_metadata.st_ino != source_handle.inode
        ):
            mismatch = (
                "cleanup quarantine received a different directory identity"
            )
        elif tuple(_walk_directory_handle(source_handle)) != expected_records:
            mismatch = (
                "cleanup quarantine received different directory contents"
            )
    except BaseException as error:
        mismatch = f"cleanup quarantine verification failed: {error}"

    if mismatch is not None:
        restoration = "the moved directory remains in the cleanup quarantine"
        try:
            os.stat(
                source_name,
                dir_fd=source_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                _atomic_rename_no_replace(
                    quarantine_name,
                    source_name,
                    src_dir_fd=quarantine_descriptor,
                    dst_dir_fd=source_descriptor,
                )
                _fsync_directory_descriptor(
                    source_descriptor,
                    source_path.parent,
                    preserve_transaction=True,
                )
                _fsync_directory_descriptor(
                    quarantine_descriptor,
                    cleanup_quarantine.display_path,
                    preserve_transaction=True,
                )
                restoration = (
                    "the moved directory was restored to its source name"
                )
            except BaseException as restore_error:
                restoration = (
                    "restoration was not safe and the recovery directory was "
                    f"preserved: {restore_error}"
                )
        except OSError as source_error:
            restoration = (
                "the source name could not be checked, so the recovery "
                f"directory was preserved: {source_error}"
            )
        raise PublishError(
            "Transaction cleanup detected a directory replacement after "
            f"verification at {source_path}: {mismatch}; {restoration}",
            preserve_transaction=True,
        )

    source_handle.name = quarantine_name
    source_handle.display_path = quarantine_path
    return quarantine_name, quarantine_path


def _clear_directory_handle(
    handle: DirectoryHandle,
    *,
    expected_records: tuple[tuple[str, bytes, bytes], ...] | None = None,
    prefix: str = "",
    cleanup_quarantine: DirectoryHandle | None = None,
) -> None:
    file_descriptor = _require_directory_descriptor(handle)
    try:
        current_mode = stat.S_IMODE(os.fstat(file_descriptor).st_mode)
        os.fchmod(
            file_descriptor,
            current_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
        )
    except OSError as error:
        raise PublishError(
            f"Could not make transaction directory removable "
            f"{handle.display_path}: {error}"
        ) from error
    try:
        names = sorted(os.listdir(file_descriptor))
    except OSError as error:
        raise PublishError(
            f"Could not list transaction directory {handle.display_path}: {error}"
        ) from error
    expected_entries: dict[str, tuple[str, bytes, bytes]] = {}
    if expected_records is not None:
        prefix_with_separator = f"{prefix}/" if prefix else ""
        for relative, kind, payload in expected_records:
            if prefix_with_separator:
                if not relative.startswith(prefix_with_separator):
                    continue
                remainder = relative[len(prefix_with_separator) :]
            else:
                remainder = relative
            if "/" not in remainder:
                expected_entries[remainder] = (relative, kind, payload)
        if tuple(names) != tuple(sorted(expected_entries)):
            raise PublishError(
                "Transaction cleanup tree gained unrecognized content; "
                f"preserving {handle.display_path}",
                preserve_transaction=True,
            )
    for name in names:
        display_path = handle.display_path / name
        expected_entry = expected_entries.get(name)
        try:
            metadata = os.stat(
                name,
                dir_fd=file_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise PublishError(
                f"Could not inspect transaction entry {display_path}: {error}"
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            if expected_records is not None and (
                expected_entry is None or expected_entry[1] != b"directory"
            ):
                raise PublishError(
                    "Transaction cleanup entry type changed; preserving "
                    f"{display_path}",
                    preserve_transaction=True,
                )
            child = _open_directory_handle_at(
                file_descriptor,
                name,
                display_path,
            )
            child_device = child.device
            child_inode = child.inode
            cleanup_name: str | None = None
            cleanup_path = display_path
            recursive_error: BaseException | None = None
            try:
                if expected_records is not None:
                    if cleanup_quarantine is None:
                        raise PublishError(
                            "Verified transaction cleanup lacks a private "
                            f"quarantine; preserving {display_path}",
                            preserve_transaction=True,
                        )
                    assert expected_entry is not None
                    cleanup_name, cleanup_path = (
                        _quarantine_open_verified_directory(
                            file_descriptor,
                            name,
                            display_path,
                            child,
                            _expected_subtree_records(
                                expected_records,
                                expected_entry[0],
                            ),
                            cleanup_quarantine,
                        )
                    )
                _clear_directory_handle(
                    child,
                    expected_records=expected_records,
                    prefix=(
                        expected_entry[0]
                        if expected_entry is not None
                        else ""
                    ),
                    cleanup_quarantine=cleanup_quarantine,
                )
            except BaseException as error:
                recursive_error = error
            close_errors = _close_directory_handle(child)
            if recursive_error is not None:
                if close_errors:
                    raise PublishError(
                        f"{recursive_error}; closing transaction directory "
                        f"{display_path} also reported: "
                        + "; ".join(close_errors),
                        preserve_transaction=True,
                    ) from recursive_error
                raise recursive_error
            if close_errors:
                raise PublishError(
                    f"Could not close transaction directory {cleanup_path}: "
                    + "; ".join(close_errors),
                    preserve_transaction=True,
                )
            removal_descriptor = file_descriptor
            removal_name = name
            if expected_records is not None:
                assert cleanup_quarantine is not None
                assert cleanup_name is not None
                removal_descriptor = _require_directory_descriptor(
                    cleanup_quarantine
                )
                removal_name = cleanup_name
            observed = os.stat(
                removal_name,
                dir_fd=removal_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_dev != child_device
                or observed.st_ino != child_inode
            ):
                raise PublishError(
                    "Transaction directory changed before removal: "
                    f"{cleanup_path}",
                    preserve_transaction=expected_records is not None,
                )
            try:
                os.rmdir(removal_name, dir_fd=removal_descriptor)
            except OSError as error:
                if expected_records is not None and error.errno in {
                    errno.EEXIST,
                    errno.ENOTEMPTY,
                }:
                    raise PublishError(
                        "Transaction cleanup directory gained unrecognized "
                        f"content; preserving {cleanup_path}",
                        preserve_transaction=True,
                    ) from error
                raise
            if expected_records is not None:
                assert cleanup_quarantine is not None
                _fsync_directory_descriptor(
                    removal_descriptor,
                    cleanup_quarantine.display_path,
                    preserve_transaction=True,
                )
            continue
        if expected_records is not None:
            if (
                expected_entry is None
                or expected_entry[1] != b"file"
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise PublishError(
                    "Transaction cleanup entry type changed; preserving "
                    f"{display_path}",
                    preserve_transaction=True,
                )
            observed_payload = _read_regular_file_at(
                file_descriptor,
                name,
                display_path,
                metadata,
            )
            if observed_payload != expected_entry[2]:
                raise PublishError(
                    "Transaction cleanup file changed; preserving "
                    f"{display_path}",
                    preserve_transaction=True,
                )
            observed = os.stat(
                name,
                dir_fd=file_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_dev != metadata.st_dev
                or observed.st_ino != metadata.st_ino
            ):
                raise PublishError(
                    "Transaction cleanup file identity changed; preserving "
                    f"{display_path}",
                    preserve_transaction=True,
                )
            if cleanup_quarantine is None:
                raise PublishError(
                    "Verified transaction cleanup lacks a private quarantine; "
                    f"preserving {display_path}",
                    preserve_transaction=True,
                )
            _remove_verified_file_via_quarantine(
                file_descriptor,
                name,
                display_path,
                metadata,
                expected_entry[2],
                cleanup_quarantine,
            )
            continue
        os.unlink(name, dir_fd=file_descriptor)
    if expected_records is not None and os.listdir(file_descriptor):
        raise PublishError(
            "Transaction cleanup tree gained unrecognized content; "
            f"preserving {handle.display_path}",
            preserve_transaction=True,
        )
    _fsync_directory_descriptor(
        file_descriptor,
        handle.display_path,
        preserve_transaction=True,
    )


def _remove_transaction_workspace(
    transaction_root: DirectoryHandle,
    destination_root: DestinationRootIdentity,
    transaction_lock: TransactionLock,
    cleanup_plan: TransactionCleanupPlan | None = None,
) -> None:
    _assert_mutation_authorized(destination_root, transaction_lock)
    root_descriptor = transaction_lock.file_descriptor
    if root_descriptor is None:
        raise PublishError("Publication destination lock descriptor is missing")
    if cleanup_plan is not None:
        _verify_success_cleanup_state(transaction_root, cleanup_plan)
    expected_records = tuple(_walk_directory_handle(transaction_root))
    _assert_mutation_authorized(destination_root, transaction_lock)
    cleanup_name = (
        f"{CLEANUP_QUARANTINE_PREFIX}{secrets.token_hex(16)}"
    )
    cleanup_path = destination_root.path / cleanup_name
    root_cleanup = _create_directory_handle_at(
        root_descriptor,
        cleanup_name,
        cleanup_path,
    )
    cleanup_device = root_cleanup.device
    cleanup_inode = root_cleanup.inode
    quarantined_name: str | None = None
    operation_error: BaseException | None = None
    try:
        quarantined_name, _quarantined_path = (
            _quarantine_open_verified_directory(
                root_descriptor,
                transaction_root.name,
                transaction_root.display_path,
                transaction_root,
                expected_records,
                root_cleanup,
            )
        )
        # A same-prefix public entry appearing after the root move is
        # unrecognized state. Detect it before deleting any accepted recovery
        # child. Deliberate mutation after this check would require access to
        # the fresh random 0700 quarantine and remains the documented same-UID
        # POSIX boundary.
        _assert_no_recovery_transactions(
            destination_root,
            transaction_lock,
            allowed_directories=(
                (cleanup_name, root_cleanup.device, root_cleanup.inode),
            ),
        )
        if cleanup_plan is not None:
            _clear_verified_success_workspace(transaction_root, cleanup_plan)
        else:
            _clear_directory_handle(
                transaction_root,
                expected_records=expected_records,
                cleanup_quarantine=root_cleanup,
            )
        transaction_close_errors = _close_directory_handle(transaction_root)
        if transaction_close_errors:
            raise PublishError(
                "Could not close privately quarantined transaction workspace "
                "before removal: "
                + "; ".join(transaction_close_errors),
                preserve_transaction=True,
            )
        if quarantined_name is None:
            raise PublishError(
                "Transaction workspace was not moved into private cleanup "
                "quarantine",
                preserve_transaction=True,
            )
        _remove_verified_empty_directory_at(
            _require_directory_descriptor(root_cleanup),
            quarantined_name,
            transaction_root.display_path,
            transaction_root.device,
            transaction_root.inode,
            root_cleanup,
        )
    except BaseException as error:
        operation_error = error
    transaction_close_errors = _close_directory_handle(transaction_root)
    if transaction_close_errors:
        if operation_error is None:
            operation_error = PublishError(
                "Could not close transaction workspace during cleanup: "
                + "; ".join(transaction_close_errors),
                preserve_transaction=True,
            )
        else:
            operation_error = PublishError(
                f"{operation_error}; transaction workspace closure also "
                f"reported: {'; '.join(transaction_close_errors)}",
                preserve_transaction=True,
            )
    root_cleanup_close_errors = _close_directory_handle(root_cleanup)
    if operation_error is not None:
        if root_cleanup_close_errors:
            raise PublishError(
                f"{operation_error}; root cleanup quarantine closure also "
                f"reported: {'; '.join(root_cleanup_close_errors)}",
                preserve_transaction=True,
            ) from operation_error
        raise operation_error
    if root_cleanup_close_errors:
        raise PublishError(
            "Could not close root cleanup quarantine: "
            + "; ".join(root_cleanup_close_errors),
            preserve_transaction=True,
        )
    _remove_fresh_private_empty_directory_at(
        root_descriptor,
        cleanup_name,
        cleanup_path,
        cleanup_device,
        cleanup_inode,
    )
    # The transaction's original name is a public recovery boundary. A
    # concurrent same-name entry may appear after the accepted directory moves
    # into private quarantine, so verify all recovery prefixes again while the
    # destination lock is still held.
    _assert_no_recovery_transactions(
        destination_root,
        transaction_lock,
    )


def _verify_owned_cleanup_directory(
    transaction_descriptor: int,
    name: str,
    expected_device: int,
    expected_inode: int,
    display_path: Path,
) -> DirectoryHandle:
    handle = _open_directory_handle_at(
        transaction_descriptor,
        name,
        display_path,
    )
    if handle.device != expected_device or handle.inode != expected_inode:
        close_errors = _close_directory_handle(handle)
        close_suffix = (
            "; descriptor cleanup also reported: " + "; ".join(close_errors)
            if close_errors
            else ""
        )
        raise PublishError(
            "Transaction cleanup directory identity changed; preserving "
            f"unrecognized content at {display_path}{close_suffix}",
            preserve_transaction=True,
        )
    return handle


def _verify_success_cleanup_state(
    transaction_root: DirectoryHandle,
    cleanup_plan: TransactionCleanupPlan,
) -> None:
    """Refuse committed cleanup unless every remaining entry is recognized."""

    transaction_descriptor = _require_directory_descriptor(transaction_root)
    expected_root_entries = ("backups", "quarantine", "stage")
    observed_root_entries = tuple(sorted(os.listdir(transaction_descriptor)))
    if observed_root_entries != expected_root_entries:
        raise PublishError(
            "Transaction workspace contains unrecognized entries after "
            "publication; preserving it for review: "
            f"{transaction_root.display_path}",
            preserve_transaction=True,
        )

    expected_directories = (
        (
            "stage",
            cleanup_plan.stage_device,
            cleanup_plan.stage_inode,
        ),
        (
            "backups",
            cleanup_plan.backup_device,
            cleanup_plan.backup_inode,
        ),
        (
            "quarantine",
            cleanup_plan.quarantine_device,
            cleanup_plan.quarantine_inode,
        ),
    )
    handles: dict[str, DirectoryHandle] = {}
    try:
        for name, device, inode in expected_directories:
            handles[name] = _verify_owned_cleanup_directory(
                transaction_descriptor,
                name,
                device,
                inode,
                transaction_root.display_path / name,
            )

        for name in ("stage", "quarantine"):
            descriptor = _require_directory_descriptor(handles[name])
            if os.listdir(descriptor):
                raise PublishError(
                    "Transaction cleanup found unrecognized committed "
                    f"content in {handles[name].display_path}; preserving it "
                    "for review",
                    preserve_transaction=True,
                )

        backup_handle = handles["backups"]
        backup_descriptor = _require_directory_descriptor(backup_handle)
        expected_backups = dict(cleanup_plan.backup_states)
        observed_backup_names = tuple(sorted(os.listdir(backup_descriptor)))
        if observed_backup_names != tuple(sorted(expected_backups)):
            raise PublishError(
                "Transaction cleanup found unrecognized backup entries in "
                f"{backup_handle.display_path}; preserving them for review",
                preserve_transaction=True,
            )
        for folder, expected_state in expected_backups.items():
            observed_state = _capture_destination_state_at(
                backup_descriptor,
                folder,
                backup_handle.display_path / folder,
            )
            if observed_state != expected_state:
                raise PublishError(
                    "Transaction cleanup found changed backup content at "
                    f"{backup_handle.display_path / folder}; preserving it "
                    "for review",
                    preserve_transaction=True,
                )
    finally:
        close_errors: list[str] = []
        for handle in handles.values():
            close_errors.extend(_close_directory_handle(handle))
        if close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                "Could not close transaction cleanup verification "
                "directories: " + "; ".join(close_errors),
                preserve_transaction=True,
            )


def _remove_verified_directory_tree_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    expected_state: DestinationState,
    cleanup_quarantine: DirectoryHandle,
) -> None:
    observed_state = _capture_destination_state_at(
        parent_descriptor,
        name,
        display_path,
    )
    if observed_state != expected_state:
        raise PublishError(
            "Transaction cleanup tree changed; preserving unrecognized "
            f"content at {display_path}",
            preserve_transaction=True,
        )
    handle = _open_directory_handle_at(
        parent_descriptor,
        name,
        display_path,
    )
    if (
        handle.device != expected_state.device
        or handle.inode != expected_state.inode
    ):
        close_errors = _close_directory_handle(handle)
        close_suffix = (
            "; descriptor cleanup also reported: " + "; ".join(close_errors)
            if close_errors
            else ""
        )
        raise PublishError(
            "Transaction cleanup tree identity changed; preserving "
            f"unrecognized content at {display_path}{close_suffix}",
            preserve_transaction=True,
        )
    cleanup_name: str | None = None
    cleanup_path = display_path
    recursive_error: BaseException | None = None
    try:
        cleanup_records = tuple(_walk_directory_handle(handle))
        cleanup_digest = _destination_tree_digest_records(cleanup_records)
        if cleanup_digest != expected_state.tree_sha256:
            raise PublishError(
                "Transaction cleanup tree changed before deletion; preserving "
                f"unrecognized content at {display_path}",
                preserve_transaction=True,
            )
        cleanup_name, cleanup_path = _quarantine_open_verified_directory(
            parent_descriptor,
            name,
            display_path,
            handle,
            cleanup_records,
            cleanup_quarantine,
        )
        _clear_directory_handle(
            handle,
            expected_records=cleanup_records,
            cleanup_quarantine=cleanup_quarantine,
        )
    except BaseException as error:
        recursive_error = error
    close_errors = _close_directory_handle(handle)
    if recursive_error is not None:
        if close_errors:
            raise PublishError(
                f"{recursive_error}; cleanup descriptor closure also "
                f"reported at {cleanup_path}: "
                + "; ".join(close_errors),
                preserve_transaction=True,
            ) from recursive_error
        raise recursive_error
    if close_errors:
        raise PublishError(
            f"Could not close verified cleanup tree {cleanup_path}: "
            + "; ".join(close_errors),
            preserve_transaction=True,
        )
    if cleanup_name is None:
        raise PublishError(
            "Verified cleanup tree was not moved into private quarantine; "
            f"preserving {display_path}",
            preserve_transaction=True,
        )
    quarantine_descriptor = _require_directory_descriptor(cleanup_quarantine)
    observed = os.stat(
        cleanup_name,
        dir_fd=quarantine_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != expected_state.device
        or observed.st_ino != expected_state.inode
    ):
        raise PublishError(
            "Transaction cleanup tree changed before removal; preserving "
            f"unrecognized content at {cleanup_path}",
            preserve_transaction=True,
        )
    try:
        os.rmdir(cleanup_name, dir_fd=quarantine_descriptor)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise PublishError(
                "Transaction cleanup directory gained unrecognized content; "
                f"preserving {cleanup_path}",
                preserve_transaction=True,
            ) from error
        raise
    _fsync_directory_descriptor(
        quarantine_descriptor,
        cleanup_quarantine.display_path,
        preserve_transaction=True,
    )


def _remove_verified_empty_directory_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    expected_device: int,
    expected_inode: int,
    cleanup_quarantine: DirectoryHandle,
) -> None:
    """Move an expected empty directory into private quarantine before removal."""

    handle = _verify_owned_cleanup_directory(
        parent_descriptor,
        name,
        expected_device,
        expected_inode,
        display_path,
    )
    quarantine_descriptor = _require_directory_descriptor(cleanup_quarantine)
    quarantine_name = f"{CLEANUP_DIRECTORY_PREFIX}{secrets.token_hex(16)}"
    quarantine_path = cleanup_quarantine.display_path / quarantine_name
    operation_error: BaseException | None = None
    try:
        if os.listdir(_require_directory_descriptor(handle)):
            raise PublishError(
                "Transaction cleanup found unrecognized content in "
                f"{display_path}; preserving it for review",
                preserve_transaction=True,
            )
        _atomic_rename_no_replace(
            name,
            quarantine_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=quarantine_descriptor,
        )
        _fsync_directory_descriptor(
            parent_descriptor,
            display_path.parent,
            preserve_transaction=True,
        )
        _fsync_directory_descriptor(
            quarantine_descriptor,
            cleanup_quarantine.display_path,
            preserve_transaction=True,
        )

        moved = os.stat(
            quarantine_name,
            dir_fd=quarantine_descriptor,
            follow_symlinks=False,
        )
        moved_handle = os.fstat(_require_directory_descriptor(handle))
        if (
            not stat.S_ISDIR(moved.st_mode)
            or moved.st_dev != expected_device
            or moved.st_ino != expected_inode
            or moved_handle.st_dev != expected_device
            or moved_handle.st_ino != expected_inode
            or os.listdir(_require_directory_descriptor(handle))
        ):
            restoration = (
                "the moved directory remains in the private cleanup quarantine"
            )
            try:
                os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                try:
                    _atomic_rename_no_replace(
                        quarantine_name,
                        name,
                        src_dir_fd=quarantine_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    _fsync_directory_descriptor(
                        parent_descriptor,
                        display_path.parent,
                        preserve_transaction=True,
                    )
                    _fsync_directory_descriptor(
                        quarantine_descriptor,
                        cleanup_quarantine.display_path,
                        preserve_transaction=True,
                    )
                    restoration = (
                        "the moved directory was restored to its public name"
                    )
                except BaseException as restore_error:
                    restoration = (
                        "restoration was not safe and the moved directory "
                        f"remains preserved: {restore_error}"
                    )
            except OSError as source_error:
                restoration = (
                    "the public name could not be checked, so the moved "
                    f"directory remains preserved: {source_error}"
                )
            raise PublishError(
                "Transaction cleanup moved a substituted empty directory; "
                f"{restoration}: {display_path}",
                preserve_transaction=True,
            )
        handle.name = quarantine_name
        handle.display_path = quarantine_path
    except BaseException as error:
        operation_error = error

    close_errors = _close_directory_handle(handle)
    if operation_error is not None:
        if close_errors:
            raise PublishError(
                f"{operation_error}; empty-directory cleanup descriptor "
                f"closure also reported at {quarantine_path}: "
                + "; ".join(close_errors),
                preserve_transaction=True,
            ) from operation_error
        if (
            isinstance(operation_error, PublishError)
            and operation_error.preserve_transaction
        ):
            raise operation_error
        raise PublishError(
            "Could not move an empty transaction directory into private "
            f"cleanup quarantine; preserving {display_path}: {operation_error}",
            preserve_transaction=True,
        ) from operation_error
    if close_errors:
        raise PublishError(
            f"Could not close privately quarantined empty directory "
            f"{quarantine_path}: " + "; ".join(close_errors),
            preserve_transaction=True,
        )

    observed = os.stat(
        quarantine_name,
        dir_fd=quarantine_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != expected_device
        or observed.st_ino != expected_inode
    ):
        raise PublishError(
            "Private cleanup directory changed before removal; preserving "
            f"{quarantine_path}",
            preserve_transaction=True,
        )
    # As with file unlink, POSIX has no inode-conditional rmdir. The directory
    # is empty and now has a random name inside a fresh 0700 quarantine; a
    # remaining substitution window requires a deliberate same-user race.
    try:
        os.rmdir(quarantine_name, dir_fd=quarantine_descriptor)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise PublishError(
                "Privately quarantined cleanup directory gained unrecognized "
                f"content; preserving {quarantine_path}",
                preserve_transaction=True,
            ) from error
        raise
    _fsync_directory_descriptor(
        quarantine_descriptor,
        cleanup_quarantine.display_path,
        preserve_transaction=True,
    )


def _remove_fresh_private_empty_directory_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    expected_device: int,
    expected_inode: int,
) -> None:
    """Remove a newly created 0700 cleanup quarantine after final verification."""

    handle = _verify_owned_cleanup_directory(
        parent_descriptor,
        name,
        expected_device,
        expected_inode,
        display_path,
    )
    try:
        if os.listdir(_require_directory_descriptor(handle)):
            raise PublishError(
                "Private cleanup quarantine retained recovery content; "
                f"preserving {display_path}",
                preserve_transaction=True,
            )
    finally:
        close_errors = _close_directory_handle(handle)
        if close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                f"Could not close private cleanup quarantine {display_path}: "
                + "; ".join(close_errors),
                preserve_transaction=True,
            )
    observed = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != expected_device
        or observed.st_ino != expected_inode
    ):
        raise PublishError(
            "Private cleanup quarantine changed before removal; preserving "
            f"{display_path}",
            preserve_transaction=True,
        )
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise PublishError(
                "Private cleanup quarantine gained recovery content; "
                f"preserving {display_path}",
                preserve_transaction=True,
            ) from error
        raise
    _fsync_directory_descriptor(
        parent_descriptor,
        display_path.parent,
        preserve_transaction=True,
    )


def _clear_verified_success_workspace_entries(
    transaction_root: DirectoryHandle,
    cleanup_plan: TransactionCleanupPlan,
    workspace_cleanup: DirectoryHandle,
) -> None:
    """Remove only entries recorded by the successful transaction."""

    transaction_descriptor = _require_directory_descriptor(transaction_root)
    _remove_verified_empty_directory_at(
        transaction_descriptor,
        "stage",
        transaction_root.display_path / "stage",
        cleanup_plan.stage_device,
        cleanup_plan.stage_inode,
        workspace_cleanup,
    )

    backup_path = transaction_root.display_path / "backups"
    backup_handle = _verify_owned_cleanup_directory(
        transaction_descriptor,
        "backups",
        cleanup_plan.backup_device,
        cleanup_plan.backup_inode,
        backup_path,
    )
    backup_descriptor = _require_directory_descriptor(backup_handle)
    backup_error: BaseException | None = None
    cleanup_quarantine: DirectoryHandle | None = None
    try:
        expected_backups = dict(cleanup_plan.backup_states)
        if tuple(sorted(os.listdir(backup_descriptor))) != tuple(
            sorted(expected_backups)
        ):
            raise PublishError(
                "Transaction cleanup found unrecognized backup entries in "
                f"{backup_path}; preserving them for review",
                preserve_transaction=True,
            )
        if expected_backups:
            cleanup_name = (
                f"{CLEANUP_QUARANTINE_PREFIX}{secrets.token_hex(16)}"
            )
            cleanup_path = backup_path / cleanup_name
            cleanup_quarantine = _create_directory_handle_at(
                backup_descriptor,
                cleanup_name,
                cleanup_path,
            )
            cleanup_device = cleanup_quarantine.device
            cleanup_inode = cleanup_quarantine.inode
            for folder, expected_state in expected_backups.items():
                _remove_verified_directory_tree_at(
                    backup_descriptor,
                    folder,
                    backup_path / folder,
                    expected_state,
                    cleanup_quarantine,
                )
            if os.listdir(
                _require_directory_descriptor(cleanup_quarantine)
            ):
                raise PublishError(
                    "Private cleanup quarantine retained recovery entries; "
                    f"preserving {cleanup_path}",
                    preserve_transaction=True,
                )
            cleanup_close_errors = _close_directory_handle(
                cleanup_quarantine
            )
            if cleanup_close_errors:
                raise PublishError(
                    "Could not close private cleanup quarantine before "
                    f"removal: {'; '.join(cleanup_close_errors)}",
                    preserve_transaction=True,
                )
            observed_cleanup = os.stat(
                cleanup_name,
                dir_fd=backup_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed_cleanup.st_mode)
                or observed_cleanup.st_dev != cleanup_device
                or observed_cleanup.st_ino != cleanup_inode
            ):
                raise PublishError(
                    "Private cleanup quarantine changed before removal; "
                    f"preserving {cleanup_path}",
                    preserve_transaction=True,
                )
            try:
                os.rmdir(cleanup_name, dir_fd=backup_descriptor)
            except OSError as error:
                if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise PublishError(
                        "Private cleanup quarantine gained recovery entries; "
                        f"preserving {cleanup_path}",
                        preserve_transaction=True,
                    ) from error
                raise
            _fsync_directory_descriptor(
                backup_descriptor,
                backup_path,
                preserve_transaction=True,
            )
        if os.listdir(backup_descriptor):
            raise PublishError(
                "Transaction backup directory changed during cleanup; "
                f"preserving {backup_path}",
                preserve_transaction=True,
            )
        _fsync_directory_descriptor(
            backup_descriptor,
            backup_path,
            preserve_transaction=True,
        )
    except BaseException as error:
        backup_error = error
    cleanup_close_errors = _close_directory_handle(cleanup_quarantine)
    backup_close_errors = (
        cleanup_close_errors + _close_directory_handle(backup_handle)
    )
    if backup_error is not None:
        if backup_close_errors:
            raise PublishError(
                f"{backup_error}; backup cleanup descriptor closure also "
                f"reported: {'; '.join(backup_close_errors)}",
                preserve_transaction=True,
            ) from backup_error
        raise backup_error
    if backup_close_errors:
        raise PublishError(
            "Could not close verified backup directory: "
            + "; ".join(backup_close_errors),
            preserve_transaction=True,
        )
    _remove_verified_empty_directory_at(
        transaction_descriptor,
        "backups",
        backup_path,
        cleanup_plan.backup_device,
        cleanup_plan.backup_inode,
        workspace_cleanup,
    )

    _remove_verified_empty_directory_at(
        transaction_descriptor,
        "quarantine",
        transaction_root.display_path / "quarantine",
        cleanup_plan.quarantine_device,
        cleanup_plan.quarantine_inode,
        workspace_cleanup,
    )
    _fsync_directory_descriptor(
        transaction_descriptor,
        transaction_root.display_path,
        preserve_transaction=True,
    )


def _clear_verified_success_workspace(
    transaction_root: DirectoryHandle,
    cleanup_plan: TransactionCleanupPlan,
) -> None:
    """Remove committed transaction entries through a fresh private quarantine."""

    transaction_descriptor = _require_directory_descriptor(transaction_root)
    cleanup_name = (
        f"{CLEANUP_QUARANTINE_PREFIX}{secrets.token_hex(16)}"
    )
    cleanup_path = transaction_root.display_path / cleanup_name
    workspace_cleanup = _create_directory_handle_at(
        transaction_descriptor,
        cleanup_name,
        cleanup_path,
    )
    cleanup_device = workspace_cleanup.device
    cleanup_inode = workspace_cleanup.inode
    operation_error: BaseException | None = None
    try:
        _clear_verified_success_workspace_entries(
            transaction_root,
            cleanup_plan,
            workspace_cleanup,
        )
    except BaseException as error:
        operation_error = error

    close_errors = _close_directory_handle(workspace_cleanup)
    if operation_error is not None:
        if close_errors:
            raise PublishError(
                f"{operation_error}; workspace cleanup quarantine closure also "
                f"reported: {'; '.join(close_errors)}",
                preserve_transaction=True,
            ) from operation_error
        raise operation_error
    if close_errors:
        raise PublishError(
            "Could not close workspace cleanup quarantine: "
            + "; ".join(close_errors),
            preserve_transaction=True,
        )
    _remove_fresh_private_empty_directory_at(
        transaction_descriptor,
        cleanup_name,
        cleanup_path,
        cleanup_device,
        cleanup_inode,
    )


def _rollback(
    states: list[ReplacementState],
    destination_root: DestinationRootIdentity,
    transaction_lock: TransactionLock,
    backup_root: DirectoryHandle,
    quarantine_root: DirectoryHandle,
) -> list[str]:
    errors: list[str] = []
    root_descriptor = transaction_lock.file_descriptor
    if root_descriptor is None:
        return ["publication destination lock descriptor is missing"]
    try:
        backup_descriptor = _require_directory_descriptor(backup_root)
        quarantine_descriptor = _require_directory_descriptor(quarantine_root)
    except BaseException as error:
        return [f"rollback setup could not anchor recovery directories: {error}"]
    for state in reversed(states):
        folder = state.destination.name
        try:
            _assert_mutation_authorized(destination_root, transaction_lock)
            observed_backup = _capture_destination_state_at(
                backup_descriptor,
                folder,
                state.backup,
            )
            observed_destination = _capture_destination_state_at(
                root_descriptor,
                folder,
                state.destination,
            )
            observed_quarantine = _capture_destination_state_at(
                quarantine_descriptor,
                folder,
                quarantine_root.display_path / folder,
            )
            conflicts: list[str] = []
            expected_backup = (
                state.backup_state_after_move
                if state.backup_state_after_move is not None
                else (
                    state.original_state
                    if state.original_existed
                    else DestinationState(kind="absent")
                )
            )
            if observed_backup not in {
                DestinationState(kind="absent"),
                expected_backup,
            }:
                raise PublishError(
                    "Backup changed before rollback; preserving recovery "
                    f"bytes without restoring them: {state.backup}",
                    preserve_transaction=True,
                )
            if observed_quarantine != DestinationState(kind="absent"):
                conflicts.append(
                    "rollback quarantine contains unexpected bytes at "
                    f"{quarantine_root.display_path / folder}"
                )

            expected_installed = (
                state.installed_state
                if state.installed_state is not None
                else state.staged_state
            )
            if (
                expected_installed is not None
                and _same_directory_identity(
                    observed_destination,
                    expected_installed,
                )
            ):
                if conflicts:
                    raise PublishError(
                        "; ".join(conflicts),
                        preserve_transaction=True,
                    )
                _assert_mutation_authorized(destination_root, transaction_lock)
                _atomic_rename_no_replace(
                    folder,
                    folder,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=quarantine_descriptor,
                )
                _fsync_directory_descriptor(
                    quarantine_descriptor,
                    quarantine_root.display_path,
                    preserve_transaction=True,
                )
                _fsync_directory_descriptor(
                    root_descriptor,
                    destination_root.path,
                    preserve_transaction=True,
                )
                quarantined_state = _capture_destination_state_at(
                    quarantine_descriptor,
                    folder,
                    quarantine_root.display_path / folder,
                )
                if quarantined_state != expected_installed:
                    if _same_directory_identity(
                        quarantined_state,
                        expected_installed,
                    ):
                        state.installed_state = quarantined_state
                    else:
                        destination_state = _capture_destination_state_at(
                            root_descriptor,
                            folder,
                            state.destination,
                        )
                        if destination_state == DestinationState(kind="absent"):
                            _atomic_rename_no_replace(
                                folder,
                                folder,
                                src_dir_fd=quarantine_descriptor,
                                dst_dir_fd=root_descriptor,
                            )
                            _fsync_directory_descriptor(
                                root_descriptor,
                                destination_root.path,
                                preserve_transaction=True,
                            )
                            _fsync_directory_descriptor(
                                quarantine_descriptor,
                                quarantine_root.display_path,
                                preserve_transaction=True,
                            )
                        raise PublishError(
                            "Installed skill identity changed during rollback "
                            "quarantine; restored it without deleting bytes: "
                            f"{state.destination}",
                            preserve_transaction=True,
                        )
                observed_destination = _capture_destination_state_at(
                    root_descriptor,
                    folder,
                    state.destination,
                )
                state.installed = False
            elif observed_destination == expected_backup:
                if observed_backup != DestinationState(kind="absent"):
                    raise PublishError(
                        "Original skill appears in both destination and backup; "
                        f"preserving transaction for review: {state.destination}",
                        preserve_transaction=True,
                    )
            elif observed_destination != DestinationState(kind="absent"):
                raise PublishError(
                    "Skill destination changed before rollback; refusing to "
                    f"remove concurrent state: {state.destination}",
                    preserve_transaction=True,
                )

            if state.original_existed:
                if observed_backup == expected_backup:
                    observed_destination = _capture_destination_state_at(
                        root_descriptor,
                        folder,
                        state.destination,
                    )
                    if observed_destination != DestinationState(kind="absent"):
                        raise PublishError(
                            "Skill destination is no longer absent before "
                            f"rollback restore: {state.destination}",
                            preserve_transaction=True,
                        )
                    _assert_mutation_authorized(
                        destination_root,
                        transaction_lock,
                    )
                    _atomic_rename_no_replace(
                        folder,
                        folder,
                        src_dir_fd=backup_descriptor,
                        dst_dir_fd=root_descriptor,
                    )
                    _fsync_directory_descriptor(
                        root_descriptor,
                        destination_root.path,
                        preserve_transaction=True,
                    )
                    _fsync_directory_descriptor(
                        backup_descriptor,
                        backup_root.display_path,
                        preserve_transaction=True,
                    )
                    restored_state = _capture_destination_state_at(
                        root_descriptor,
                        folder,
                        state.destination,
                    )
                    if restored_state != expected_backup:
                        raise PublishError(
                            "Restored skill changed during rollback; preserved "
                            f"the observed destination: {state.destination}",
                            preserve_transaction=True,
                        )
                    state.backup_created = False
                elif (
                    _capture_destination_state_at(
                        root_descriptor,
                        folder,
                        state.destination,
                    )
                    != expected_backup
                ):
                    raise PublishError(
                        "Original skill is absent from both destination and "
                        f"verified backup: {state.destination}",
                        preserve_transaction=True,
                    )
            elif (
                _capture_destination_state_at(
                    root_descriptor,
                    folder,
                    state.destination,
                )
                != DestinationState(kind="absent")
            ):
                raise PublishError(
                    "A destination that was originally absent could not be "
                    f"cleared during rollback: {state.destination}",
                    preserve_transaction=True,
                )

            if conflicts:
                raise PublishError(
                    "; ".join(conflicts),
                    preserve_transaction=True,
                )
            state.backup_attempted = False
            state.install_attempted = False
        except BaseException as error:
            errors.append(f"{state.destination}: {error}")
    return errors


def _swap_all(
    stage_root: DirectoryHandle,
    dest_root: Path,
    transaction_root: DirectoryHandle,
    expected_skill_digests: dict[str, str],
    expected_destination_states: dict[str, DestinationState],
    transaction_lock: TransactionLock,
    destination_root: DestinationRootIdentity,
) -> TransactionCleanupPlan:
    root_descriptor = transaction_lock.file_descriptor
    if root_descriptor is None:
        raise PublishError("Publication destination lock descriptor is missing")
    stage_descriptor = _require_directory_descriptor(stage_root)
    transaction_descriptor = _require_directory_descriptor(transaction_root)
    _assert_mutation_authorized(destination_root, transaction_lock)
    observed_destination_states = preflight_destinations(
        dest_root,
        root_descriptor,
    )
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
    backup_root = _create_directory_handle_at(
        transaction_descriptor,
        "backups",
        transaction_root.display_path / "backups",
    )
    quarantine_root: DirectoryHandle | None = None
    states: list[ReplacementState] = []
    committed = False
    cleanup_plan: TransactionCleanupPlan | None = None
    try:
        quarantine_root = _create_directory_handle_at(
            transaction_descriptor,
            "quarantine",
            transaction_root.display_path / "quarantine",
        )
        backup_descriptor = _require_directory_descriptor(backup_root)
        quarantine_descriptor = _require_directory_descriptor(quarantine_root)
        for folder in SKILL_FOLDERS:
            destination = dest_root / folder
            original_state = expected_destination_states[folder]
            _assert_mutation_authorized(destination_root, transaction_lock)
            observed_original = _capture_destination_state_at(
                root_descriptor,
                folder,
                destination,
            )
            if observed_original != original_state:
                raise PublishError(
                    "Skill destination changed immediately before backup; "
                    f"refusing to replace it: {destination}",
                )
            state = ReplacementState(
                destination=destination,
                backup=backup_root.display_path / folder,
                staged=stage_root.display_path / folder,
                original_existed=original_state.kind != "absent",
                original_state=original_state,
            )
            states.append(state)
            if state.original_existed:
                _assert_mutation_authorized(destination_root, transaction_lock)
                state.backup_attempted = True
                _atomic_rename_no_replace(
                    folder,
                    folder,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=backup_descriptor,
                )
                _fsync_directory_descriptor(
                    backup_descriptor,
                    backup_root.display_path,
                    preserve_transaction=True,
                )
                _fsync_directory_descriptor(
                    root_descriptor,
                    destination_root.path,
                    preserve_transaction=True,
                )
                state.backup_created = True
                observed_backup = _capture_destination_state_at(
                    backup_descriptor,
                    folder,
                    state.backup,
                )
                if (
                    observed_backup.kind != "directory"
                    or observed_backup.device != original_state.device
                    or observed_backup.inode != original_state.inode
                ):
                    raise PublishError(
                        "Skill destination identity changed during backup; "
                        "publication will preserve the transaction without "
                        "treating the observed backup as the original: "
                        f"{destination}",
                        preserve_transaction=True,
                    )
                state.backup_state_after_move = observed_backup
                if observed_backup != original_state:
                    raise PublishError(
                        "Skill destination changed during backup; publication "
                        f"will restore the moved bytes: {destination}",
                    )
            _assert_mutation_authorized(destination_root, transaction_lock)
            observed_destination = _capture_destination_state_at(
                root_descriptor,
                folder,
                destination,
            )
            if observed_destination != DestinationState(kind="absent"):
                raise PublishError(
                    "Skill destination is not absent immediately before install; "
                    f"refusing to overwrite it: {destination}",
                    preserve_transaction=True,
                )
            state.staged_state = _capture_destination_state_at(
                stage_descriptor,
                folder,
                state.staged,
            )
            if state.staged_state.kind != "directory":
                raise PublishError(
                    f"Staged skill is not a directory: {state.staged}",
                    preserve_transaction=state.backup_attempted,
                )
            quarantine_state = _capture_destination_state_at(
                quarantine_descriptor,
                folder,
                quarantine_root.display_path / folder,
            )
            if quarantine_state != DestinationState(kind="absent"):
                raise PublishError(
                    "Publication quarantine is not empty: "
                    f"{quarantine_root.display_path / folder}",
                    preserve_transaction=state.backup_attempted,
                )
            _assert_mutation_authorized(destination_root, transaction_lock)
            state.install_attempted = True
            _atomic_rename_no_replace(
                folder,
                folder,
                src_dir_fd=stage_descriptor,
                dst_dir_fd=root_descriptor,
            )
            state.installed = True
            observed_installed = _capture_destination_state_at(
                root_descriptor,
                folder,
                destination,
            )
            state.installed_state = observed_installed
            _fsync_directory_descriptor(
                root_descriptor,
                destination_root.path,
                preserve_transaction=True,
            )
            _fsync_directory_descriptor(
                stage_descriptor,
                stage_root.display_path,
                preserve_transaction=True,
            )
            if observed_installed != state.staged_state:
                raise PublishError(
                    "Installed skill changed during atomic placement: "
                    f"{destination}",
                    preserve_transaction=True,
                )
        _verify_installed_skill_set(
            root_descriptor,
            dest_root,
            states,
            expected_skill_digests,
        )
        mismatched_backups = [
            (folder, state)
            for folder, state in zip(SKILL_FOLDERS, states, strict=True)
            if (
                state.backup_created
                and _capture_destination_state_at(
                    backup_descriptor,
                    folder,
                    state.backup,
                )
                != state.original_state
            )
        ]
        if mismatched_backups:
            raise OSError(
                "skill backups changed before commit: "
                + ", ".join(folder for folder, _ in mismatched_backups)
            )
        cleanup_plan = TransactionCleanupPlan(
            stage_device=stage_root.device,
            stage_inode=stage_root.inode,
            backup_device=backup_root.device,
            backup_inode=backup_root.inode,
            quarantine_device=quarantine_root.device,
            quarantine_inode=quarantine_root.inode,
            backup_states=tuple(
                (state.destination.name, state.original_state)
                for state in states
                if state.original_existed
            ),
        )
        committed = True
    except BaseException as error:
        preserve_conflict = (
            isinstance(error, PublishError)
            and error.preserve_transaction
        )
        rollback_errors = (
            _rollback(
                states,
                destination_root,
                transaction_lock,
                backup_root,
                quarantine_root,
            )
            if quarantine_root is not None
            else (
                ["rollback quarantine was not available"]
                if states
                else []
            )
        )
        if rollback_errors:
            details = (
                "; ".join(rollback_errors)
                if rollback_errors
                else str(error)
            )
            recovery_location = _describe_transaction_recovery(
                transaction_root,
                destination_root,
                transaction_lock,
            )
            raise PublishError(
                "Publication failed and rollback was incomplete. Recovery files "
                f"remain in {recovery_location}: {details}",
                preserve_transaction=True,
            ) from error
        if preserve_conflict:
            recovery_location = _describe_transaction_recovery(
                transaction_root,
                destination_root,
                transaction_lock,
            )
            raise PublishError(
                "Publication failed and destinations were restored, but "
                "unrecognized transaction state remains for review in "
                f"{recovery_location}: {error}",
                preserve_transaction=True,
            ) from error
        raise PublishError(
            "Publication failed and all destinations were rolled back. "
            "The transaction was preserved for review instead of applying "
            f"manifest-less cleanup: {error}",
            preserve_transaction=True,
        ) from error
    finally:
        quarantine_close_errors = _close_directory_handle(quarantine_root)
        backup_close_errors = _close_directory_handle(backup_root)
        close_errors = quarantine_close_errors + backup_close_errors
        if close_errors and committed:
            print(
                "WARNING: publication committed and verified, but transaction "
                "directory descriptor closure was indeterminate: "
                + "; ".join(close_errors),
                file=sys.stderr,
            )
        elif close_errors and sys.exc_info()[0] is None:
            raise PublishError(
                "Could not close publication transaction directories: "
                + "; ".join(close_errors),
                preserve_transaction=True,
            )
        elif close_errors:
            print(
                "WARNING: publication rollback was already in progress and "
                "transaction directory descriptor closure also reported: "
                + "; ".join(close_errors),
                file=sys.stderr,
            )
    if cleanup_plan is None:
        raise PublishError(
            "Publication completed without a verified transaction cleanup plan",
            preserve_transaction=True,
        )
    return cleanup_plan


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

    # Authorize every existing destination component before mkdir or lock-file
    # creation can mutate the requested publication path.
    _capture_authorized_destination_path(
        destination_root,
        allow_missing=True,
    )

    transaction_lock: TransactionLock | None = None
    transaction_root: DirectoryHandle | None = None
    stage_root: DirectoryHandle | None = None
    destination_identity: DestinationRootIdentity | None = None
    cleanup_plan: TransactionCleanupPlan | None = None
    try:
        _ensure_destination_root(destination_root)
        destination_identity = _capture_destination_root_identity(
            destination_root,
            destination_root,
        )
        _assert_destination_root(destination_identity)
        transaction_lock = _acquire_transaction_lock(destination_identity)
        _assert_no_recovery_transactions(
            destination_identity,
            transaction_lock,
        )
        root_descriptor = transaction_lock.file_descriptor
        if root_descriptor is None:
            raise PublishError(
                "Publication destination lock descriptor is missing"
            )
        destination_states = preflight_destinations(
            destination_root,
            root_descriptor,
        )
        _assert_mutation_authorized(destination_identity, transaction_lock)
        transaction_root = _create_transaction_workspace(
            destination_identity,
            transaction_lock,
        )
        _assert_mutation_authorized(destination_identity, transaction_lock)
        stage_root = _stage_all(source_root, transaction_root)
        _preflight_skill_tree_handle(stage_root)

        # Bind publication to the copied tree, not merely to the source tree
        # that existed before staging.
        staged_records = _walk_directory_handle(
            stage_root,
            require_canonical_modes=True,
            require_canonical_root=False,
        )
        if _skill_tree_digest_records(
            staged_records
        ) != receipt.get("tree_sha256"):
            raise PublishError(
                "Staged skill tree differs from the validated generated tree."
            )
        try:
            staged_receipt = verify_validation_receipt(
                build_root=source_root,
                receipt_path=receipt_path,
            )
        except ValueError as error:
            raise PublishError(str(error)) from error
        require_fresh_receipt(staged_receipt, now=now)
        if any(
            staged_receipt.get(field) != receipt.get(field)
            for field in ("source_sha256", "tree_sha256")
        ):
            raise PublishError(
                "Validation receipt source/tree identity changed during "
                "publication staging. Run make validate and publish again."
            )

        expected_skill_digests = {
            folder: _skill_digest_from_tree_records(
                staged_records,
                folder,
            )
            for folder in SKILL_FOLDERS
        }
        cleanup_plan = _swap_all(
            stage_root,
            destination_root,
            transaction_root,
            expected_skill_digests,
            destination_states,
            transaction_lock,
            destination_identity,
        )
    except BaseException as error:
        recovery_location = (
            _describe_transaction_recovery(
                transaction_root,
                destination_identity,
                transaction_lock,
            )
            if (
                transaction_root is not None
                and destination_identity is not None
                and transaction_lock is not None
            )
            else None
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
                recovery_suffix = (
                    f"; publication recovery state remains in {recovery_location}"
                    if recovery_location is not None
                    else ""
                )
                raise PublishError(
                    f"{error}; transaction-lock cleanup also "
                    f"failed{recovery_suffix}: {cleanup_error}",
                    preserve_transaction=True,
                ) from error
        if isinstance(error, PublishError):
            if transaction_root is not None and not error.preserve_transaction:
                raise PublishError(
                    f"{error} Transaction recovery state was preserved for "
                    f"review in {recovery_location}.",
                    preserve_transaction=True,
                ) from error
            raise
        if isinstance(error, OSError):
            recovery_suffix = (
                " Transaction recovery state was preserved for review in "
                f"{recovery_location}."
                if recovery_location is not None
                else ""
            )
            raise PublishError(
                f"Publication staging failed: {error}.{recovery_suffix}",
                preserve_transaction=transaction_root is not None,
            ) from error
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
                stage_close_errors = _close_directory_handle(stage_root)
                if stage_close_errors:
                    raise PublishError(
                        "Could not close staged tree before cleanup: "
                        + "; ".join(stage_close_errors)
                    )
                _remove_transaction_workspace(
                    transaction_root,
                    destination_identity,
                    transaction_lock,
                    cleanup_plan,
                )
            except DestinationAuthorizationError as cleanup_error:
                recovery_location = _describe_transaction_recovery(
                    transaction_root,
                    destination_identity,
                    transaction_lock,
                )
                raise PublishError(
                    "Publication committed to the anchored destination, but "
                    "the requested destination path changed before recovery "
                    "cleanup. Recovery state was preserved in "
                    f"{recovery_location}: {cleanup_error}",
                    preserve_transaction=True,
                ) from cleanup_error
            except (OSError, PublishError) as cleanup_error:
                recovery_location = _describe_transaction_recovery(
                    transaction_root,
                    destination_identity,
                    transaction_lock,
                )
                if (
                    isinstance(cleanup_error, PublishError)
                    and cleanup_error.preserve_transaction
                ):
                    raise PublishError(
                        "Publication committed and verified, but transaction "
                        "cleanup found unrecognized state. Recovery files were "
                        f"preserved in {recovery_location}: "
                        f"{cleanup_error}",
                        preserve_transaction=True,
                    ) from cleanup_error
                print(
                    "WARNING: publication committed and verified, but obsolete "
                    "transaction backup cleanup failed; recovery files remain at "
                    f"{recovery_location}: {cleanup_error}",
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
            except DestinationAuthorizationError as cleanup_error:
                raise PublishError(
                    "Publication committed to the anchored destination, but "
                    "the requested destination path changed before lock "
                    f"release: {cleanup_error}",
                    preserve_transaction=True,
                ) from cleanup_error
            except (OSError, PublishError) as cleanup_error:
                print(
                    "WARNING: publication committed and verified, but the "
                    "kernel transaction-lock release state is indeterminate; "
                    "no unsafe descriptor retry will be attempted. "
                    f"Sentinel: {transaction_lock.path}: {cleanup_error}",
                    file=sys.stderr,
                )
    finally:
        finalizer_errors: list[str] = []
        finalizer_errors.extend(_close_directory_handle(stage_root))
        finalizer_errors.extend(_close_directory_handle(transaction_root))
        if transaction_lock is not None:
            finalizer_errors.extend(_close_transaction_lock(transaction_lock))
        if finalizer_errors:
            print(
                "WARNING: publication descriptor finalization reported: "
                + "; ".join(finalizer_errors),
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
