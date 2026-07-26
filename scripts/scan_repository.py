#!/usr/bin/env python3
"""Scan tracked and unignored files for artifacts and high-confidence secrets."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from libskillpack import REPO_ROOT
from release_state import SourcePathInventory, source_path_inventory


FORBIDDEN_NAMES = {
    ".DS_Store",
    ".env",
    "backup.trk",
    "stata.trk",
}
FORBIDDEN_SUFFIXES = {
    ".ado",
    ".doc",
    ".docx",
    ".dta",
    ".dll",
    ".dylib",
    ".gph",
    ".lic",
    ".log",
    ".mata",
    ".mlib",
    ".mo",
    ".o",
    ".obj",
    ".pkg",
    ".plugin",
    ".smcl",
    ".so",
    ".ster",
    ".sthlp",
    ".toc",
}
FORBIDDEN_ROOTS = (
    Path(".cache"),
    Path(".codex"),
    Path(".pytest_cache"),
    Path(".venv"),
    Path("build"),
    Path("raw"),
    Path("tests/tmp"),
)
ALLOWED_TEXT_PATHS = {Path("llms.txt")}
SECRET_PATTERNS = (
    (
        "private key",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "GitHub token",
        re.compile(
            rb"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|"
            rb"github_pat_[A-Za-z0-9_]{20,})\b"
        ),
    ),
    (
        "OpenAI API key",
        re.compile(rb"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "Slack token",
        re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    ),
)


def _existing_directory_identity(
    repo_root: Path,
    relative: Path,
) -> tuple[int, int] | None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current_fd: int | None = None
    try:
        current_fd = os.open(repo_root, directory_flags)
        for part in relative.parts:
            try:
                metadata = os.stat(
                    part,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(metadata.st_mode):
                return None
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if _metadata_fingerprint(opened) != _metadata_fingerprint(metadata):
                os.close(next_fd)
                raise ValueError(
                    f"repository directory changed while opening: {relative}"
                )
            os.close(current_fd)
            current_fd = next_fd
        observed = os.fstat(current_fd)
        return observed.st_dev, observed.st_ino
    except FileNotFoundError:
        return None
    finally:
        if current_fd is not None:
            os.close(current_fd)


def is_under_forbidden_root(relative: Path, repo_root: Path) -> bool:
    if any(
        relative == forbidden_root or forbidden_root in relative.parents
        for forbidden_root in FORBIDDEN_ROOTS
    ):
        return True
    for forbidden_root in FORBIDDEN_ROOTS:
        if len(relative.parts) < len(forbidden_root.parts):
            continue
        candidate = Path(*relative.parts[: len(forbidden_root.parts)])
        candidate_identity = _existing_directory_identity(repo_root, candidate)
        if candidate_identity is None:
            continue
        forbidden_identity = _existing_directory_identity(
            repo_root,
            forbidden_root,
        )
        if (
            forbidden_identity is not None
            and candidate_identity == forbidden_identity
        ):
            return True
    return False


def reviewable_paths(
    repo_root: Path = REPO_ROOT,
    *,
    inventory: SourcePathInventory | None = None,
) -> list[Path]:
    observed = inventory or source_path_inventory(repo_root)
    return sorted(
        {
            *observed.tracked,
            *observed.untracked,
            *observed.untracked_gate_inputs,
        },
        key=Path.as_posix,
    )


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_reviewable_file(repo_root: Path, relative: Path) -> bytes:
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("unsafe relative path")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current_fd = os.open(repo_root, directory_flags)
    file_fd: int | None = None
    try:
        for part in relative.parts[:-1]:
            metadata = os.stat(
                part,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("symbolic-link or non-directory ancestor")
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if _metadata_fingerprint(opened) != _metadata_fingerprint(metadata):
                os.close(next_fd)
                raise ValueError("ancestor changed while opening")
            os.close(current_fd)
            current_fd = next_fd

        metadata = os.stat(
            relative.name,
            dir_fd=current_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("symbolic links are not reviewable source files")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("special files are not reviewable source files")
        file_fd = os.open(
            relative.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current_fd,
        )
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _metadata_fingerprint(opened) != _metadata_fingerprint(metadata)
        ):
            raise ValueError("source file changed while opening")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        named_after = os.stat(
            relative.name,
            dir_fd=current_fd,
            follow_symlinks=False,
        )
        if (
            _metadata_fingerprint(after) != _metadata_fingerprint(opened)
            or _metadata_fingerprint(named_after)
            != _metadata_fingerprint(opened)
        ):
            raise ValueError("source file changed while reading")
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def scan_paths(repo_root: Path, relative_paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for relative in sorted(relative_paths):
        path = repo_root / relative
        forbidden_artifact = (
            path.name in FORBIDDEN_NAMES
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or is_under_forbidden_root(relative, repo_root)
            or (
                path.suffix.lower() == ".txt"
                and relative not in ALLOWED_TEXT_PATHS
            )
            or "__pycache__" in relative.parts
        )
        if forbidden_artifact:
            errors.append(f"{relative}: forbidden generated or third-party artifact")
        try:
            payload = _read_reviewable_file(repo_root, relative)
        except (OSError, ValueError) as error:
            errors.append(f"{relative}: unsafe or missing source file: {error}")
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(payload):
                errors.append(f"{relative}: possible {label}")
    return errors


def repository_scan_errors(repo_root: Path = REPO_ROOT) -> tuple[list[str], int]:
    inventory = source_path_inventory(repo_root)
    paths = reviewable_paths(repo_root, inventory=inventory)
    errors = scan_paths(repo_root, paths)
    errors.extend(
        f"{relative}: untracked validation input must be tracked"
        for relative in inventory.untracked_gate_inputs
    )
    return errors, len(paths)


def main() -> int:
    try:
        errors, path_count = repository_scan_errors(REPO_ROOT)
    except Exception as error:
        print(f"ERROR: {error}")
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        "Repository secret/artifact scan passed "
        f"({path_count} tracked or unignored files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
