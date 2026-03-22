#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import platform
import shutil
import tempfile
from libskillpack import (
    MANIFEST_ROOT,
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


PLUGIN_H_URL = "https://www.stata.com/plugins/stplugin.h"
PLUGIN_C_URL = "https://www.stata.com/plugins/stplugin.c"
PLUGIN_HELLO_URL = "https://www.stata.com/plugins/hello.c"


def validate_core(stata_binary: Path, work_root: Path) -> tuple[bool, str]:
    do_file = TESTS_ROOT / "stata" / "core" / "core_smoke.do"
    run_dir = ensure_dir(work_root / "core")
    result, log_path = run_stata_do(stata_binary, do_file, run_dir, timeout_seconds=90)
    log_text = read_text(log_path) if log_path.exists() else ""
    success = result.returncode == 0 and log_path.exists() and not has_stata_error(log_text)
    return success, log_text


def package_do_text(entry: dict, plus_dir: Path) -> str:
    lines = [
        "clear all",
        "set more off",
        f'sysdir set PLUS "{plus_dir.as_posix()}"',
        f'sysdir set PERSONAL "{(plus_dir / "personal").as_posix()}"',
    ]
    lines.extend(entry.get("install_commands", []))
    lines.append(entry["smoke_test"])
    lines.append(f'display "PASS: {entry["slug"]}"')
    lines.append('display "VALIDATION COMPLETE"')
    lines.append("exit, clear")
    return "\n".join(lines) + "\n"


def validate_packages(stata_binary: Path, work_root: Path, limit: int | None) -> list[tuple[str, bool, str]]:
    manifest = read_yaml(MANIFEST_ROOT / "package-map.yaml")
    results: list[tuple[str, bool, str]] = []
    entries = manifest.get("entries", [])
    if limit is not None:
        entries = entries[:limit]

    for entry in entries:
        run_dir = ensure_dir(work_root / "packages" / entry["slug"])
        plus_dir = ensure_dir(run_dir / "plus")
        ensure_dir(plus_dir / "personal")
        do_file = run_dir / f'{entry["slug"]}_smoke.do'
        write_text(do_file, package_do_text(entry, plus_dir))
        result, log_path = run_stata_do(stata_binary, do_file, run_dir, timeout_seconds=60)
        log_text = read_text(log_path) if log_path.exists() else ""
        success = result.returncode == 0 and log_path.exists() and not has_stata_error(log_text)
        results.append((entry["slug"], success, log_text))
    return results


def plugin_do_text(plugin_path: Path) -> str:
    return "\n".join(
        [
            "clear all",
            "set more off",
            f'program hello, plugin using("{plugin_path.as_posix()}")',
            "hello",
            'display "PASS: plugin-smoke"',
            'display "VALIDATION COMPLETE"',
            "exit, clear",
        ]
    ) + "\n"


def validate_plugin(stata_binary: Path, work_root: Path) -> tuple[bool, str]:
    run_dir = ensure_dir(work_root / "plugins")
    download_binary(PLUGIN_H_URL, run_dir / "stplugin.h")
    download_binary(PLUGIN_C_URL, run_dir / "stplugin.c")
    download_binary(PLUGIN_HELLO_URL, run_dir / "hello.c")

    machine = platform.machine().lower()
    if machine == "arm64":
        output_name = "hello.plugin.arm64"
        target = "arm64-apple-macos11"
    else:
        output_name = "hello.plugin.x86_64"
        target = "x86_64-apple-macos10.12"

    compile = run_command(
        ["clang", "-bundle", "-DSYSTEM=APPLEMAC", "-target", target, "stplugin.c", "hello.c", "-o", output_name],
        cwd=run_dir,
    )
    if compile.returncode != 0:
        return False, (compile.stdout or "") + (compile.stderr or "")

    do_file = run_dir / "plugin_smoke.do"
    write_text(do_file, plugin_do_text(run_dir / output_name))
    result, log_path = run_stata_do(stata_binary, do_file, run_dir, timeout_seconds=30)
    log_text = read_text(log_path) if log_path.exists() else ""
    success = result.returncode == 0 and log_path.exists() and not has_stata_error(log_text)
    return success, log_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-packages", action="store_true")
    parser.add_argument("--skip-plugin", action="store_true")
    parser.add_argument("--package-limit", type=int, default=None)
    args = parser.parse_args()

    lint_errors = lint_repo()
    if lint_errors:
        for error in lint_errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)

    stata_binary = detect_stata_binary()
    if not stata_binary:
        raise SystemExit("Could not locate a Stata binary under /Applications/Stata")

    work_root = Path(tempfile.mkdtemp(prefix="stata-codex-validate-"))
    try:
        core_ok, core_log = validate_core(stata_binary, work_root)
        print(f"core: {'PASS' if core_ok else 'FAIL'}")
        if not core_ok:
            print(core_log[-4000:])

        if not args.skip_packages:
            package_results = validate_packages(stata_binary, work_root, args.package_limit)
            for slug, success, log_text in package_results:
                print(f"package {slug}: {'PASS' if success else 'FAIL'}")
                if not success:
                    print(log_text[-2000:])

        if not args.skip_plugin:
            plugin_ok, plugin_log = validate_plugin(stata_binary, work_root)
            print(f"plugin: {'PASS' if plugin_ok else 'FAIL'}")
            if not plugin_ok:
                print(plugin_log[-4000:])
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    main()
