#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import platform
import re
import shutil
import tempfile
import uuid

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    REPO_ROOT,
    iter_content_entries,
    load_skill_config,
    detect_stata_binary,
    download_binary,
    ensure_dir,
    has_stata_error,
    read_text,
    read_yaml,
    run_command,
    run_stata_do,
    sha256_file,
    write_text,
)
from lint_skill_pack import lint_repo
from release_state import (
    VALIDATION_RECEIPT_PATH,
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

    lock_path = LOCK_ROOT / "packages.yaml"
    lock = read_yaml(lock_path)
    packages = lock.get("packages")
    if lock.get("schema_version") != 1 or not isinstance(packages, dict):
        return False, f"{lock_path}: invalid package lock"
    package = packages.get(slug)
    if not isinstance(package, dict):
        return False, f"{lock_path}: missing package lock for {slug}"
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
            write_text(do_file, package_do_text(entry, plus_dir, marker))
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
    return "\n".join(
        [
            "clear all",
            "set more off",
            f'program hello, plugin using("{plugin_path.as_posix()}")',
            "hello",
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
    write_text(do_file, plugin_do_text(plugin_path, marker))
    result, log_path = run_stata_do(
        stata_binary,
        do_file,
        run_dir,
        completion_marker=marker,
        timeout_seconds=30,
    )
    log_text = read_text(log_path) if log_path.exists() else ""
    success = (
        result.returncode == 0
        and log_path.exists()
        and not has_stata_error(log_text)
        and has_exact_log_line(log_text, "PASS: plugin-smoke")
    )
    return success, combined_output(log_text, result.stdout, result.stderr)


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
        help="Preserve the temporary work directory when validation fails.",
    )
    parser.add_argument(
        "--write-receipt",
        action="store_true",
        help=(
            "Write a digest-bound publication receipt after an unfiltered "
            "default validation succeeds."
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
    if args.write_receipt:
        receipt_path.unlink(missing_ok=True)
        try:
            initial_validation_state = validation_state(BUILD_ROOT)
        except Exception as error:
            print(f"ERROR: cannot begin receipt-bearing validation: {error}")
            return 1
    else:
        initial_validation_state = None
    results: list[tuple[str, bool, str]] = []
    work_root = Path(tempfile.mkdtemp(prefix="stata-codex-validate-"))

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

        needs_stata = any(
            suite in suites for suite in ("core", "packages", "plugin-runtime")
        )
        stata_binary = detect_stata_binary() if needs_stata else None

        if "core" in suites:
            if stata_binary is None:
                record("core", False, "Could not locate a Stata binary under /Applications/Stata")
            else:
                try:
                    core_results = validate_core(
                        stata_binary,
                        work_root,
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
                    record("core", False, f"{type(error).__name__}: {error}")

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
                        work_root,
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
                            record(f"package {slug}", success, diagnostics)
                except Exception as error:
                    record("packages", False, f"{type(error).__name__}: {error}")

        plugin_ok = False
        plugin_output = ""
        plugin_path: Path | None = None
        if "plugin-compile" in suites or "plugin-runtime" in suites:
            try:
                plugin_ok, plugin_output, plugin_path = validate_plugin_compile(work_root)
            except Exception as error:
                plugin_output = f"{type(error).__name__}: {error}"
            record("plugin compile", plugin_ok, plugin_output)

        if "plugin-runtime" in suites:
            if not plugin_ok or plugin_path is None:
                record("plugin runtime", False, "Plugin compilation prerequisite failed.")
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
                        work_root,
                        plugin_path,
                    )
                    record("plugin runtime", runtime_ok, runtime_output)
                except Exception as error:
                    record("plugin runtime", False, f"{type(error).__name__}: {error}")
    finally:
        failed = any(not success for _, success, _ in results)
        if args.keep_workdir and failed:
            print(f"failed validation workdir preserved at: {work_root}")
        else:
            shutil.rmtree(work_root, ignore_errors=True)

    failed = any(not success for _, success, _ in results)
    if not failed and args.write_receipt:
        try:
            write_validation_receipt(
                build_root=BUILD_ROOT,
                receipt_path=receipt_path,
                expected_state=initial_validation_state,
            )
        except Exception as error:
            print(f"ERROR: could not write validation receipt: {error}")
            receipt_path.unlink(missing_ok=True)
            return 1
        print(f"Validation receipt written: {receipt_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
