#!/usr/bin/env python3
"""Scan tracked and unignored files for artifacts and high-confidence secrets."""

from __future__ import annotations

from pathlib import Path
import re

from libskillpack import REPO_ROOT, run_command


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


def reviewable_paths(repo_root: Path = REPO_ROOT) -> list[Path]:
    result = run_command(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo_root,
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "git ls-files failed")
    return sorted({Path(value) for value in result.stdout.split("\0") if value})


def scan_paths(repo_root: Path, relative_paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for relative in sorted(relative_paths):
        path = repo_root / relative
        forbidden_artifact = (
            path.name in FORBIDDEN_NAMES
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or (
                path.suffix.lower() == ".txt"
                and relative not in ALLOWED_TEXT_PATHS
            )
            or "__pycache__" in relative.parts
        )
        if forbidden_artifact:
            errors.append(f"{relative}: forbidden generated or third-party artifact")
        if not path.is_file():
            errors.append(f"{relative}: tracked path is missing")
            continue
        payload = path.read_bytes()
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(payload):
                errors.append(f"{relative}: possible {label}")
    return errors


def main() -> int:
    try:
        paths = reviewable_paths()
        errors = scan_paths(REPO_ROOT, paths)
    except Exception as error:
        print(f"ERROR: {error}")
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        "Repository secret/artifact scan passed "
        f"({len(paths)} tracked or unignored files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
