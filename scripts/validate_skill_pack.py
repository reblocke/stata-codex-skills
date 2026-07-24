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
    MANIFEST_ROOT,
    REPO_ROOT,
    TESTS_ROOT,
    detect_stata_binary,
    download_binary,
    ensure_dir,
    has_stata_error,
    read_text,
    read_yaml,
    run_command,
    run_stata_do,
    write_text,
)
from lint_skill_pack import lint_repo


PLUGIN_SOURCES = (
    (
        "https://www.stata.com/plugins/stplugin.h",
        "stplugin.h",
        "0d32086bfb7a621e30ed7fefa41b351b6733bb4561da28a4c581580d62c64e8b",
    ),
    (
        "https://www.stata.com/plugins/stplugin.c",
        "stplugin.c",
        "ab694f53e30a404bbfbe59d301a81b8bc59eeecf84bc5427eb65cbf0c5020d6d",
    ),
    (
        "https://www.stata.com/plugins/hello.c",
        "hello.c",
        "1ea64f7dea195acd9bd5715b827669d62a642e62ea9d4cf4990649b87f69758c",
    ),
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
MARKER_PLACEHOLDER = 'display "VALIDATION COMPLETE"'
SENSITIVE_LOG_LINE = re.compile(
    r"(?i)\b("
    r"licensed?\s+to|license\s+(?:code|number|serial)|"
    r"serial\s+number|authorization\s+code"
    r")\b"
)


def completion_marker(label: str) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper()
    return f"CODEX_VALIDATION_COMPLETE::{safe_label}::{uuid.uuid4().hex}"


def has_exact_log_line(log_text: str, expected: str) -> bool:
    return any(line.strip() == expected for line in log_text.splitlines())


def stage_do_file(source: Path, run_dir: Path, marker: str) -> Path:
    """Copy a smoke test into its run directory with a unique marker and name."""
    source_text = read_text(source)
    if MARKER_PLACEHOLDER not in source_text:
        raise ValueError(f"{source.name} does not contain the validation marker placeholder")
    run_token = marker.rsplit("::", 1)[-1]
    staged = run_dir / f"{source.stem}_{run_token}.do"
    write_text(staged, source_text.replace(MARKER_PLACEHOLDER, f'display "{marker}"'))
    return staged


def combined_output(log_text: str, stdout: str | None, stderr: str | None) -> str:
    return "\n".join(part for part in (log_text, stdout or "", stderr or "") if part).strip()


def validate_core(stata_binary: Path, work_root: Path) -> tuple[bool, str]:
    source_do_file = TESTS_ROOT / "stata" / "core" / "core_smoke.do"
    run_dir = ensure_dir(work_root / "core")
    marker = completion_marker("core")
    do_file = stage_do_file(source_do_file, run_dir, marker)
    result, log_path = run_stata_do(
        stata_binary,
        do_file,
        run_dir,
        completion_marker=marker,
        timeout_seconds=90,
    )
    log_text = read_text(log_path) if log_path.exists() else ""
    success = result.returncode == 0 and log_path.exists() and not has_stata_error(log_text)
    return success, combined_output(log_text, result.stdout, result.stderr)


def package_do_text(entry: dict, plus_dir: Path, marker: str) -> str:
    lines = [
        "clear all",
        "set more off",
        "set seed 271828",
        f'sysdir set PLUS "{plus_dir.as_posix()}"',
        f'sysdir set PERSONAL "{(plus_dir / "personal").as_posix()}"',
    ]
    lines.extend(entry.get("install_commands", []))
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
    manifest = read_yaml(MANIFEST_ROOT / "package-map.yaml")
    manifest_entries = manifest.get("entries")
    if not isinstance(manifest_entries, list) or not manifest_entries:
        return [
            (
                "<manifest>",
                False,
                "Package manifest has no validation entries; refusing a vacuous pass.",
            )
        ]
    entries, unknown = selected_package_entries(manifest_entries, package_slugs)
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
                timeout_seconds=60,
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
        except Exception as error:
            success = False
            diagnostics = f"{type(error).__name__}: {error}"
        results.append((slug, success, diagnostics))
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
        for url, filename, expected_sha256 in PLUGIN_SOURCES:
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
        "--package",
        action="append",
        dest="packages",
        metavar="SLUG",
        help="Limit package validation to this manifest slug; may be repeated.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Preserve the temporary work directory when validation fails.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suites = resolve_suites(args.suite)
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
                    core_ok, core_output = validate_core(stata_binary, work_root)
                    record("core", core_ok, core_output)
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

    return 1 if any(not success for _, success, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
