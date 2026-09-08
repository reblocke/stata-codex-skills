#!/usr/bin/env python3
"""Run a trusted Stata do-file unattended and retain private execution evidence."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import sys
import uuid


def launch_arguments(
    stata_binary: Path,
    do_file: Path,
    platform_name: str | None = None,
) -> list[str]:
    """Select the direct, unattended interface without a GUI fallback."""
    system = platform_name or platform.system()
    if system not in {"Darwin", "Linux"}:
        raise ValueError(f"Unattended Stata execution is unsupported on {system}.")
    executable = stata_binary.resolve()
    parts = executable.parts
    in_app_bundle = any(
        part.endswith(".app") and parts[index + 1:index + 3] == ("Contents", "MacOS")
        for index, part in enumerate(parts)
    )
    options = ["-e"] if system == "Darwin" and in_app_bundle else ["-b", "do"]
    return [str(executable), *options, str(do_file)]


def _stata_path(path: Path) -> str:
    value = str(path)
    if any(character in value for character in "\"'`$\r\n"):
        raise ValueError("Stata paths cannot contain quotes, backticks, dollars, or newlines.")
    return f'"{value}"'


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _wrapper_text(do_file: Path, cwd: Path, run_dir: Path, token: str) -> str:
    return "\n".join(
        [
            "set more off",
            "capture log close _all",
            f"log using {_stata_path(run_dir / 'stata.log')}, text name(codex_runner)",
            f"cd {_stata_path(cwd)}",
            f"capture noisily do {_stata_path(do_file)}",
            "local codex_stata_rc = _rc",
            "tempname codex_result",
            f"file open `codex_result' using {_stata_path(run_dir / 'completion.txt')}, write text",
            f'file write `codex_result\' "STATA_RUNNER::{token}::`codex_stata_rc\'" _n',
            "file close `codex_result'",
            "capture log close codex_runner",
            "exit `codex_stata_rc', clear STATA",
            "",
        ]
    )


def _stop_owned_group(process: subprocess.Popen[bytes]) -> bool:
    """Signal only our unreaped session leader's group, then reap the leader."""
    if process.returncode is not None:
        return False
    signaled = True
    try:
        if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
            return False
        # The deadline is final. Do not reap the leader before this group signal.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        signaled = False
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    return signaled


def _read_stata_rc(path: Path, token: str) -> int:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("The completion sidecar is not a regular file.")
        evidence = stream.read(257)
    match = re.fullmatch(
        rb"STATA_RUNNER::" + token.encode("ascii") + rb"::(0|[1-9][0-9]{0,9})\r?\n",
        evidence,
    )
    if match is None:
        raise ValueError("Missing exact current-run Stata return-code evidence.")
    return int(match.group(1))


def run_stata(
    stata_binary: Path,
    do_file: Path,
    run_dir: Path,
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 300,
) -> dict[str, object]:
    """Execute once; caller-owned run artifacts are never overwritten or removed."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("The timeout must be a positive finite number of seconds.")
    executable = stata_binary.expanduser().resolve(strict=True)
    target = do_file.expanduser().resolve(strict=True)
    project = (cwd or Path.cwd()).expanduser().resolve(strict=True)
    requested_run = Path(os.path.abspath(run_dir.expanduser()))
    destination = requested_run.parent.resolve(strict=True) / requested_run.name
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("--stata must name an executable file.")
    if not target.is_file() or not project.is_dir():
        raise ValueError("--do-file must name a file and --cwd must name a directory.")
    for path in (target, project, destination):
        _stata_path(path)
    wrapper = destination / "wrapper.do"
    command = launch_arguments(executable, wrapper)
    token = uuid.uuid4().hex
    wrapper_text = _wrapper_text(target, project, destination, token)
    destination.mkdir(mode=0o700)
    result: dict[str, object] = {
        "schema": 1,
        "run_id": token,
        "run_dir": str(destination),
        "cwd": str(project),
        "do_file": str(target),
        "command": command,
        "os_returncode": None,
        "stata_returncode": None,
        "natural_exit": False,
        "timed_out": False,
        "cleanup_succeeded": None,
        "success": False,
        "exit_code": 1,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        _write_private(wrapper, wrapper_text)
        _write_private(destination / "stdout.txt", "")
        _write_private(destination / "stderr.txt", "")
        with (destination / "stdout.txt").open("wb") as stdout, (
            destination / "stderr.txt"
        ).open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=destination,
                env={**os.environ, "PWD": str(destination)},
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                umask=0o077,
            )
            try:
                result["os_returncode"] = process.wait(timeout=timeout_seconds)
                result["natural_exit"] = process.returncode >= 0
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                result["exit_code"] = 124
                result["error"] = "Stata did not exit before the deadline."
                result["cleanup_succeeded"] = _stop_owned_group(process)
                result["os_returncode"] = process.returncode
        try:
            result["stata_returncode"] = _read_stata_rc(destination / "completion.txt", token)
        except (OSError, ValueError):
            result.setdefault("error", "Missing exact current-run Stata return-code evidence.")
        log_path = destination / "stata.log"
        log_present = log_path.is_file() and not log_path.is_symlink()
        if not log_present:
            result.setdefault("error", "The retained Stata log is missing or is not a regular file.")
        if not result["timed_out"]:
            if result["os_returncode"] != 0:
                result.setdefault("error", "The Stata process returned a nonzero OS status.")
            elif result["stata_returncode"] not in (None, 0):
                result["error"] = "The do-file returned a nonzero Stata status."
            elif result["stata_returncode"] == 0 and result["natural_exit"] and log_present:
                result["success"] = True
                result["exit_code"] = 0
    except (OSError, KeyboardInterrupt) as error:
        result["error"] = "Execution was interrupted." if isinstance(error, KeyboardInterrupt) else str(error)
        if process is not None and process.returncode is None:
            result["cleanup_succeeded"] = _stop_owned_group(process)
            result["os_returncode"] = process.returncode
    _write_private(destination / "result.json", json.dumps(result, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stata", type=Path, required=True)
    parser.add_argument("--do-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--timeout", type=float, default=300, metavar="SECONDS")
    args = parser.parse_args(argv)
    try:
        result = run_stata(
            args.stata,
            args.do_file,
            args.run_dir,
            cwd=args.cwd,
            timeout_seconds=args.timeout,
        )
    except (ValueError, OSError) as error:
        result = {"success": False, "exit_code": 2, "error": str(error)}
    print(json.dumps(result, sort_keys=True))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
