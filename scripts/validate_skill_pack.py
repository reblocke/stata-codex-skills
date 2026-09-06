#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import argparse
import fcntl
import os
import platform
import re
import stat
import tempfile
import uuid

from runtime_guard import require_supported_runtime

require_supported_runtime()

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    PACKAGE_LOCK_ROOT,
    REPO_ROOT,
    iter_content_entries,
    load_skill_config,
    detect_stata_binary,
    download_binary,
    ensure_dir,
    has_stata_error,
    is_safe_slug,
    read_text,
    read_yaml,
    run_command,
    run_stata_do,
    sha256_file,
    write_text,
)
from lint_skill_pack import lint_repo
from render_skills import (
    _capture_directory_descriptor_tree,
    _close_descriptor_list,
    _remove_owned_directory,
    _remove_verified_empty_directory_via_quarantine,
    _raise_after_descriptor_finalization,
    _retain_owned_directory,
)
from release_state import (
    VALIDATION_RECEIPT_PATH,
    ValidationReceiptTransaction,
    begin_validation_receipt_transaction,
    close_validation_receipt_transaction,
    retained_validation_receipt_locations,
    validation_state,
    write_validation_receipt,
)


SUITE_CHOICES = (
    "static",
    "core",
    "packages",
    "plugin-compile",
    "plugin-runtime",
    "default",
)
DEFAULT_SUITES = ("static", "core", "packages", "plugin-compile")
SENSITIVE_LOG_LINE = re.compile(
    r"(?i)\b("
    r"licensed?\s+to|license\s+(?:code|number|serial)|"
    r"serial\s+number|authorization\s+code"
    r")\b"
)
TRACK_METADATA_FILES = {"stata.trk", "backup.trk"}
VALIDATION_TRANSACTION_PREFIX = "stata-codex-validate-"
VALIDATION_WORKDIR_NAME = "work"


@dataclass(frozen=True)
class ValidationWorkspace:
    """Descriptor-retained validation workspace and its private transaction root."""

    temp_parent: Path
    transaction_root: Path
    work_root: Path
    temp_parent_descriptor: int | None
    transaction_descriptor: int
    work_descriptor: int
    transaction_identity: tuple[int, int]
    work_identity: tuple[int, int]
    transaction_mode: int
    work_mode: int
    owns_transaction_root: bool = True


def package_content_entries() -> list[dict]:
    config = load_skill_config()
    entries = [
        entry
        for skill_key, _, entry in iter_content_entries(CONTENT_ROOT, config)
        if skill_key == "packages" and entry.get("validation_mode") == "stata"
    ]
    return sorted(entries, key=lambda entry: (entry.get("order", 10**9), entry.get("slug", "")))


def plugin_sources() -> list[tuple[str, str, str]]:
    lock_path = LOCK_ROOT / "plugin-sdk.yaml"
    lock = read_yaml(lock_path)
    sources = lock.get("sources")
    if lock.get("schema_version") != 1 or not isinstance(sources, list) or not sources:
        raise ValueError(f"{lock_path}: invalid or empty plugin SDK lock")
    resolved: list[tuple[str, str, str]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError(f"{lock_path}: source must be a mapping")
        url = source.get("url")
        filename = source.get("filename")
        sha256 = source.get("sha256")
        if not all(isinstance(value, str) and value for value in (url, filename, sha256)):
            raise ValueError(f"{lock_path}: source requires url, filename, and sha256")
        resolved.append((url, filename, sha256))
    return resolved


def parse_stata_track(text: str) -> list[dict]:
    """Parse stable module metadata from an isolated PLUS/stata.trk file."""

    modules: list[dict] = []
    current: dict | None = None
    descriptions: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line:
            continue
        code, _, value = raw_line.partition(" ")
        value = value.strip()
        if code == "S":
            if current is not None:
                current["description"] = descriptions
                modules.append(current)
            current = {
                "source": value,
                "descriptor": "",
                "distribution_date": "",
                "files": [],
            }
            descriptions = []
        elif current is None:
            continue
        elif code == "N":
            current["descriptor"] = value
        elif code == "d":
            descriptions.append(value)
            match = re.fullmatch(r"Distribution-Date:\s*(\d{8})", value)
            if match:
                current["distribution_date"] = match.group(1)
        elif code == "f":
            current["files"].append(value)
        elif code == "e":
            current["description"] = descriptions
            modules.append(current)
            current = None
            descriptions = []
    if current is not None:
        current["description"] = descriptions
        modules.append(current)
    return modules


def verify_package_install_lock(slug: str, plus_dir: Path) -> tuple[bool, str]:
    """Verify isolated installed files against the checked package lock.

    Stata-managed ``stata.trk`` and ``backup.trk`` files are deliberately
    excluded because their administrative state is mutable. The lock records
    stable distribution metadata for review and hashes every package artifact
    expected under the isolated PLUS directory.
    """

    if not is_safe_slug(slug):
        return False, f"Unsafe package lock slug: {slug!r}"
    lock_path = PACKAGE_LOCK_ROOT / f"{slug}.yaml"
    lock = read_yaml(lock_path)
    if (
        not isinstance(lock, dict)
        or lock.get("schema_version") != 1
        or lock.get("slug") != slug
    ):
        return False, f"{lock_path}: invalid package lock"
    package = lock
    distributions = package.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        return False, f"{lock_path}: package {slug} has no locked distributions"
    errors: list[str] = []
    track_path = plus_dir / "stata.trk"
    if not track_path.is_file():
        return False, "Isolated installation did not create PLUS/stata.trk; refresh required"
    observed_modules = parse_stata_track(read_text(track_path))
    expected_modules = [
        {
            "source": distribution.get("source"),
            "descriptor": distribution.get("descriptor"),
            "distribution_date": distribution.get("distribution_date"),
            "files": sorted(distribution.get("files", {})),
        }
        for distribution in distributions
        if isinstance(distribution, dict)
    ]
    normalized_observed = [
        {
            "source": module.get("source"),
            "descriptor": module.get("descriptor"),
            "distribution_date": module.get("distribution_date"),
            # A few SSC descriptors repeat the same file line.  The lock stores
            # one hash per path, so compare the corresponding set of paths.
            "files": sorted(set(module.get("files", []))),
        }
        for module in observed_modules
    ]
    sort_key = lambda module: (
        str(module.get("descriptor")),
        str(module.get("source")),
    )
    if sorted(normalized_observed, key=sort_key) != sorted(
        expected_modules, key=sort_key
    ):
        errors.append(
            "stata.trk source/descriptor/distribution/file metadata drifted; "
            "refresh the package lock "
            f"(expected {sorted(expected_modules, key=sort_key)!r}; "
            f"observed {sorted(normalized_observed, key=sort_key)!r})"
        )

    locked_files: dict[str, str] = {}
    for distribution in distributions:
        if not isinstance(distribution, dict) or not isinstance(
            distribution.get("files"), dict
        ):
            errors.append("invalid distribution lock")
            continue
        for relative, expected_sha256 in distribution["files"].items():
            previous = locked_files.get(relative)
            if previous is not None and previous != expected_sha256:
                errors.append(f"conflicting hashes for {relative}")
            locked_files[relative] = expected_sha256
    generated_files = package.get("generated_files", {})
    if not isinstance(generated_files, dict):
        errors.append("generated_files lock must be a mapping")
        generated_files = {}
    for relative, expected_sha256 in generated_files.items():
        previous = locked_files.get(relative)
        if previous is not None and previous != expected_sha256:
            errors.append(f"conflicting hashes for {relative}")
        locked_files[relative] = expected_sha256

    observed_files = {
        str(path.relative_to(plus_dir))
        for path in plus_dir.rglob("*")
        if path.is_file()
        and str(path.relative_to(plus_dir)) not in TRACK_METADATA_FILES
    }
    expected_files = set(locked_files)
    unexpected = sorted(observed_files - expected_files)
    missing = sorted(expected_files - observed_files)
    if unexpected:
        errors.append(
            "unexpected installed files (refresh required): " + ", ".join(unexpected)
        )
    if missing:
        errors.append("missing locked files: " + ", ".join(missing))
    for relative, expected_sha256 in sorted(locked_files.items()):
        candidate = plus_dir / relative
        if not candidate.is_file():
            errors.append(f"missing locked file {relative}")
            continue
        actual_sha256 = sha256_file(candidate)
        if actual_sha256 != expected_sha256:
            errors.append(
                f"SHA-256 mismatch for {relative}: expected {expected_sha256}, got {actual_sha256}"
            )
    return not errors, "\n".join(errors)


def diagnostics_alias_check() -> tuple[bool, str]:
    config = load_skill_config()
    aliases = [
        alias
        for alias in config.get("route_aliases", [])
        if alias.get("from_skill") == "packages"
        and alias.get("from_route") == "packages/diagnostics.md"
    ]
    if len(aliases) != 1:
        return False, "Expected exactly one packages/diagnostics.md compatibility alias"
    alias = aliases[0]
    targets = [
        entry
        for skill_key, _, entry in iter_content_entries(CONTENT_ROOT, config)
        if skill_key == alias.get("to_skill") and entry.get("slug") == alias.get("to_slug")
    ]
    if len(targets) != 1:
        return False, "Diagnostics compatibility alias does not resolve to one canonical target"
    target = targets[0]
    if target.get("validation_mode") != "stata":
        return False, "Diagnostics compatibility target is not covered by Stata validation"
    return True, (
        "Historical package route resolves to "
        f"{config['skills'][alias['to_skill']]['name']}/"
        f"{config['skills'][alias['to_skill']]['route_dir']}/{target['slug']}.md"
    )


def completion_marker(label: str) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper()
    return f"CODEX_VALIDATION_COMPLETE::{safe_label}::{uuid.uuid4().hex}"


def has_exact_log_line(log_text: str, expected: str) -> bool:
    return any(line.strip() == expected for line in log_text.splitlines())


def combined_output(log_text: str, stdout: str | None, stderr: str | None) -> str:
    return "\n".join(part for part in (log_text, stdout or "", stderr or "") if part).strip()


def core_content_entries() -> list[dict]:
    config = load_skill_config()
    entries = [
        entry
        for skill_key, _, entry in iter_content_entries(CONTENT_ROOT, config)
        if skill_key == "core" and entry.get("validation_mode") == "stata"
    ]
    return sorted(entries, key=lambda entry: (entry.get("order", 10**9), entry.get("slug", "")))


def stata_entry_do_text(entry: dict, marker: str) -> str:
    return "\n".join(
        [
            "clear all",
            "set more off",
            "set seed 271828",
            entry["smoke_test"],
            f'display "PASS: {entry["slug"]}"',
            f'display "{marker}"',
            "exit, clear",
        ]
    ) + "\n"


def validate_core(
    stata_binary: Path,
    work_root: Path,
    core_slugs: list[str] | None = None,
) -> list[tuple[str, bool, str]]:
    content_entries = core_content_entries()
    if not content_entries:
        return [
            (
                "<content>",
                False,
                "Canonical core content has no Stata validation entries; refusing a vacuous pass.",
            )
        ]
    entries, unknown = selected_package_entries(content_entries, core_slugs)
    results: list[tuple[str, bool, str]] = [
        (slug, False, f"Unknown core slug: {slug}") for slug in unknown
    ]
    for entry in entries:
        slug = entry.get("slug", "<missing-slug>")
        try:
            run_dir = ensure_dir(work_root / "core" / slug)
            marker = completion_marker(f"core-{slug}")
            run_token = marker.rsplit("::", 1)[-1]
            do_file = run_dir / f"{slug}_smoke_{run_token}.do"
            write_text(do_file, stata_entry_do_text(entry, marker))
            result, log_path = run_stata_do(
                stata_binary,
                do_file,
                run_dir,
                completion_marker=marker,
                timeout_seconds=90,
            )
            log_text = read_text(log_path) if log_path.exists() else ""
            success = (
                result.returncode == 0
                and log_path.exists()
                and not has_stata_error(log_text)
                and has_exact_log_line(log_text, f"PASS: {slug}")
            )
            diagnostics = combined_output(log_text, result.stdout, result.stderr)
        except Exception as error:
            success = False
            diagnostics = f"{type(error).__name__}: {error}"
        results.append((slug, success, diagnostics))
    return results


def package_do_text(entry: dict, plus_dir: Path, marker: str) -> str:
    lines = [
        "clear all",
        "set more off",
        "set seed 271828",
        f'sysdir set PLUS "{plus_dir.as_posix()}"',
        f'sysdir set PERSONAL "{(plus_dir / "personal").as_posix()}"',
    ]
    lines.extend(entry.get("install_commands", []))
    for command in entry.get("preflight_commands", []):
        lines.append(
            re.sub(
                r"(?i)^\s*(?:(?:capture|quietly|noisily)\s+)+",
                "",
                command,
            ).strip()
        )
    lines.append(entry["smoke_test"])
    lines.append(f'display "PASS: {entry["slug"]}"')
    lines.append(f'display "{marker}"')
    lines.append("exit, clear")
    return "\n".join(lines) + "\n"


def selected_package_entries(
    entries: list[dict],
    package_slugs: list[str] | None,
) -> tuple[list[dict], list[str]]:
    if not package_slugs:
        return entries, []

    by_slug = {entry.get("slug"): entry for entry in entries}
    selected: list[dict] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for slug in package_slugs:
        if slug in seen:
            continue
        seen.add(slug)
        entry = by_slug.get(slug)
        if entry is None:
            unknown.append(slug)
        else:
            selected.append(entry)
    return selected, unknown


def validate_packages(
    stata_binary: Path,
    work_root: Path,
    package_slugs: list[str] | None = None,
) -> list[tuple[str, bool, str]]:
    content_entries = package_content_entries()
    if not content_entries:
        return [
            (
                "<content>",
                False,
                "Canonical package content has no Stata validation entries; refusing a vacuous pass.",
            )
        ]
    requested = package_slugs or []
    include_alias = not requested or "diagnostics" in requested
    canonical_requests = [slug for slug in requested if slug != "diagnostics"]
    if requested and not canonical_requests:
        entries, unknown = [], []
    else:
        entries, unknown = selected_package_entries(
            content_entries,
            canonical_requests if requested else None,
        )
    results: list[tuple[str, bool, str]] = [
        (slug, False, f"Unknown package slug: {slug}") for slug in unknown
    ]

    for entry in entries:
        slug = entry.get("slug", "<missing-slug>")
        try:
            run_dir = ensure_dir(work_root / "packages" / slug)
            plus_dir = ensure_dir(run_dir / "plus")
            ensure_dir(plus_dir / "personal")
            marker = completion_marker(f"package-{slug}")
            run_token = marker.rsplit("::", 1)[-1]
            do_file = run_dir / f"{slug}_smoke_{run_token}.do"
            child_plus_dir = Path(
                os.path.relpath(plus_dir, start=run_dir)
            )
            write_text(
                do_file,
                package_do_text(entry, child_plus_dir, marker),
            )
            result, log_path = run_stata_do(
                stata_binary,
                do_file,
                run_dir,
                completion_marker=marker,
                timeout_seconds=180,
            )
            log_text = read_text(log_path) if log_path.exists() else ""
            pass_marker = f"PASS: {slug}"
            success = (
                result.returncode == 0
                and log_path.exists()
                and not has_stata_error(log_text)
                and has_exact_log_line(log_text, pass_marker)
            )
            diagnostics = combined_output(log_text, result.stdout, result.stderr)
            if success and entry.get("install_commands"):
                lock_ok, lock_diagnostics = verify_package_install_lock(slug, plus_dir)
                success = lock_ok
                diagnostics = combined_output(
                    diagnostics,
                    "",
                    lock_diagnostics,
                )
        except Exception as error:
            success = False
            diagnostics = f"{type(error).__name__}: {error}"
        results.append((slug, success, diagnostics))
    if include_alias:
        alias_ok, alias_diagnostics = diagnostics_alias_check()
        results.append(("diagnostics [compatibility alias]", alias_ok, alias_diagnostics))
    return results


def plugin_do_text(plugin_path: Path, marker: str) -> str:
    phase = f"CODEX_PLUGIN_PHASE::{marker.rsplit('::', 1)[-1]}"
    return "\n".join(
        [
            "clear all",
            "set more off",
            f'display "{phase}::before-load"',
            f'program hello, plugin using("{plugin_path.as_posix()}")',
            f'display "{phase}::after-load"',
            f'display "{phase}::before-call"',
            "plugin call hello",
            f'display "{phase}::after-call"',
            'display "PASS: plugin-smoke"',
            f'display "{marker}"',
            "exit, clear",
        ]
    ) + "\n"


def validate_plugin_compile(work_root: Path) -> tuple[bool, str, Path | None]:
    run_dir = ensure_dir(work_root / "plugins" / "compile")
    try:
        for url, filename, expected_sha256 in plugin_sources():
            download_binary(
                url,
                run_dir / filename,
                timeout_seconds=30,
                expected_sha256=expected_sha256,
            )
    except Exception as error:
        return False, f"{type(error).__name__}: {error}", None

    machine = platform.machine().lower()
    if machine == "arm64":
        output_name = "hello.plugin.arm64"
        target = "arm64-apple-macos11"
    else:
        output_name = "hello.plugin.x86_64"
        target = "x86_64-apple-macos10.12"

    compile_result = run_command(
        [
            "clang",
            "-bundle",
            "-DSYSTEM=APPLEMAC",
            "-target",
            target,
            "stplugin.c",
            "hello.c",
            "-o",
            output_name,
        ],
        cwd=run_dir,
        timeout_seconds=60,
    )
    diagnostics = combined_output("", compile_result.stdout, compile_result.stderr)
    plugin_path = run_dir / output_name
    if compile_result.returncode != 0:
        return False, diagnostics, None
    if not plugin_path.is_file():
        missing = f"Compiler exited successfully but did not create {output_name}."
        return False, combined_output(diagnostics, "", missing), None
    return True, diagnostics, plugin_path


def validate_plugin_runtime(
    stata_binary: Path,
    work_root: Path,
    plugin_path: Path,
) -> tuple[bool, str]:
    run_dir = ensure_dir(work_root / "plugins" / "runtime")
    marker = completion_marker("plugin-runtime")
    run_token = marker.rsplit("::", 1)[-1]
    do_file = run_dir / f"plugin_smoke_{run_token}.do"
    child_plugin_path = Path(
        os.path.relpath(plugin_path, start=run_dir)
    )
    write_text(
        do_file,
        plugin_do_text(child_plugin_path, marker),
    )
    result, log_path = run_stata_do(
        stata_binary,
        do_file,
        run_dir,
        completion_marker=marker,
        timeout_seconds=30,
    )
    log_text = read_text(log_path) if log_path.exists() else ""
    phase = f"CODEX_PLUGIN_PHASE::{run_token}"
    expected_lines = (
        f"{phase}::before-load",
        f"{phase}::after-load",
        f"{phase}::before-call",
        "Hello World",
        f"{phase}::after-call",
        "PASS: plugin-smoke",
        marker,
    )
    # Consume standalone log lines in order; echoed commands are not evidence.
    log_lines = iter(line.strip() for line in log_text.splitlines())
    evidence_complete = all(
        any(line == expected for line in log_lines)
        for expected in expected_lines
    )
    success = (
        result.returncode == 0
        and log_path.exists()
        and not has_stata_error(log_text)
        and evidence_complete
    )
    diagnostics = combined_output(log_text, result.stdout, result.stderr)
    if not evidence_complete:
        diagnostics = combined_output(
            diagnostics,
            "",
            "Missing or out-of-order plugin phase, callback, or completion evidence.",
        )
    return success, diagnostics


def sanitize_diagnostics(
    text: str,
    work_root: Path | None = None,
    max_chars: int = 4000,
) -> str:
    """Redact local paths and Stata license metadata before printing failures."""
    sanitized = text.replace("\r\n", "\n").replace("\r", "\n")
    replacements = [
        (str(REPO_ROOT), "<REPO_ROOT>"),
        (str(Path.home()), "<HOME>"),
    ]
    if work_root is not None:
        replacements.insert(0, (str(work_root), "<WORKDIR>"))
    for raw, replacement in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if raw:
            sanitized = sanitized.replace(raw, replacement)

    lines: list[str] = []
    redacted_license_block = False
    for line in sanitized.splitlines():
        if SENSITIVE_LOG_LINE.search(line):
            if not redacted_license_block:
                lines.append("[Stata license metadata redacted]")
                redacted_license_block = True
            continue
        redacted_license_block = False
        lines.append(line)
    sanitized = "\n".join(lines).strip()
    if len(sanitized) > max_chars:
        sanitized = f"[diagnostic tail]\n{sanitized[-max_chars:]}"
    return sanitized


def resolve_suites(requested: list[str] | None) -> tuple[str, ...]:
    selected = requested or ["default"]
    resolved: list[str] = []
    for suite in selected:
        expanded = DEFAULT_SUITES if suite == "default" else (suite,)
        for item in expanded:
            if item not in resolved:
                resolved.append(item)
    return tuple(resolved)


def _path_matches_identity(
    path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == expected_identity
    )


def _descriptor_reported_path(descriptor: int) -> Path | None:
    """Ask the host OS for the current pathname of an open descriptor."""

    if platform.system() == "Darwin":
        try:
            raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
            resolved = raw.split(b"\0", 1)[0]
            if resolved:
                return Path(os.fsdecode(resolved))
        except (OSError, ValueError):
            pass
    else:
        try:
            resolved = os.readlink(f"/proc/self/fd/{descriptor}")
            if resolved and not resolved.endswith(" (deleted)"):
                return Path(resolved)
        except OSError:
            pass
    return None


def _descriptor_path(descriptor: int, fallback: Path) -> Path | None:
    """Return only a pathname that still names the retained directory."""

    try:
        held = os.fstat(descriptor)
    except OSError:
        return None
    expected_identity = (held.st_dev, held.st_ino)
    candidates: list[Path] = []
    reported = _descriptor_reported_path(descriptor)
    if reported is not None:
        candidates.append(reported)
    candidates.append(fallback)
    checked: set[Path] = set()
    for candidate in candidates:
        if candidate in checked:
            continue
        checked.add(candidate)
        if _path_matches_identity(candidate, expected_identity):
            return candidate
    return None


def _descriptor_location(descriptor: int, fallback: Path) -> str:
    """Describe a retained directory without inventing a stale pathname."""

    verified = _descriptor_path(descriptor, fallback)
    if verified is not None:
        return str(verified)
    try:
        held = os.fstat(descriptor)
    except OSError:
        return "unknown pathname (descriptor identity unavailable)"
    return (
        "unknown pathname "
        f"(device={held.st_dev}, inode={held.st_ino})"
    )


def _required_descriptor_path(
    descriptor: int,
    fallback: Path,
    label: str,
) -> Path:
    verified = _descriptor_path(descriptor, fallback)
    if verified is None:
        raise OSError(
            f"{label} has {_descriptor_location(descriptor, fallback)}; "
            "no verified cleanup pathname is available"
        )
    return verified


def _retained_child_path(
    parent_descriptor: int,
    parent_fallback: Path,
    expected_identity: tuple[int, int],
) -> Path | None:
    """Return one verified direct-child path for an accepted identity."""

    try:
        matches = []
        for name in os.listdir(parent_descriptor):
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                stat.S_ISDIR(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == expected_identity
            ):
                matches.append(name)
        if len(matches) != 1:
            return None
        parent_path = _descriptor_path(
            parent_descriptor,
            parent_fallback,
        )
        if parent_path is None:
            return None
        candidate = parent_path / matches[0]
        if not _path_matches_identity(candidate, expected_identity):
            return None
        return candidate
    except BaseException:
        return None


def _create_validation_workspace() -> ValidationWorkspace:
    """Create and retain a run-private workspace below the canonical temp root."""

    temp_parent = Path(tempfile.gettempdir()).expanduser().resolve(strict=True)
    temp_parent_descriptor: int | None = None
    transaction_descriptor: int | None = None
    work_descriptor: int | None = None
    transaction_root: Path | None = None
    transaction_name: str | None = None
    transaction_identity: tuple[int, int] | None = None
    try:
        temp_parent_descriptor = os.open(
            temp_parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        temp_parent_metadata = os.fstat(temp_parent_descriptor)
        if not stat.S_ISDIR(temp_parent_metadata.st_mode):
            raise OSError(
                f"validation temp parent is not a directory: {temp_parent}"
            )

        for _ in range(32):
            candidate = f"{VALIDATION_TRANSACTION_PREFIX}{uuid.uuid4().hex}"
            try:
                os.mkdir(
                    candidate,
                    mode=0o500,
                    dir_fd=temp_parent_descriptor,
                )
            except FileExistsError:
                continue
            transaction_name = candidate
            break
        if transaction_name is None:
            raise FileExistsError(
                "could not allocate a unique validation transaction directory"
            )

        transaction_root = temp_parent / transaction_name
        transaction_metadata = os.stat(
            transaction_name,
            dir_fd=temp_parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(transaction_metadata.st_mode):
            raise OSError(
                "created validation transaction root is not a directory: "
                f"{transaction_root}"
            )
        if stat.S_IMODE(transaction_metadata.st_mode) != 0o500:
            raise OSError(
                "created validation transaction root changed before identity "
                f"capture: {transaction_root}"
            )
        transaction_identity = (
            transaction_metadata.st_dev,
            transaction_metadata.st_ino,
        )
        transaction_descriptor = os.open(
            transaction_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=temp_parent_descriptor,
        )
        opened_transaction = os.fstat(transaction_descriptor)
        public_transaction = os.stat(
            transaction_name,
            dir_fd=temp_parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened_transaction.st_mode)
            or not stat.S_ISDIR(public_transaction.st_mode)
            or (
                opened_transaction.st_dev,
                opened_transaction.st_ino,
            )
            != transaction_identity
            or (
                public_transaction.st_dev,
                public_transaction.st_ino,
            )
            != transaction_identity
            or stat.S_IMODE(opened_transaction.st_mode) != 0o500
            or stat.S_IMODE(public_transaction.st_mode) != 0o500
            or os.listdir(transaction_descriptor)
        ):
            raise OSError(
                "validation transaction root changed while opening it: "
                f"{transaction_root}"
            )
        os.fchmod(transaction_descriptor, 0o700)
        opened_transaction = os.fstat(transaction_descriptor)
        public_transaction = os.stat(
            transaction_name,
            dir_fd=temp_parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (opened_transaction.st_dev, opened_transaction.st_ino)
            != transaction_identity
            or (public_transaction.st_dev, public_transaction.st_ino)
            != transaction_identity
            or stat.S_IMODE(opened_transaction.st_mode) != 0o700
            or stat.S_IMODE(public_transaction.st_mode) != 0o700
            or os.listdir(transaction_descriptor)
        ):
            raise OSError(
                "validation transaction root changed during initialization: "
                f"{transaction_root}"
            )

        os.mkdir(
            VALIDATION_WORKDIR_NAME,
            mode=0o500,
            dir_fd=transaction_descriptor,
        )
        work_root = transaction_root / VALIDATION_WORKDIR_NAME
        work_metadata = os.stat(
            VALIDATION_WORKDIR_NAME,
            dir_fd=transaction_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(work_metadata.st_mode):
            raise OSError(
                f"created validation workdir is not a directory: {work_root}"
            )
        if stat.S_IMODE(work_metadata.st_mode) != 0o500:
            raise OSError(
                "created validation workdir changed before identity capture: "
                f"{work_root}"
            )
        work_identity = (work_metadata.st_dev, work_metadata.st_ino)
        work_descriptor = os.open(
            VALIDATION_WORKDIR_NAME,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=transaction_descriptor,
        )
        opened_work = os.fstat(work_descriptor)
        public_work = os.stat(
            VALIDATION_WORKDIR_NAME,
            dir_fd=transaction_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened_work.st_mode)
            or not stat.S_ISDIR(public_work.st_mode)
            or (opened_work.st_dev, opened_work.st_ino) != work_identity
            or (public_work.st_dev, public_work.st_ino) != work_identity
            or stat.S_IMODE(opened_work.st_mode) != 0o500
            or stat.S_IMODE(public_work.st_mode) != 0o500
            or os.listdir(work_descriptor)
            or set(os.listdir(transaction_descriptor))
            != {VALIDATION_WORKDIR_NAME}
        ):
            raise OSError(
                f"validation workdir changed while opening it: {work_root}"
            )
        os.fchmod(work_descriptor, 0o700)
        opened_work = os.fstat(work_descriptor)
        public_work = os.stat(
            VALIDATION_WORKDIR_NAME,
            dir_fd=transaction_descriptor,
            follow_symlinks=False,
        )
        if (
            (opened_work.st_dev, opened_work.st_ino) != work_identity
            or (public_work.st_dev, public_work.st_ino) != work_identity
            or stat.S_IMODE(opened_work.st_mode) != 0o700
            or stat.S_IMODE(public_work.st_mode) != 0o700
            or os.listdir(work_descriptor)
            or set(os.listdir(transaction_descriptor))
            != {VALIDATION_WORKDIR_NAME}
        ):
            raise OSError(
                "validation workdir changed during initialization: "
                f"{work_root}"
            )
        os.fsync(work_descriptor)
        os.fsync(transaction_descriptor)
        os.fsync(temp_parent_descriptor)
        return ValidationWorkspace(
            temp_parent=temp_parent,
            transaction_root=transaction_root,
            work_root=work_root,
            temp_parent_descriptor=temp_parent_descriptor,
            transaction_descriptor=transaction_descriptor,
            work_descriptor=work_descriptor,
            transaction_identity=transaction_identity,
            work_identity=work_identity,
            transaction_mode=stat.S_IMODE(opened_transaction.st_mode),
            work_mode=stat.S_IMODE(opened_work.st_mode),
        )
    except BaseException as error:
        recovery_root: str | None = None
        recovery_errors: list[BaseException] = []
        try:
            if (
                transaction_descriptor is not None
                and transaction_root is not None
                and transaction_identity is not None
                and (
                    (
                        held_transaction := os.fstat(
                            transaction_descriptor
                        )
                    ).st_dev,
                    held_transaction.st_ino,
                )
                == transaction_identity
            ):
                recovery_root = _descriptor_location(
                    transaction_descriptor,
                    transaction_root,
                )
            elif (
                temp_parent_descriptor is not None
                and transaction_identity is not None
            ):
                retained_child = _retained_child_path(
                    temp_parent_descriptor,
                    temp_parent,
                    transaction_identity,
                )
                if retained_child is not None:
                    recovery_root = str(retained_child)
                else:
                    recovery_root = (
                        "unknown pathname for the accepted validation "
                        "transaction "
                        f"(device={transaction_identity[0]}, "
                        f"inode={transaction_identity[1]})"
                    )
            elif transaction_root is not None:
                recovery_root = (
                    "unknown pathname (transaction creation was incomplete; "
                    f"candidate path was {transaction_root})"
                )
        except BaseException as recovery_error:
            recovery_errors.append(recovery_error)
            recovery_root = (
                "unknown pathname "
                "(recovery-location reporting was interrupted)"
            )
        close_errors: list[BaseException] = []
        for descriptor in (
            work_descriptor,
            transaction_descriptor,
            temp_parent_descriptor,
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as close_error:
                    close_errors.append(close_error)
        finalization_errors = [*recovery_errors, *close_errors]
        closure_note = (
            "; setup finalization also encountered "
            + ", ".join(
                type(finalization_error).__name__
                for finalization_error in finalization_errors
            )
            if finalization_errors
            else ""
        )
        if isinstance(error, Exception) and recovery_root is not None:
            raise OSError(
                f"{error}; incomplete validation transaction location: "
                f"{recovery_root}{closure_note}"
            ) from error
        if recovery_root is not None:
            try:
                print(
                    "validation workspace setup was interrupted; incomplete "
                    f"transaction location: {recovery_root}{closure_note}"
                )
            except BaseException:
                pass
        elif finalization_errors:
            try:
                print(
                    "validation workspace setup failed before any validation "
                    f"transaction path was created{closure_note}"
                )
            except BaseException:
                pass
        raise


def _open_validation_workdir(
    work_root: Path,
    expected_identity: tuple[int, int],
) -> tuple[int, int, tuple[int, int]]:
    """Retain the created validation directory and its parent."""

    root_descriptor: int | None = None
    parent_descriptor = os.open(
        work_root.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        public_metadata = os.stat(
            work_root.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(public_metadata.st_mode):
            raise OSError(
                f"validation workdir is not a directory: {work_root}"
            )
        root_descriptor = os.open(
            work_root.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(opened_metadata.st_mode)
            or opened_metadata.st_dev != public_metadata.st_dev
            or opened_metadata.st_ino != public_metadata.st_ino
            or (opened_metadata.st_dev, opened_metadata.st_ino)
            != expected_identity
        ):
            raise OSError(
                f"validation workdir changed while opening it: {work_root}"
            )
    except BaseException:
        try:
            if root_descriptor is not None:
                os.close(root_descriptor)
        finally:
            os.close(parent_descriptor)
        raise
    return (
        parent_descriptor,
        root_descriptor,
        (opened_metadata.st_dev, opened_metadata.st_ino),
    )


def _retain_existing_validation_workspace(
    work_root: Path,
) -> ValidationWorkspace:
    """Retain an existing test fixture without claiming ownership of its parent."""

    metadata = work_root.lstat()
    identity = (metadata.st_dev, metadata.st_ino)
    parent_descriptor, work_descriptor, opened_identity = (
        _open_validation_workdir(work_root, identity)
    )
    parent_metadata = os.fstat(parent_descriptor)
    return ValidationWorkspace(
        temp_parent=work_root.parent,
        transaction_root=work_root.parent,
        work_root=work_root,
        temp_parent_descriptor=None,
        transaction_descriptor=parent_descriptor,
        work_descriptor=work_descriptor,
        transaction_identity=(
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ),
        work_identity=opened_identity,
        transaction_mode=stat.S_IMODE(parent_metadata.st_mode),
        work_mode=stat.S_IMODE(metadata.st_mode),
        owns_transaction_root=False,
    )


def _retain_validation_workdir(
    work_root: Path,
    parent_descriptor: int,
    root_descriptor: int,
    expected_identity: tuple[int, int],
    expected_mode: int,
) -> Path:
    """Verify and retain the descriptor-captured workspace for explicit cleanup."""

    held_metadata = os.fstat(root_descriptor)
    try:
        public_metadata = os.stat(
            work_root.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        public_metadata = None
    if (
        public_metadata is None
        or not stat.S_ISDIR(public_metadata.st_mode)
        or (held_metadata.st_dev, held_metadata.st_ino) != expected_identity
        or (public_metadata.st_dev, public_metadata.st_ino)
        != expected_identity
    ):
        matching_names: list[str] = []
        try:
            for name in os.listdir(parent_descriptor):
                metadata = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (metadata.st_dev, metadata.st_ino) == expected_identity:
                    matching_names.append(name)
        except OSError:
            matching_names = []
        recovery = (
            "unknown pathname "
            f"(device={expected_identity[0]}, inode={expected_identity[1]})"
        )
        if len(matching_names) == 1:
            parent_path = _descriptor_path(
                parent_descriptor,
                work_root.parent,
            )
            candidate = (
                parent_path / matching_names[0]
                if parent_path is not None
                else None
            )
            if (
                candidate is not None
                and _path_matches_identity(candidate, expected_identity)
            ):
                recovery = str(candidate)
        raise OSError(
            "validation workdir identity changed before cleanup; retained "
            f"workspace survives at {recovery}"
        )

    expected_entries = _capture_directory_descriptor_tree(
        root_descriptor,
        work_root,
    )
    _retain_owned_directory(
        work_root,
        expected_identity[0],
        expected_identity[1],
        expected_entries,
        expected_mode=expected_mode,
        parent_descriptor=parent_descriptor,
    )
    return _required_descriptor_path(
        root_descriptor,
        work_root,
        "validation workdir",
    )


def _remove_owned_validation_workspace(
    workspace: ValidationWorkspace,
) -> None:
    """Remove one run-private workspace through verified quarantine moves."""

    if (
        not workspace.owns_transaction_root
        or workspace.temp_parent_descriptor is None
    ):
        raise OSError(
            "validation workspace is not an owned run-private transaction"
        )

    transaction_root = _required_descriptor_path(
        workspace.transaction_descriptor,
        workspace.transaction_root,
        "validation transaction",
    )
    work_root = _required_descriptor_path(
        workspace.work_descriptor,
        workspace.work_root,
        "validation workdir",
    )
    expected_entries = _capture_directory_descriptor_tree(
        workspace.work_descriptor,
        work_root,
    )
    _remove_owned_directory(
        work_root,
        workspace.work_identity[0],
        workspace.work_identity[1],
        expected_entries,
        expected_mode=workspace.work_mode,
        parent_descriptor=workspace.transaction_descriptor,
    )

    held_transaction = os.fstat(workspace.transaction_descriptor)
    public_transaction = os.stat(
        transaction_root.name,
        dir_fd=workspace.temp_parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(held_transaction.st_mode)
        or not stat.S_ISDIR(public_transaction.st_mode)
        or (
            held_transaction.st_dev,
            held_transaction.st_ino,
        )
        != workspace.transaction_identity
        or (
            public_transaction.st_dev,
            public_transaction.st_ino,
        )
        != workspace.transaction_identity
        or stat.S_IMODE(held_transaction.st_mode)
        != workspace.transaction_mode
        or stat.S_IMODE(public_transaction.st_mode)
        != workspace.transaction_mode
        or os.listdir(workspace.transaction_descriptor)
    ):
        raise OSError(
            "validation transaction changed after workdir cleanup; "
            "preserving it"
        )

    _remove_verified_empty_directory_via_quarantine(
        workspace.temp_parent_descriptor,
        transaction_root.name,
        transaction_root,
        held_transaction,
    )


def _validation_workspace_recovery_path(
    workspace: ValidationWorkspace,
) -> Path:
    """Return the verified current location of a preserved workspace."""

    return _required_descriptor_path(
        workspace.work_descriptor,
        workspace.work_root,
        "validation workdir",
    )


def _validation_cleanup_path(workspace: ValidationWorkspace) -> Path:
    """Return the exact owned directory that a human may later remove."""

    if workspace.owns_transaction_root:
        return _required_descriptor_path(
            workspace.transaction_descriptor,
            workspace.transaction_root,
            "validation transaction",
        )
    return _validation_workspace_recovery_path(workspace)


@contextmanager
def _validation_workdir_scope(
    workspace: ValidationWorkspace,
):
    """Run path-oriented validators from the descriptor-retained workdir."""

    original_cwd_descriptor: int | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
    finalization_errors: list[BaseException] = []
    try:
        original_cwd_descriptor = os.open(
            ".",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        held_work = os.fstat(workspace.work_descriptor)
        public_work = os.stat(
            workspace.work_root.name,
            dir_fd=workspace.transaction_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(held_work.st_mode)
            or not stat.S_ISDIR(public_work.st_mode)
            or (held_work.st_dev, held_work.st_ino)
            != workspace.work_identity
            or (public_work.st_dev, public_work.st_ino)
            != workspace.work_identity
            or stat.S_IMODE(held_work.st_mode) != workspace.work_mode
            or stat.S_IMODE(public_work.st_mode) != workspace.work_mode
            or os.listdir(workspace.work_descriptor)
            or set(os.listdir(workspace.transaction_descriptor))
            != {workspace.work_root.name}
        ):
            raise OSError(
                "validation workdir changed before a validation phase"
            )
        os.fchdir(workspace.work_descriptor)
        anchored = os.stat(".", follow_symlinks=False)
        if (
            (anchored.st_dev, anchored.st_ino)
            != workspace.work_identity
            or os.listdir(workspace.work_descriptor)
            or set(os.listdir(workspace.transaction_descriptor))
            != {workspace.work_root.name}
        ):
            raise OSError(
                "validation workdir changed while anchoring validation phases"
            )
        yield Path(".")
        after = os.stat(".", follow_symlinks=False)
        held_after = os.fstat(workspace.work_descriptor)
        if (
            (after.st_dev, after.st_ino) != workspace.work_identity
            or (held_after.st_dev, held_after.st_ino)
            != workspace.work_identity
        ):
            raise OSError(
                "validation workdir changed during a validation phase"
            )
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    if original_cwd_descriptor is not None:
        try:
            os.fchdir(original_cwd_descriptor)
        except BaseException as restore_error:
            finalization_errors.append(restore_error)
    finalization_errors.extend(
        _close_descriptor_list(
            (
                [original_cwd_descriptor]
                if original_cwd_descriptor is not None
                else []
            ),
            "validation workdir scope",
        )
    )
    _raise_after_descriptor_finalization(
        primary_error,
        primary_traceback,
        tuple(finalization_errors),
        "validation workdir scope",
    )


def _finalize_validation_receipt_transaction(
    transaction: ValidationReceiptTransaction | None,
    *,
    report_retained: bool,
    primary_error: BaseException | None = None,
    primary_traceback: object | None = None,
) -> None:
    """Report retained receipt state and close without replacing a primary error."""

    finalization_errors: list[BaseException] = []
    if transaction is not None and report_retained:
        try:
            locations = retained_validation_receipt_locations(transaction)
            if locations:
                print(
                    "NOTICE: validation receipt transaction state retained for "
                    "explicit inspection at: "
                    + "; ".join(locations)
                )
        except BaseException as reporting_error:
            finalization_errors.append(reporting_error)
    if transaction is not None:
        try:
            close_validation_receipt_transaction(transaction)
        except BaseException as close_error:
            finalization_errors.append(close_error)
    _raise_after_descriptor_finalization(
        primary_error,
        primary_traceback,
        tuple(finalization_errors),
        "validation receipt transaction",
    )


def _report_validation_receipt_error_notes(error: BaseException) -> None:
    """Surface retained receipt locations attached during setup failures."""

    for note in getattr(error, "__notes__", ()):
        print(f"NOTICE: {note}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate generated Stata Codex skills.")
    parser.add_argument(
        "--suite",
        action="append",
        choices=SUITE_CHOICES,
        help="Validation suite to run; may be repeated. Defaults to the default suite.",
    )
    parser.add_argument(
        "--core",
        action="append",
        dest="core_slugs",
        metavar="SLUG",
        help="Limit core validation to this canonical content slug; may be repeated.",
    )
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        metavar="SLUG",
        help="Limit package validation to this canonical content slug; may be repeated.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help=(
            "Retain the run-private validation transaction for explicit "
            "inspection. Without this flag, ordinary completed runs remove "
            "their verified workspace."
        ),
    )
    parser.add_argument(
        "--write-receipt",
        action="store_true",
        help=(
            "Write a digest-bound publication receipt after an unfiltered "
            "default validation succeeds."
        ),
    )
    parser.add_argument(
        "--invalidate-receipt",
        action="store_true",
        help=(
            "Invalidate any prior publication receipt through a retained "
            "descriptor transaction, then exit."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt_path = BUILD_ROOT.parent / VALIDATION_RECEIPT_PATH.name
    if (
        BUILD_ROOT.parent.is_symlink()
        or BUILD_ROOT.is_symlink()
        or receipt_path.is_symlink()
    ):
        print(
            "ERROR: validation build and receipt paths must not be symbolic links."
        )
        return 2
    if args.invalidate_receipt:
        if (
            args.suite
            or args.core_slugs
            or args.packages
            or args.keep_workdir
            or args.write_receipt
        ):
            print(
                "ERROR: --invalidate-receipt cannot be combined with "
                "validation or receipt-writing options."
            )
            return 2
        try:
            invalidation = begin_validation_receipt_transaction(receipt_path)
        except FileNotFoundError:
            return 0
        except Exception as error:
            print(f"ERROR: could not invalidate validation receipt: {error}")
            _report_validation_receipt_error_notes(error)
            return 1
        try:
            locations = retained_validation_receipt_locations(invalidation)
            if invalidation.prior_backup_name is not None and locations:
                print(
                    "NOTICE: prior validation receipt retained for explicit "
                    f"cleanup at: {locations[0]}"
                )
            _finalize_validation_receipt_transaction(
                invalidation,
                report_retained=False,
            )
        except Exception as error:
            try:
                _finalize_validation_receipt_transaction(
                    invalidation,
                    report_retained=True,
                )
            except Exception as finalization_error:
                print(
                    "ERROR: validation receipt invalidation finalization also "
                    f"failed: {finalization_error}"
                )
            print(
                "ERROR: could not finalize validation receipt invalidation: "
                f"{error}"
            )
            return 1
        except BaseException as error:
            _finalize_validation_receipt_transaction(
                invalidation,
                report_retained=False,
                primary_error=error,
                primary_traceback=error.__traceback__,
            )
            raise AssertionError("unreachable")
        return 0
    suites = resolve_suites(args.suite)
    if args.write_receipt and (
        set(suites) != set(DEFAULT_SUITES)
        or args.core_slugs
        or args.packages
    ):
        print(
            "ERROR: --write-receipt requires the complete, unfiltered "
            "default validation suite."
        )
        return 2
    receipt_transaction: ValidationReceiptTransaction | None = None
    if args.write_receipt:
        try:
            receipt_transaction = begin_validation_receipt_transaction(
                receipt_path
            )
            initial_validation_state = validation_state(BUILD_ROOT)
        except Exception as error:
            print(f"ERROR: cannot begin receipt-bearing validation: {error}")
            _report_validation_receipt_error_notes(error)
            try:
                _finalize_validation_receipt_transaction(
                    receipt_transaction,
                    report_retained=True,
                )
            except Exception as finalization_error:
                print(
                    "ERROR: validation receipt finalization also failed: "
                    f"{finalization_error}"
                )
            return 1
        except BaseException as error:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=True,
                primary_error=error,
                primary_traceback=error.__traceback__,
            )
            raise AssertionError("unreachable")
    else:
        initial_validation_state = None
    results: list[tuple[str, bool, str]] = []
    try:
        workspace = _create_validation_workspace()
        work_root = workspace.work_root
    except Exception as setup_error:
        print(
            "ERROR: could not retain the created validation workdir: "
            f"{setup_error}"
        )
        try:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=True,
            )
        except Exception as finalization_error:
            print(
                "ERROR: validation receipt finalization also failed: "
                f"{finalization_error}"
            )
        return 1
    except BaseException as error:
        _finalize_validation_receipt_transaction(
            receipt_transaction,
            report_retained=True,
            primary_error=error,
            primary_traceback=error.__traceback__,
        )
        raise AssertionError("unreachable")
    validation_error: BaseException | None = None
    validation_traceback = None

    def record(label: str, success: bool, diagnostics: str = "") -> None:
        results.append((label, success, diagnostics))
        print(f"{label}: {'PASS' if success else 'FAIL'}")
        if not success and diagnostics:
            safe = sanitize_diagnostics(diagnostics, work_root=work_root)
            if safe:
                print(safe)

    try:
        if "static" in suites:
            try:
                lint_errors = lint_repo()
                record("static", not lint_errors, "\n".join(lint_errors))
            except Exception as error:
                record("static", False, f"{type(error).__name__}: {error}")

        with _validation_workdir_scope(workspace) as phase_work_root:
            needs_stata = any(
                suite in suites
                for suite in ("core", "packages", "plugin-runtime")
            )
            stata_binary = detect_stata_binary() if needs_stata else None

            if "core" in suites:
                if stata_binary is None:
                    record(
                        "core",
                        False,
                        "Could not locate a Stata binary under /Applications/Stata",
                    )
                else:
                    try:
                        core_results = validate_core(
                            stata_binary,
                            phase_work_root,
                            args.core_slugs,
                        )
                        if not core_results:
                            record(
                                "core",
                                False,
                                "Core validation produced no results; refusing a vacuous pass.",
                            )
                        else:
                            for slug, success, diagnostics in core_results:
                                record(f"core {slug}", success, diagnostics)
                    except Exception as error:
                        record(
                            "core",
                            False,
                            f"{type(error).__name__}: {error}",
                        )

            if "packages" in suites:
                if stata_binary is None:
                    record(
                        "packages",
                        False,
                        "Could not locate a Stata binary under /Applications/Stata",
                    )
                else:
                    try:
                        package_results = validate_packages(
                            stata_binary,
                            phase_work_root,
                            args.packages,
                        )
                        if not package_results:
                            record(
                                "packages",
                                False,
                                "Package validation produced no results; refusing a vacuous pass.",
                            )
                        else:
                            for slug, success, diagnostics in package_results:
                                record(
                                    f"package {slug}",
                                    success,
                                    diagnostics,
                                )
                    except Exception as error:
                        record(
                            "packages",
                            False,
                            f"{type(error).__name__}: {error}",
                        )

            plugin_ok = False
            plugin_output = ""
            plugin_path: Path | None = None
            if "plugin-compile" in suites or "plugin-runtime" in suites:
                try:
                    plugin_ok, plugin_output, plugin_path = (
                        validate_plugin_compile(phase_work_root)
                    )
                except Exception as error:
                    plugin_output = f"{type(error).__name__}: {error}"
                record("plugin compile", plugin_ok, plugin_output)

            if "plugin-runtime" in suites:
                if not plugin_ok or plugin_path is None:
                    record(
                        "plugin runtime",
                        False,
                        "Plugin compilation prerequisite failed.",
                    )
                elif stata_binary is None:
                    record(
                        "plugin runtime",
                        False,
                        "Could not locate a Stata binary under /Applications/Stata",
                    )
                else:
                    try:
                        runtime_ok, runtime_output = validate_plugin_runtime(
                            stata_binary,
                            phase_work_root,
                            plugin_path,
                        )
                        record("plugin runtime", runtime_ok, runtime_output)
                    except Exception as error:
                        record(
                            "plugin runtime",
                            False,
                            f"{type(error).__name__}: {error}",
                        )
    except Exception as error:
        record(
            "validation workspace",
            False,
            f"{type(error).__name__}: {error}",
        )
    except BaseException as error:
        validation_error = error
        validation_traceback = error.__traceback__
    finally:
        deferred_cleanup_error: BaseException | None = None

        def defer_finalization_error(error: BaseException) -> None:
            nonlocal deferred_cleanup_error
            if deferred_cleanup_error is None:
                deferred_cleanup_error = error

        def safe_descriptor_location(
            descriptor: int,
            fallback: Path,
        ) -> str:
            try:
                return _descriptor_location(descriptor, fallback)
            except BaseException as location_error:
                defer_finalization_error(location_error)
                try:
                    held = os.fstat(descriptor)
                except BaseException as identity_error:
                    defer_finalization_error(identity_error)
                    return (
                        "unknown pathname "
                        "(descriptor identity reporting failed)"
                    )
                return (
                    "unknown pathname "
                    f"(device={held.st_dev}, inode={held.st_ino})"
                )

        def safe_finalization_print(message: str) -> None:
            try:
                print(message)
            except BaseException as print_error:
                defer_finalization_error(print_error)

        def record_finalization_failure(
            label: str,
            diagnostics: str,
        ) -> None:
            results.append((label, False, diagnostics))
            safe_finalization_print(f"{label}: FAIL")
            try:
                safe = sanitize_diagnostics(
                    diagnostics,
                    work_root=work_root,
                )
            except BaseException as sanitize_error:
                defer_finalization_error(sanitize_error)
                return
            if safe:
                safe_finalization_print(safe)

        retain_workspace = (
            args.keep_workdir
            or validation_error is not None
            or not workspace.owns_transaction_root
        )
        try:
            if retain_workspace:
                retained_work_root = _retain_validation_workdir(
                    work_root,
                    workspace.transaction_descriptor,
                    workspace.work_descriptor,
                    workspace.work_identity,
                    workspace.work_mode,
                )
                cleanup_path = _validation_cleanup_path(workspace)
                cleanup_label = (
                    "transaction"
                    if workspace.owns_transaction_root
                    else "workdir"
                )
                safe_finalization_print(
                    f"validation {cleanup_label} retained for explicit cleanup "
                    f"at: {cleanup_path}"
                )
                if workspace.owns_transaction_root:
                    safe_finalization_print(
                        "validation workdir retained for inspection at: "
                        f"{retained_work_root}"
                    )
            else:
                _remove_owned_validation_workspace(workspace)
        except Exception as cleanup_error:
            recovery_path = safe_descriptor_location(
                workspace.work_descriptor,
                workspace.work_root,
            )
            cleanup_path = (
                safe_descriptor_location(
                    workspace.transaction_descriptor,
                    workspace.transaction_root,
                )
                if workspace.owns_transaction_root
                else recovery_path
            )
            record_finalization_failure(
                (
                    "validation workspace retention verification"
                    if retain_workspace
                    else "validation workspace cleanup"
                ),
                (
                    f"{type(cleanup_error).__name__}: {cleanup_error}; "
                    "preserved validation state is at "
                    f"{recovery_path}; exact retained cleanup unit is "
                    f"{cleanup_path}"
                ),
            )
            safe_finalization_print(
                "validation workdir "
                + (
                    "retention verification"
                    if retain_workspace
                    else "cleanup"
                )
                + " failed closed; inspect the preservation "
                f"details above before retrying: {cleanup_path}"
            )
        except BaseException as cleanup_error:
            defer_finalization_error(cleanup_error)
            recovery_path = safe_descriptor_location(
                workspace.work_descriptor,
                workspace.work_root,
            )
            cleanup_path = (
                safe_descriptor_location(
                    workspace.transaction_descriptor,
                    workspace.transaction_root,
                )
                if workspace.owns_transaction_root
                else recovery_path
            )
            safe_finalization_print(
                "validation workdir "
                + (
                    "retention verification"
                    if retain_workspace
                    else "cleanup"
                )
                + " was interrupted; the validation "
                f"state may remain at or beneath: {recovery_path}; exact "
                f"retained cleanup unit is {cleanup_path}"
            )
        finally:
            descriptor_errors: list[str] = []
            for label, descriptor in (
                ("workdir", workspace.work_descriptor),
                (
                    "validation transaction",
                    workspace.transaction_descriptor,
                ),
                ("temp parent", workspace.temp_parent_descriptor),
            ):
                if descriptor is None:
                    continue
                try:
                    os.close(descriptor)
                except BaseException as close_error:
                    if isinstance(close_error, Exception):
                        descriptor_errors.append(
                            f"{label} descriptor closure failed: {close_error}"
                        )
                    else:
                        defer_finalization_error(close_error)
            if descriptor_errors:
                record_finalization_failure(
                    "validation workspace descriptor cleanup",
                    "; ".join(descriptor_errors),
                )
            if (
                deferred_cleanup_error is not None
                and validation_error is not None
            ):
                safe_finalization_print(
                    "validation finalization also encountered "
                    f"{type(deferred_cleanup_error).__name__}; preserving the "
                    "active validation interruption"
                )
        if validation_error is not None:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=True,
                primary_error=validation_error,
                primary_traceback=validation_traceback,
            )
            raise AssertionError("unreachable")
        if deferred_cleanup_error is not None:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=True,
                primary_error=deferred_cleanup_error,
                primary_traceback=deferred_cleanup_error.__traceback__,
            )
            raise AssertionError("unreachable")

    failed = any(not success for _, success, _ in results)
    if failed:
        try:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=True,
            )
        except Exception as finalization_error:
            print(
                "ERROR: validation receipt finalization also failed: "
                f"{finalization_error}"
            )
        return 1
    if not failed and args.write_receipt:
        try:
            write_validation_receipt(
                build_root=BUILD_ROOT,
                receipt_path=receipt_path,
                expected_state=initial_validation_state,
                transaction=receipt_transaction,
            )
        except Exception as error:
            print(f"ERROR: could not write validation receipt: {error}")
            try:
                _finalize_validation_receipt_transaction(
                    receipt_transaction,
                    report_retained=True,
                )
            except Exception as finalization_error:
                print(
                    "ERROR: validation receipt finalization also failed: "
                    f"{finalization_error}"
                )
            return 1
        except BaseException as error:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=True,
                primary_error=error,
                primary_traceback=error.__traceback__,
            )
            raise AssertionError("unreachable")
        print(f"Validation receipt written: {receipt_path}")
        try:
            _finalize_validation_receipt_transaction(
                receipt_transaction,
                report_retained=False,
            )
        except Exception as finalization_error:
            print(
                "ERROR: validation receipt finalization failed after "
                f"publication: {finalization_error}"
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
