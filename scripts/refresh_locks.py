#!/usr/bin/env python3
"""Generate reviewable lock candidates without mutating checked-in locks/content."""

from __future__ import annotations

from pathlib import Path
import argparse
import plistlib
import shutil
import tempfile
import uuid

from libskillpack import (
    CONTENT_ROOT,
    LOCK_ROOT,
    RAW_ROOT,
    STATA_ADO_BASE,
    STATA_ROOT,
    UPSTREAM_REPO_DIR,
    detect_stata_binary,
    ensure_dir,
    find_help_files_exact,
    iter_content_entries,
    load_skill_config,
    read_text,
    read_yaml,
    relative_to_stata,
    run_command,
    run_stata_do,
    sha256_file,
    write_text,
    write_yaml,
)
from validate_skill_pack import (
    package_content_entries,
    parse_stata_track,
    sanitize_diagnostics,
)


CANDIDATE_ROOT = RAW_ROOT / "candidates"
EXPECTED_UPSTREAM_COMMIT = "33a7efc85e92cd30edc7b907f1deb9d7038397bc"
TRACK_METADATA_FILES = {"stata.trk", "backup.trk"}


def upstream_candidate() -> dict:
    result = run_command(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "rev-parse", "HEAD"],
        timeout_seconds=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "Could not read raw upstream commit")
    commit = result.stdout.strip()
    config = load_skill_config()
    files: dict[str, dict] = {}
    for _, _, entry in iter_content_entries(CONTENT_ROOT, config):
        for relative in entry.get("provenance", {}).get("upstream_files", []):
            source = UPSTREAM_REPO_DIR / relative
            if not source.is_file():
                raise RuntimeError(f"Missing declared upstream file: {relative}")
            files[relative] = {"sha256": sha256_file(source)}
    return {
        "schema_version": 1,
        "repository": {
            "url": "https://github.com/dylantmoore/stata-skill.git",
            "commit": commit,
            "expected_commit": EXPECTED_UPSTREAM_COMMIT,
        },
        "files": dict(sorted(files.items())),
    }


def stata_release_identity() -> dict:
    app = STATA_ROOT / "StataBE.app"
    info_path = app / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise RuntimeError(f"Missing Stata application metadata: {info_path}")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    executable = info.get("CFBundleExecutable")
    return {
        "edition": "BE",
        "bundle_identifier": info.get("CFBundleIdentifier"),
        "bundle_version": info.get("CFBundleShortVersionString"),
        "executable": executable,
        "platform": "macOS",
    }


def stata_help_candidate() -> dict:
    if not STATA_ADO_BASE.is_dir():
        raise RuntimeError(f"Missing local Stata help root: {STATA_ADO_BASE}")
    config = load_skill_config()
    selectors: dict[str, dict] = {}
    files: dict[str, dict] = {}
    errors: list[str] = []
    for skill_key, path, entry in iter_content_entries(CONTENT_ROOT, config):
        provenance = entry.get("provenance", {})
        declared = provenance.get("local_help_files", [])
        if not declared:
            continue
        exact = provenance.get("local_help_topics", [])
        globs = provenance.get("local_help_globs", [])
        resolved, missing = find_help_files_exact(exact, globs)
        resolved_relative = [relative_to_stata(source) for source in resolved]
        if missing:
            errors.append(f"{path}: missing selectors {missing}")
        if set(resolved_relative) != set(declared):
            errors.append(f"{path}: resolved files differ from curated declaration")
        key = f"{skill_key}/{entry['slug']}"
        selectors[key] = {
            "exact_stems": exact,
            "globs": globs,
            "files": declared,
        }
        for source in resolved:
            files[relative_to_stata(source)] = {"sha256": sha256_file(source)}
    if errors:
        raise RuntimeError("\n".join(errors))
    return {
        "schema_version": 1,
        "stata_release": stata_release_identity(),
        "selectors": dict(sorted(selectors.items())),
        "files": dict(sorted(files.items())),
    }


def plugin_sdk_candidate() -> dict:
    lock = read_yaml(LOCK_ROOT / "plugin-sdk.yaml")
    sources = lock.get("sources", [])
    refreshed: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="plugin-sdk-lock-") as temp_root:
        temp_path = Path(temp_root)
        from libskillpack import download_binary

        for source in sources:
            destination = temp_path / source["filename"]
            actual = download_binary(
                source["url"],
                destination,
                timeout_seconds=30,
            )
            refreshed.append(
                {
                    "filename": source["filename"],
                    "url": source["url"],
                    "sha256": actual,
                }
            )
    return {"schema_version": 1, "sources": refreshed}


def package_lock_candidate(stata_binary: Path) -> dict:
    packages: dict[str, dict] = {}
    installable = [
        entry for entry in package_content_entries() if entry.get("install_commands")
    ]
    with tempfile.TemporaryDirectory(prefix="stata-package-lock-") as temp_root:
        work_root = Path(temp_root)
        for index, entry in enumerate(installable, start=1):
            install_commands = entry["install_commands"]
            slug = entry["slug"]
            print(
                f"Refreshing package lock {index}/{len(installable)}: {slug}",
                flush=True,
            )
            run_dir = ensure_dir(work_root / slug)
            plus_dir = ensure_dir(run_dir / "plus")
            ensure_dir(plus_dir / "personal")
            marker = f"CODEX_LOCK_CANDIDATE::{slug.upper()}::{uuid.uuid4().hex}"
            do_file = run_dir / f"{slug}_{uuid.uuid4().hex}.do"
            do_text = "\n".join(
                [
                    "clear all",
                    "set more off",
                    f'sysdir set PLUS "{plus_dir.as_posix()}"',
                    f'sysdir set PERSONAL "{(plus_dir / "personal").as_posix()}"',
                    *install_commands,
                    f'display "{marker}"',
                    "exit, clear",
                ]
            ) + "\n"
            write_text(do_file, do_text)
            result, log_path = run_stata_do(
                stata_binary,
                do_file,
                run_dir,
                completion_marker=marker,
                timeout_seconds=180,
            )
            if result.returncode != 0:
                diagnostics = read_text(log_path) if log_path.exists() else result.stderr
                safe_diagnostics = sanitize_diagnostics(
                    diagnostics,
                    work_root=work_root,
                    max_chars=2000,
                )
                raise RuntimeError(f"{slug} install failed:\n{safe_diagnostics}")
            track_path = plus_dir / "stata.trk"
            if not track_path.is_file():
                raise RuntimeError(f"{slug}: install did not produce stata.trk")
            modules = parse_stata_track(read_text(track_path))
            distributions: list[dict] = []
            tracked_files: set[str] = set()
            for module in modules:
                file_hashes: dict[str, str] = {}
                for relative in sorted(module["files"]):
                    installed = plus_dir / relative
                    if not installed.is_file():
                        raise RuntimeError(f"{slug}: tracked file missing: {relative}")
                    file_hashes[relative] = sha256_file(installed)
                    tracked_files.add(relative)
                distributions.append(
                    {
                        "source": module["source"],
                        "descriptor": module["descriptor"],
                        "distribution_date": module["distribution_date"],
                        "files": file_hashes,
                    }
                )
            generated_files = {
                str(path.relative_to(plus_dir)): sha256_file(path)
                for path in sorted(plus_dir.rglob("*"))
                if path.is_file()
                and str(path.relative_to(plus_dir)) not in TRACK_METADATA_FILES
                and str(path.relative_to(plus_dir)) not in tracked_files
            }
            packages[slug] = {
                "distributions": distributions,
                "generated_files": generated_files,
            }
    return {"schema_version": 1, "packages": packages}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        action="append",
        choices=("upstream", "stata-help", "plugin-sdk", "packages", "all"),
        required=True,
    )
    args = parser.parse_args(argv)
    targets = {
        item
        for target in args.target
        for item in (
            ("upstream", "stata-help", "plugin-sdk", "packages")
            if target == "all"
            else (target,)
        )
    }
    builders = {
        "upstream": upstream_candidate,
        "stata-help": stata_help_candidate,
        "plugin-sdk": plugin_sdk_candidate,
    }
    try:
        for target in ("upstream", "stata-help", "plugin-sdk"):
            if target not in targets:
                continue
            payload = builders[target]()
            destination = CANDIDATE_ROOT / f"{target}-lock.yaml"
            write_yaml(destination, payload)
            print(f"Wrote review candidate {destination}")
        if "packages" in targets:
            stata_binary = detect_stata_binary()
            if stata_binary is None:
                raise RuntimeError("Could not locate Stata for package lock refresh")
            payload = package_lock_candidate(stata_binary)
            destination = CANDIDATE_ROOT / "packages-lock.yaml"
            write_yaml(destination, payload)
            print(f"Wrote review candidate {destination}")
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
