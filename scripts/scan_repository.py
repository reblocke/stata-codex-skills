#!/usr/bin/env python3
"""Scan tracked and unignored files for artifacts and high-confidence secrets."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from runtime_guard import require_supported_runtime

require_supported_runtime()

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
# Repository review inputs larger than 4 MiB require explicit handling rather
# than silently expanding this public-repository secret scan.
MAX_REVIEWABLE_FILE_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
)
PRIVATE_KEY_OVERLAP_BYTES = 64
ASCII_WORD_BYTES = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


@dataclass(frozen=True)
class TokenPattern:
    label: str
    prefix: bytes
    allowed: frozenset[int]
    minimum: int
    maximum: int | None = None


@dataclass
class TokenCandidate:
    pattern: TokenPattern
    count: int = 0
    last_is_word: bool = False


ALNUM_UPPER = frozenset(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
ALNUM_UNDERSCORE = ASCII_WORD_BYTES
ALNUM_UNDERSCORE_HYPHEN = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
ALNUM_HYPHEN = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
)
TOKEN_PATTERNS = (
    TokenPattern("AWS access key", b"AKIA", ALNUM_UPPER, 16, 16),
    TokenPattern("AWS access key", b"ASIA", ALNUM_UPPER, 16, 16),
    TokenPattern("GitHub token", b"ghp_", ALNUM_UNDERSCORE, 30),
    TokenPattern("GitHub token", b"gho_", ALNUM_UNDERSCORE, 30),
    TokenPattern("GitHub token", b"ghu_", ALNUM_UNDERSCORE, 30),
    TokenPattern("GitHub token", b"ghs_", ALNUM_UNDERSCORE, 30),
    TokenPattern("GitHub token", b"ghr_", ALNUM_UNDERSCORE, 30),
    TokenPattern("GitHub token", b"github_pat_", ALNUM_UNDERSCORE, 20),
    TokenPattern("OpenAI API key", b"sk-", ALNUM_UNDERSCORE_HYPHEN, 20),
    TokenPattern(
        "OpenAI API key",
        b"sk-proj-",
        ALNUM_UNDERSCORE_HYPHEN,
        20,
    ),
    TokenPattern(
        "OpenAI API key",
        b"sk-svcacct-",
        ALNUM_UNDERSCORE_HYPHEN,
        20,
    ),
    TokenPattern("Slack token", b"xoxb-", ALNUM_HYPHEN, 20),
    TokenPattern("Slack token", b"xoxa-", ALNUM_HYPHEN, 20),
    TokenPattern("Slack token", b"xoxp-", ALNUM_HYPHEN, 20),
    TokenPattern("Slack token", b"xoxr-", ALNUM_HYPHEN, 20),
    TokenPattern("Slack token", b"xoxs-", ALNUM_HYPHEN, 20),
)
SECRET_LABEL_ORDER = (
    "private key",
    "AWS access key",
    "GitHub token",
    "OpenAI API key",
    "Slack token",
)
MAX_TOKEN_PREFIX_BYTES = max(len(pattern.prefix) for pattern in TOKEN_PATTERNS)


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


def _token_candidate_starts(
    prefix_tail: bytes,
    chunk: bytes,
    labels: set[str],
) -> dict[int, list[TokenCandidate]]:
    """Locate token prefixes ending in this chunk with real left context."""

    combined = prefix_tail + chunk
    chunk_offset = len(prefix_tail)
    starts: dict[int, list[TokenCandidate]] = {}
    for pattern in TOKEN_PATTERNS:
        if pattern.label in labels:
            continue
        search_from = 0
        while True:
            prefix_start = combined.find(pattern.prefix, search_from)
            if prefix_start < 0:
                break
            prefix_end = prefix_start + len(pattern.prefix)
            search_from = prefix_start + 1
            # Prefixes ending in the retained tail were already activated.
            if prefix_end <= chunk_offset:
                continue
            preceding = (
                combined[prefix_start - 1]
                if prefix_start > 0
                else None
            )
            if preceding is not None and preceding in ASCII_WORD_BYTES:
                continue
            local_suffix_start = prefix_end - chunk_offset
            if 0 <= local_suffix_start <= len(chunk):
                starts.setdefault(local_suffix_start, []).append(
                    TokenCandidate(pattern)
                )
    return starts


def _advance_token_candidates(
    candidates: list[TokenCandidate],
    value: int,
    labels: set[str],
) -> list[TokenCandidate]:
    """Advance compact token state by one byte without retaining token bytes."""

    current_is_word = value in ASCII_WORD_BYTES
    retained: list[TokenCandidate] = []
    for candidate in candidates:
        pattern = candidate.pattern
        if pattern.label in labels:
            continue
        if (
            pattern.maximum is not None
            and candidate.count == pattern.maximum
        ):
            if (
                candidate.count >= pattern.minimum
                and candidate.last_is_word != current_is_word
            ):
                labels.add(pattern.label)
            continue
        # A regex match may end before the current byte even when that byte
        # is also permitted by the token alphabet.
        if (
            candidate.count >= pattern.minimum
            and candidate.last_is_word != current_is_word
        ):
            labels.add(pattern.label)
            continue
        if value not in pattern.allowed:
            continue
        candidate.count += 1
        candidate.last_is_word = current_is_word
        retained.append(candidate)
    return retained


def _finish_token_candidates(
    candidates: list[TokenCandidate],
    labels: set[str],
) -> None:
    """Apply the trailing word-boundary rule at end of file."""

    for candidate in candidates:
        pattern = candidate.pattern
        if (
            pattern.label not in labels
            and candidate.count >= pattern.minimum
            and (
                pattern.maximum is None
                or candidate.count == pattern.maximum
            )
            and candidate.last_is_word
        ):
            labels.add(pattern.label)


def _read_reviewable_file(repo_root: Path, relative: Path) -> tuple[str, ...]:
    """Return secret labels found by a bounded, descriptor-safe file scan."""
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
        if opened.st_size > MAX_REVIEWABLE_FILE_BYTES:
            raise ValueError(
                "source file exceeds "
                f"{MAX_REVIEWABLE_FILE_BYTES}-byte scan limit"
            )

        labels: set[str] = set()
        private_key_overlap = b""
        token_prefix_tail = b""
        token_candidates: list[TokenCandidate] = []
        bytes_read = 0
        while True:
            remaining_with_probe = MAX_REVIEWABLE_FILE_BYTES - bytes_read + 1
            chunk = os.read(
                file_fd,
                min(READ_CHUNK_BYTES, remaining_with_probe),
            )
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > MAX_REVIEWABLE_FILE_BYTES:
                raise ValueError(
                    "source file exceeds "
                    f"{MAX_REVIEWABLE_FILE_BYTES}-byte scan limit"
                )
            private_key_window = private_key_overlap + chunk
            if (
                "private key" not in labels
                and PRIVATE_KEY_PATTERN.search(private_key_window)
            ):
                labels.add("private key")
            private_key_overlap = private_key_window[
                -PRIVATE_KEY_OVERLAP_BYTES:
            ]

            starts = _token_candidate_starts(
                token_prefix_tail,
                chunk,
                labels,
            )
            for index, value in enumerate(chunk):
                new_candidates = starts.get(index)
                if new_candidates:
                    token_candidates.extend(new_candidates)
                if token_candidates:
                    token_candidates = _advance_token_candidates(
                        token_candidates,
                        value,
                        labels,
                    )
            token_candidates.extend(starts.get(len(chunk), ()))
            token_prefix_tail = (token_prefix_tail + chunk)[
                -(MAX_TOKEN_PREFIX_BYTES + 1):
            ]

        _finish_token_candidates(token_candidates, labels)
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
        return tuple(label for label in SECRET_LABEL_ORDER if label in labels)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def scan_paths(repo_root: Path, relative_paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for relative in sorted(relative_paths):
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            errors.append(
                f"{relative}: unsafe or missing source file: "
                "unsafe relative path"
            )
            continue
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
            continue
        try:
            secret_labels = _read_reviewable_file(repo_root, relative)
        except (OSError, ValueError) as error:
            errors.append(f"{relative}: unsafe or missing source file: {error}")
            continue
        errors.extend(
            f"{relative}: possible {label}"
            for label in secret_labels
        )
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
