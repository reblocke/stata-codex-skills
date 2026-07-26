from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request
import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver


REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_ROOT = REPO_ROOT / "content"
CONFIG_ROOT = REPO_ROOT / "config"
LOCK_ROOT = REPO_ROOT / "locks"
MANIFEST_ROOT = REPO_ROOT / "manifests"
RAW_ROOT = REPO_ROOT / "raw"
BUILD_ROOT = REPO_ROOT / "build" / "generated"
TEMPLATES_ROOT = REPO_ROOT / "templates"
TESTS_ROOT = REPO_ROOT / "tests"
SKILL_CONFIG_PATH = CONFIG_ROOT / "skills.yaml"
PROMPT_CASES_PATH = TESTS_ROOT / "prompts" / "cases.yaml"
UPSTREAM_REPO_URL = "https://github.com/dylantmoore/stata-skill.git"
UPSTREAM_REPO_DIR = RAW_ROOT / "upstream" / "stata-skill"
STATA_ROOT = Path("/Applications/Stata")
STATA_ADO_BASE = STATA_ROOT / "ado" / "base"
STATA_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5
MACOS_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
STATA_SANDBOX_PROFILE = (
    "(version 1)"
    "(allow default)"
    "(deny process-fork)"
    "(deny lsopen)"
    "(deny appleevent-send)"
)
STATA_SANDBOX_PROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class _ProcessStopResult:
    stdout: str
    stderr: str
    diagnostic: str
    cleanup_confirmed: bool


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses ambiguous mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )
    loader.flatten_mapping(node)
    mapping: dict = {}
    key_marks: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            first_mark = key_marks[key]
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                (
                    f"found duplicate key {key!r}; first occurrence was at "
                    f"line {first_mark.line + 1}, column {first_mark.column + 1}"
                ),
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
        key_marks[key] = key_node.start_mark
    return mapping


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def parse_yaml(data: str | bytes, *, source: str | Path) -> object:
    """Parse safe YAML while rejecting duplicate keys at every nesting level."""

    if isinstance(data, bytes):
        data = data.decode("utf-8")
    stream = io.StringIO(data)
    stream.name = os.fspath(source)
    return yaml.load(stream, Loader=_UniqueKeySafeLoader)


def atomic_rename_at_no_replace(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically move one descriptor-relative entry only when target is absent."""

    for label, name in (
        ("source", source_name),
        ("destination", destination_name),
    ):
        if not name or name in {".", ".."} or Path(name).name != name:
            raise ValueError(
                f"atomic no-replace {label} must be one path component"
            )

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename_exclusive = libc.renameatx_np
        except AttributeError as error:
            raise RuntimeError(
                "This macOS runtime lacks descriptor-relative atomic "
                "no-replace rename support"
            ) from error
        flag = 0x00000004  # RENAME_EXCL from <sys/stdio.h>
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = libc.renameat2
        except AttributeError as error:
            raise RuntimeError(
                "This Linux runtime lacks descriptor-relative atomic "
                "no-replace rename support"
            ) from error
        flag = 1  # RENAME_NOREPLACE from <linux/fs.h>
    else:
        raise RuntimeError(
            "Descriptor-relative atomic no-replace rename is supported only "
            "on macOS and Linux"
        )
    rename_exclusive.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exclusive.restype = ctypes.c_int
    result = rename_exclusive(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )
    os.fsync(destination_descriptor)
    if source_descriptor != destination_descriptor:
        os.fsync(source_descriptor)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = parse_yaml(path.read_text(encoding="utf-8"), source=path)
    return data or {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_skill_config(path: Path = SKILL_CONFIG_PATH) -> dict:
    config = read_yaml(path)
    if not isinstance(config, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return config


def iter_content_entries(
    content_root: Path = CONTENT_ROOT,
    config: dict | None = None,
) -> list[tuple[str, Path, dict]]:
    """Return reviewed content entries without consulting generated manifests."""

    config = config or load_skill_config()
    entries: list[tuple[str, Path, dict]] = []
    for skill_key, skill in config.get("skills", {}).items():
        content_dir = content_root / skill["content_dir"]
        for path in sorted(content_dir.rglob("*.yaml")):
            data = read_yaml(path)
            entries.append((skill_key, path, data))
    return entries


def content_entries_by_skill(
    content_root: Path = CONTENT_ROOT,
    config: dict | None = None,
) -> dict[str, list[dict]]:
    config = config or load_skill_config()
    grouped = {skill_key: [] for skill_key in config.get("skills", {})}
    for skill_key, path, data in iter_content_entries(content_root, config):
        entry = dict(data)
        entry["_source_path"] = path
        grouped[skill_key].append(entry)
    return grouped


def write_yaml(path: Path, data: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=100),
        encoding="utf-8",
    )


def pretty_slug(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").strip().title()


def human_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def parse_markdown_title(path: Path) -> str:
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return pretty_slug(path.stem)


def relative_to_repo(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def relative_to_stata(path: Path) -> str:
    return str(path.relative_to(STATA_ROOT))


@lru_cache(maxsize=1)
def help_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if not STATA_ADO_BASE.exists():
        return index
    for path in STATA_ADO_BASE.rglob("*"):
        if path.suffix.lower() != ".sthlp":
            continue
        index.setdefault(path.stem.lower(), []).append(path)
    return index


def find_help_files_exact(
    exact_stems: list[str],
    declared_globs: list[str] | None = None,
) -> tuple[list[Path], list[str]]:
    """Resolve only exact .sthlp stems and explicitly declared .sthlp globs.

    The second return value contains selectors that matched nothing.  No
    normalization, prefix matching, or substring matching is permitted because
    those behaviors previously attached unrelated help files to curated topics.
    """

    if not STATA_ADO_BASE.exists():
        return [], [*exact_stems, *(declared_globs or [])]

    matches: list[Path] = []
    missing: list[str] = []
    for stem in exact_stems:
        selector = stem.strip()
        if not selector:
            missing.append(stem)
            continue
        if any(token in selector for token in ("*", "?", "[", "]", "/", "\\")):
            missing.append(selector)
            continue
        exact = sorted(help_index().get(selector.lower(), []))
        if exact:
            matches.extend(exact)
        else:
            missing.append(selector)

    for pattern in declared_globs or []:
        if (
            not pattern
            or Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
            or not pattern.endswith(".sthlp")
            or not any(token in pattern for token in ("*", "?", "["))
        ):
            missing.append(pattern)
            continue
        resolved = sorted(path for path in STATA_ADO_BASE.glob(pattern) if path.is_file())
        if resolved:
            matches.extend(resolved)
        else:
            missing.append(pattern)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(matches):
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique, missing


def find_help_files_for_topic(topic: str) -> list[Path]:
    """Backward-compatible exact resolver; fuzzy matching is intentionally gone."""

    files, _ = find_help_files_exact([topic])
    return files


def unique_list(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            output.append(item)
            seen.add(item)
    return output


def strip_smcl_markup(text: str) -> str:
    patterns = [
        (r"\{smcl\}", ""),
        (r"\{\.\.\.\}", ""),
        (r"\{cmd:([^}]*)\}", r"\1"),
        (r"\{cmd ([^}]*)\}", r"\1"),
        (r"\{it:([^}]*)\}", r"\1"),
        (r"\{bf:([^}]*)\}", r"\1"),
        (r"\{ul:([^}]*)\}", r"\1"),
        (r"\{hi:([^}]*)\}", r"\1"),
        (r"\{helpb? ([^}:]+):([^}]*)\}", r"\2"),
        (r'\{browse "([^"]+)":([^}]*)\}', r"\2 (\1)"),
        (r"\{mansection [^:]+:([^}]*)\}", r"\1"),
        (r"\{title:([^}]*)\}", r"\1"),
        (r"\{marker [^}]*\}", ""),
        (r"\{p[0-9a-z ]*\}", ""),
        (r"\{hline [^}]*\}", ""),
        (r"\{c [^}]*\}", ""),
    ]
    cleaned = text
    for pattern, replacement in patterns:
        cleaned = re.sub(pattern, replacement, cleaned)
    cleaned = re.sub(r"\{[^}]*\}", "", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_syntax_patterns(text: str, limit: int = 6) -> list[str]:
    if not text:
        return []
    lines = [line.rstrip() for line in text.splitlines()]
    patterns: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower == "syntax":
            capture = True
            continue
        if capture:
            if not stripped:
                if patterns:
                    break
                continue
            if lower.startswith("description") or lower.startswith("remarks") or lower.startswith("options"):
                break
            if len(stripped) > 120 and "[" not in stripped and "," not in stripped:
                continue
            patterns.append(stripped)
            if len(patterns) >= limit:
                break
    if patterns:
        return unique_list(patterns)

    fallback: list[str] = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) < 6 or len(stripped) > 100:
            continue
        if "," in stripped or "[" in stripped or "(" in stripped:
            fallback.append(stripped)
        if len(fallback) >= limit:
            break
    return unique_list(fallback)


def extract_warning_lines(text: str, limit: int = 4) -> list[str]:
    warnings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if not stripped:
            continue
        if any(token in lower for token in ["warning", "note:", "must", "cannot", "do not", "be careful"]):
            warnings.append(stripped)
        if len(warnings) >= limit:
            break
    return unique_list(warnings)


def run_command(
    args: list[str],
    cwd: Path | None = None,
    timeout_seconds: int = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode("utf-8", "ignore")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            error.stderr.decode("utf-8", "ignore")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        timeout_message = f"Command timed out after {timeout_seconds} seconds."
        stderr = f"{stderr}\n{timeout_message}".strip()
        return subprocess.CompletedProcess(args, 124, stdout, stderr)


def detect_stata_binary() -> Path | None:
    candidates = [
        STATA_ROOT / "StataBE.app" / "Contents" / "MacOS" / "StataBE",
        STATA_ROOT / "StataSE.app" / "Contents" / "MacOS" / "StataSE",
        STATA_ROOT / "StataMP.app" / "Contents" / "MacOS" / "StataMP",
        STATA_ROOT / "Stata.app" / "Contents" / "MacOS" / "Stata",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _signal_process_group(
    process: subprocess.Popen[str],
    process_signal: signal.Signals,
) -> bool:
    # poll() either confirms the leader is still unreaped (so its PID cannot
    # yet be reused) or reaps it and lets us avoid signaling a stale PGID.
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, process_signal)
    except ProcessLookupError:
        return True
    except PermissionError:
        # macOS can report EPERM for a vanished group after its leader is
        # already reaped. Treat that state as ambiguous rather than success;
        # a live leader remains a real cleanup failure.
        if process.poll() is None:
            raise
        return False
    return True


def _normalize_process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _merge_process_text(
    first: str | bytes | None,
    second: str | bytes | None,
) -> str:
    first_text = _normalize_process_text(first)
    second_text = _normalize_process_text(second)
    if not first_text:
        return second_text
    if not second_text:
        return first_text
    if second_text.startswith(first_text):
        return second_text
    if first_text.startswith(second_text):
        return first_text
    return f"{first_text}\n{second_text}"


def _close_process_pipes(
    process: subprocess.Popen[str],
    *,
    suppress_base_exceptions: bool = False,
) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            except BaseException:
                if not suppress_base_exceptions:
                    raise


def _bounded_process_wait(process: subprocess.Popen[str]) -> bool:
    try:
        process.wait(timeout=STATA_PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _force_cleanup_process(process: subprocess.Popen[str]) -> None:
    """Best-effort cleanup that never replaces an active caller exception."""

    try:
        _signal_process_group(process, signal.SIGKILL)
    except BaseException:
        pass
    try:
        _close_process_pipes(
            process,
            suppress_base_exceptions=True,
        )
    except BaseException:
        pass
    try:
        _bounded_process_wait(process)
    except BaseException:
        pass


def _stop_process_group(
    process: subprocess.Popen[str],
) -> _ProcessStopResult:
    """Stop one group and collect output without trusting inherited pipe EOF."""

    try:
        term_confirmed = _signal_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(
                timeout=STATA_PROCESS_CLEANUP_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as first_timeout:
            kill_confirmed = _signal_process_group(
                process,
                signal.SIGKILL,
            )
            try:
                stdout, stderr = process.communicate(
                    timeout=STATA_PROCESS_CLEANUP_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as second_timeout:
                stdout = _merge_process_text(
                    first_timeout.output,
                    second_timeout.output,
                )
                stderr = _merge_process_text(
                    first_timeout.stderr,
                    second_timeout.stderr,
                )
                _close_process_pipes(process)
                reaped = _bounded_process_wait(process)
                note = (
                    "Post-kill pipe closure timed out; closed local pipes and "
                    "used a bounded process reap."
                )
                if not reaped:
                    note += (
                        " Could not confirm process reap within the bounded "
                        "cleanup window."
                    )
                return _ProcessStopResult(
                    stdout=stdout,
                    stderr=stderr,
                    diagnostic=note,
                    cleanup_confirmed=False,
                )
            diagnostic = (
                ""
                if kill_confirmed
                else (
                    "Could not confirm process-group termination after the "
                    "leader exited."
                )
            )
            return _ProcessStopResult(
                stdout=_merge_process_text(first_timeout.output, stdout),
                stderr=_merge_process_text(first_timeout.stderr, stderr),
                diagnostic=diagnostic,
                cleanup_confirmed=kill_confirmed,
            )
        else:
            diagnostic = (
                ""
                if term_confirmed
                else (
                    "Could not confirm process-group termination after the "
                    "leader exited."
                )
            )
            return _ProcessStopResult(
                stdout=_normalize_process_text(stdout),
                stderr=_normalize_process_text(stderr),
                diagnostic=diagnostic,
                cleanup_confirmed=term_confirmed,
            )
    except BaseException:
        _force_cleanup_process(process)
        raise


def stata_containment_status() -> tuple[bool, str]:
    """Report whether the fixed Stata containment profile can be applied."""

    if sys.platform != "darwin":
        return False, "licensed Stata containment requires macOS"
    if not MACOS_SANDBOX_EXEC.is_file():
        return False, f"missing containment executable: {MACOS_SANDBOX_EXEC}"
    try:
        probe = subprocess.run(
            [
                str(MACOS_SANDBOX_EXEC),
                "-p",
                STATA_SANDBOX_PROFILE,
                "/usr/bin/true",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=STATA_SANDBOX_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, "containment probe timed out"
    except OSError as error:
        return False, f"containment probe could not start: {error}"
    if probe.returncode != 0:
        return False, "sandbox-exec could not apply the fixed containment profile"
    return True, ""


def _stata_launch_command(
    stata_binary: Path,
    child_do_file: Path,
) -> list[str]:
    containment_available, reason = stata_containment_status()
    if not containment_available:
        raise OSError(
            "Licensed Stata validation requires usable macOS sandbox-exec "
            f"process containment: {reason}."
        )
    return [
        str(MACOS_SANDBOX_EXEC),
        "-p",
        STATA_SANDBOX_PROFILE,
        str(stata_binary),
        "-e",
        "do",
        str(child_do_file),
    ]


def run_stata_do(
    stata_binary: Path,
    do_file: Path,
    cwd: Path,
    completion_marker: str,
    timeout_seconds: int = 300,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run one Stata do-file and require its exact, run-specific marker.

    Stata's macOS ``-e do`` mode names its plain-text log after the do-file in
    the process working directory.  Validation deliberately accepts only that
    path: stale logs elsewhere (especially in the repository root) are never
    candidates.
    """
    ensure_dir(cwd)
    log_path = cwd / f"{do_file.stem}.log"
    if log_path.exists():
        log_path.unlink()

    child_do_file = Path(os.path.relpath(do_file, start=cwd))
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        _stata_launch_command(stata_binary, child_do_file),
        cwd=str(cwd),
        env={**os.environ, "PWD": str(cwd)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    marker_found = False
    timed_out = False
    stopped_after_marker = False
    try:
        while True:
            if log_path.exists():
                log_text = read_text(log_path)
                marker_found = any(
                    line.strip() == completion_marker
                    for line in log_text.splitlines()
                )
                if marker_found:
                    break
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(0.1, remaining))
    except BaseException:
        _force_cleanup_process(process)
        raise

    stopped_after_marker = marker_found and process.poll() is None
    stop_result = _stop_process_group(process)

    log_text = read_text(log_path) if log_path.exists() else ""
    marker_found = any(line.strip() == completion_marker for line in log_text.splitlines())
    diagnostics: list[str] = []
    process_returncode = process.returncode if process.returncode is not None else 1
    if not log_path.exists():
        diagnostics.append(f"Stata did not create the expected log {log_path.name}.")
    elif not marker_found:
        diagnostics.append("Stata log did not contain the exact completion marker.")

    if timed_out:
        effective_returncode = 124
        diagnostics.append(f"Stata timed out after {timeout_seconds} seconds.")
    elif not stop_result.cleanup_confirmed:
        effective_returncode = 1
    elif process_returncode != 0 and not stopped_after_marker:
        effective_returncode = process_returncode
    elif not log_path.exists():
        effective_returncode = 1
    elif not marker_found:
        effective_returncode = 1
    else:
        effective_returncode = 0

    stderr_parts = [
        stop_result.stderr,
        stop_result.diagnostic,
        *diagnostics,
    ]
    effective_stderr = "\n".join(part for part in stderr_parts if part).strip()
    result = subprocess.CompletedProcess(
        process.args,
        effective_returncode,
        stop_result.stdout,
        effective_stderr,
    )
    return result, log_path


def has_stata_error(log_text: str) -> bool:
    return bool(re.search(r"(?m)^\s*r\([0-9]+\);", log_text))


def download_text(url: str, timeout_seconds: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", "ignore")


def download_binary(
    url: str,
    dest: Path,
    timeout_seconds: int = 30,
    expected_sha256: str | None = None,
) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        data = response.read()

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise ValueError(
            f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual_sha256}"
        )

    ensure_dir(dest.parent)
    dest.write_bytes(data)
    return actual_sha256
