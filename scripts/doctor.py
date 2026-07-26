#!/usr/bin/env python3
"""Offline environment diagnostics for build and validation prerequisites."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tomllib

from runtime_guard import require_supported_runtime

require_supported_runtime()

import jinja2
import yaml

from libskillpack import (
    BUILD_ROOT,
    REPO_ROOT,
    detect_stata_binary,
    stata_containment_status,
)


def required_uv_version() -> str:
    """Read the exact uv version shared by local commands and CI."""

    pyproject_path = REPO_ROOT / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not read {pyproject_path}: {error}") from error
    specifier = (
        pyproject.get("tool", {})
        .get("uv", {})
        .get("required-version")
    )
    match = re.fullmatch(r"==(\d+\.\d+\.\d+)", str(specifier))
    if match is None:
        raise ValueError(
            "pyproject.toml must declare one exact tool.uv.required-version"
        )
    return match.group(1)


def installed_uv_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"could not run uv --version: {error}") from error
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "uv --version failed")
    match = re.fullmatch(r"uv (\d+\.\d+\.\d+)(?: .*)?", result.stdout.strip())
    if match is None:
        raise ValueError(f"unexpected uv version output: {result.stdout.strip()!r}")
    return match.group(1)


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    expected_uv: str | None = None
    actual_uv: str | None = None
    for relative in ("pyproject.toml", "uv.lock", "config/skills.yaml"):
        if not (REPO_ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        errors.append("uv is not available on PATH")
    else:
        try:
            expected_uv = required_uv_version()
            actual_uv = installed_uv_version(uv_executable)
            if actual_uv != expected_uv:
                errors.append(
                    f"uv {expected_uv} required; found uv {actual_uv}"
                )
        except ValueError as error:
            errors.append(str(error))
    if shutil.which("git") is None:
        errors.append("git is not available on PATH")
    if shutil.which("clang") is None:
        warnings.append("clang is unavailable; plugin compilation validation will fail")
    stata_binary = detect_stata_binary()
    if stata_binary is None:
        warnings.append("Stata is unavailable; licensed core/package validation cannot run")
    else:
        containment_available, reason = stata_containment_status()
        if not containment_available:
            warnings.append(
                "macOS sandbox-exec containment is unavailable; licensed "
                f"core/package validation will fail before Stata starts ({reason})"
            )
    build_parent = BUILD_ROOT.parent
    if not build_parent.exists():
        nearest = build_parent.parent
        if not os.access(nearest, os.W_OK):
            errors.append(f"build parent is not writable: {nearest}")
    elif not os.access(build_parent, os.W_OK):
        errors.append(f"build parent is not writable: {build_parent}")

    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    print(f"Python: {platform.python_version()}")
    if actual_uv is not None:
        print(f"uv: {actual_uv} (required {expected_uv})")
    print(f"Jinja2: {jinja2.__version__}")
    print(f"PyYAML: {yaml.__version__}")
    print(f"Codex skills destination: {codex_home / 'skills'}")
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print("Doctor passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
