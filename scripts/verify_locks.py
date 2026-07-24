#!/usr/bin/env python3
"""Verify checked locks statically, with opt-in live hash checks."""

from __future__ import annotations

from pathlib import Path
import argparse
import plistlib
import tempfile

from libskillpack import (
    CONTENT_ROOT,
    LOCK_ROOT,
    STATA_ROOT,
    UPSTREAM_REPO_DIR,
    iter_content_entries,
    load_skill_config,
    read_yaml,
    run_command,
    sha256_file,
)
from lint_skill_pack import (
    lint_distribution_locks,
    lint_stata_help_lock,
    lint_upstream_lock,
)


def static_errors() -> list[str]:
    config = load_skill_config()
    entries = iter_content_entries(CONTENT_ROOT, config)
    return [
        *lint_upstream_lock(entries),
        *lint_stata_help_lock(entries),
        *lint_distribution_locks(entries),
    ]


def live_local_errors() -> list[str]:
    errors: list[str] = []
    upstream = read_yaml(LOCK_ROOT / "upstream.yaml")
    result = run_command(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "rev-parse", "HEAD"],
        timeout_seconds=15,
    )
    expected_commit = upstream.get("repository", {}).get("commit")
    if result.returncode != 0:
        errors.append("live upstream checkout is unavailable")
    elif result.stdout.strip() != expected_commit:
        errors.append(
            f"upstream commit drift: expected {expected_commit}, got {result.stdout.strip()}"
        )
    for relative, metadata in upstream.get("files", {}).items():
        path = UPSTREAM_REPO_DIR / relative
        if not path.is_file():
            errors.append(f"missing live upstream file {relative}")
        elif sha256_file(path) != metadata.get("sha256"):
            errors.append(f"upstream hash drift {relative}")

    stata_lock = read_yaml(LOCK_ROOT / "stata-help.yaml")
    release = stata_lock.get("stata_release", {})
    info_path = STATA_ROOT / "StataBE.app" / "Contents" / "Info.plist"
    if not info_path.is_file():
        errors.append("local Stata application metadata is unavailable")
    else:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        observed = {
            "edition": "BE",
            "bundle_identifier": info.get("CFBundleIdentifier"),
            "bundle_version": info.get("CFBundleShortVersionString"),
            "executable": info.get("CFBundleExecutable"),
            "platform": "macOS",
        }
        if observed != release:
            errors.append("local Stata release identity drift")
    for relative, metadata in stata_lock.get("files", {}).items():
        path = STATA_ROOT / relative
        if not path.is_file():
            errors.append(f"missing live Stata help file {relative}")
        elif sha256_file(path) != metadata.get("sha256"):
            errors.append(f"Stata help hash drift {relative}")
    return errors


def live_plugin_errors() -> list[str]:
    from libskillpack import download_binary

    errors: list[str] = []
    lock = read_yaml(LOCK_ROOT / "plugin-sdk.yaml")
    with tempfile.TemporaryDirectory(prefix="plugin-lock-verify-") as temp_root:
        root = Path(temp_root)
        for source in lock.get("sources", []):
            try:
                download_binary(
                    source["url"],
                    root / source["filename"],
                    timeout_seconds=30,
                    expected_sha256=source["sha256"],
                )
            except Exception as error:
                errors.append(f"{source.get('filename')}: {error}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also hash the current raw upstream checkout and local Stata help.",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Also redownload and verify plugin SDK sources; implies network access.",
    )
    args = parser.parse_args(argv)
    errors = static_errors()
    if args.live:
        errors.extend(live_local_errors())
    if args.network:
        errors.extend(live_plugin_errors())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Lock verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
