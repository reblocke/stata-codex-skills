from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
from enum import Enum
from functools import lru_cache
import hashlib
import io
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from process_guard.authorization import allow_detached_process
from runtime_guard import (
    REQUIRED_PYTHON,
    REQUIRED_UNICODE_VERSION,
    require_supported_runtime,
    runtime_compatibility_error,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_ROOT = REPO_ROOT / "content"
CONFIG_ROOT = REPO_ROOT / "config"
LOCK_ROOT = REPO_ROOT / "locks"
PACKAGE_LOCK_ROOT = LOCK_ROOT / "packages"
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
PROCESS_SIGNAL_EXIT_RACE_TIMEOUT_SECONDS = 0.25
MACOS_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
STATA_SANDBOX_PROFILE = (
    "(version 1)"
    "(allow default)"
    "(deny process-fork)"
    "(deny lsopen)"
    "(deny appleevent-send)"
)
STATA_SANDBOX_PROBE_TIMEOUT_SECONDS = 5
SAFE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
BINARY_DOWNLOAD_CHUNK_BYTES = 64 * 1024
DEFAULT_BINARY_DOWNLOAD_MAX_BYTES = 1024 * 1024
STATA_HELP_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_STATA_HELP_MAX_BYTES = 64 * 1024 * 1024
STATA_HELP_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
STATA_HELP_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True)
class _ProcessStopResult:
    stdout: str
    stderr: str
    diagnostic: str
    cleanup_confirmed: bool
    leader_kill_sent: bool
    permission_denied_after_anchored_exit: bool = False


@dataclass(frozen=True)
class _ProcessGroupSignalResult:
    cleanup_confirmed: bool
    live_leader_signaled: bool
    permission_denied_after_anchored_exit: bool = False


class _ProcessLeaderState(Enum):
    LIVE_ANCHORED = "live-anchored"
    EXITED_ANCHORED = "exited-anchored"
    UNANCHORED = "unanchored"


class _DarwinSiginfo(ctypes.Structure):
    """Darwin siginfo_t layout from <sys/signal.h>."""

    _fields_ = [
        ("si_signo", ctypes.c_int),
        ("si_errno", ctypes.c_int),
        ("si_code", ctypes.c_int),
        ("si_pid", ctypes.c_int),
        ("si_uid", ctypes.c_uint),
        ("si_status", ctypes.c_int),
        ("si_addr", ctypes.c_void_p),
        ("si_value", ctypes.c_void_p),
        ("si_band", ctypes.c_long),
        ("_pad", ctypes.c_ulong * 7),
    ]


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


def is_safe_slug(value: object) -> bool:
    """Return whether a value is an authorized canonical slug component."""

    return isinstance(value, str) and SAFE_SLUG_RE.fullmatch(value) is not None


def atomic_rename_at_no_replace(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
    *,
    sync_directories: bool = True,
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
    if sync_directories:
        os.fsync(destination_descriptor)
        if source_descriptor != destination_descriptor:
            os.fsync(source_descriptor)


def atomic_exchange_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
    *,
    sync_directories: bool = True,
) -> None:
    """Atomically exchange two descriptor-relative entries."""

    for label, name in (
        ("source", source_name),
        ("destination", destination_name),
    ):
        if not name or name in {".", ".."} or Path(name).name != name:
            raise ValueError(
                f"atomic exchange {label} must be one path component"
            )

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename_exchange = libc.renameatx_np
        except AttributeError as error:
            raise RuntimeError(
                "This macOS runtime lacks descriptor-relative atomic "
                "exchange support"
            ) from error
        flag = 0x00000002  # RENAME_SWAP from <sys/stdio.h>
    elif sys.platform.startswith("linux"):
        try:
            rename_exchange = libc.renameat2
        except AttributeError as error:
            raise RuntimeError(
                "This Linux runtime lacks descriptor-relative atomic "
                "exchange support"
            ) from error
        flag = 2  # RENAME_EXCHANGE from <linux/fs.h>
    else:
        raise RuntimeError(
            "Descriptor-relative atomic exchange is supported only on "
            "macOS and Linux"
        )
    rename_exchange.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exchange.restype = ctypes.c_int
    result = rename_exchange(
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
    if sync_directories:
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


def _stata_help_source_state(
    metadata: os.stat_result,
    relative: Path,
    *,
    max_bytes: int,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 0
        or metadata.st_size > max_bytes
    ):
        raise RuntimeError(
            f"Stata help source {relative} must be one bounded regular file "
            "with no hard links"
        )
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_stata_help_source_at(
    root_descriptor: int,
    relative: Path,
    *,
    max_bytes: int,
) -> tuple[int, tuple[int, int, int, int, int, int, int, int, int]]:
    current_descriptor = root_descriptor
    close_current = False
    try:
        for part in relative.parts[:-1]:
            next_descriptor = os.open(
                part,
                STATA_HELP_DIRECTORY_OPEN_FLAGS,
                dir_fd=current_descriptor,
            )
            try:
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeError(
                        f"Stata help source parent for {relative} is not a "
                        "real directory"
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            if close_current:
                try:
                    os.close(current_descriptor)
                except BaseException:
                    os.close(next_descriptor)
                    raise
            current_descriptor = next_descriptor
            close_current = True

        name = relative.name
        named_state = _stata_help_source_state(
            os.stat(
                name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            ),
            relative,
            max_bytes=max_bytes,
        )
        descriptor = os.open(
            name,
            STATA_HELP_FILE_OPEN_FLAGS,
            dir_fd=current_descriptor,
        )
        try:
            descriptor_state = _stata_help_source_state(
                os.fstat(descriptor),
                relative,
                max_bytes=max_bytes,
            )
        except BaseException:
            os.close(descriptor)
            raise
        if descriptor_state != named_state:
            os.close(descriptor)
            raise RuntimeError(
                f"Stata help source {relative} changed while being opened"
            )
        return descriptor, descriptor_state
    finally:
        if close_current:
            os.close(current_descriptor)


def _verify_stata_help_source_at(
    root_descriptor: int,
    relative: Path,
    owner_descriptor: int,
    expected: tuple[int, int, int, int, int, int, int, int, int],
    *,
    max_bytes: int,
) -> None:
    observed_descriptor, observed_state = _open_stata_help_source_at(
        root_descriptor,
        relative,
        max_bytes=max_bytes,
    )
    try:
        owner_state = _stata_help_source_state(
            os.fstat(owner_descriptor),
            relative,
            max_bytes=max_bytes,
        )
    finally:
        os.close(observed_descriptor)
    if owner_state != expected or observed_state != expected:
        raise RuntimeError(f"Stata help source {relative} changed while being read")


def sha256_stata_help_source(
    source: Path,
    *,
    help_root: Path | None = None,
    max_bytes: int = DEFAULT_STATA_HELP_MAX_BYTES,
) -> str:
    """Hash one stable real help file without following source path links."""

    if max_bytes <= 0:
        raise ValueError("Stata help source byte limit must be positive")
    if help_root is None:
        help_root = STATA_ADO_BASE
    absolute_root = Path(os.path.abspath(os.fspath(help_root)))
    absolute_source = Path(os.path.abspath(os.fspath(source)))
    try:
        relative = absolute_source.relative_to(absolute_root)
    except ValueError as error:
        raise RuntimeError(
            "Stata help source is outside the configured help root"
        ) from error
    if (
        not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix.lower() != ".sthlp"
    ):
        raise RuntimeError("Unsafe Stata help source path")

    root_descriptor: int | None = None
    source_descriptor: int | None = None
    observed_root_descriptor: int | None = None
    try:
        root_descriptor = os.open(
            absolute_root,
            STATA_HELP_DIRECTORY_OPEN_FLAGS,
        )
        root_metadata = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RuntimeError("Configured Stata help root is not a real directory")
        root_identity = (root_metadata.st_dev, root_metadata.st_ino)

        source_descriptor, expected = _open_stata_help_source_at(
            root_descriptor,
            relative,
            max_bytes=max_bytes,
        )
        _verify_stata_help_source_at(
            root_descriptor,
            relative,
            source_descriptor,
            expected,
            max_bytes=max_bytes,
        )

        digest = hashlib.sha256()
        remaining = expected[6]
        while remaining:
            chunk = os.read(
                source_descriptor,
                min(STATA_HELP_READ_CHUNK_BYTES, remaining),
            )
            if not chunk:
                raise RuntimeError(
                    f"Stata help source {relative} changed while being read"
                )
            digest.update(chunk)
            remaining -= len(chunk)

        if (
            _stata_help_source_state(
                os.fstat(source_descriptor),
                relative,
                max_bytes=max_bytes,
            )
            != expected
        ):
            raise RuntimeError(
                f"Stata help source {relative} changed while being read"
            )
        observed_root_descriptor = os.open(
            absolute_root,
            STATA_HELP_DIRECTORY_OPEN_FLAGS,
        )
        observed_root = os.fstat(observed_root_descriptor)
        if (
            not stat.S_ISDIR(observed_root.st_mode)
            or (observed_root.st_dev, observed_root.st_ino) != root_identity
        ):
            raise RuntimeError(
                "Configured Stata help root changed while a source was read"
            )
        _verify_stata_help_source_at(
            observed_root_descriptor,
            relative,
            source_descriptor,
            expected,
            max_bytes=max_bytes,
        )
        return digest.hexdigest()
    except OSError as error:
        raise RuntimeError(
            f"Could not safely read Stata help source {relative}"
        ) from error
    finally:
        for descriptor in (
            observed_root_descriptor,
            source_descriptor,
            root_descriptor,
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


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
    timeout_seconds: float = 120,
) -> subprocess.CompletedProcess[str]:
    with allow_detached_process():
        process = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        stop_result = _stop_process_group(process)
        timeout_message = (
            f"Command timed out after {timeout_seconds:g} seconds."
        )
        cleanup_diagnostic = stop_result.diagnostic
        if not stop_result.cleanup_confirmed and not cleanup_diagnostic:
            cleanup_diagnostic = (
                "Process-group cleanup could not be confirmed."
            )
        effective_stderr = "\n".join(
            part
            for part in (
                stop_result.stderr,
                timeout_message,
                cleanup_diagnostic,
            )
            if part
        ).strip()
        return subprocess.CompletedProcess(
            process.args,
            124,
            stop_result.stdout,
            effective_stderr,
        )
    except BaseException:
        _force_cleanup_process(process)
        raise
    return subprocess.CompletedProcess(
        process.args,
        process.returncode or 0,
        stdout,
        stderr,
    )


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
    *,
    fork_denial_guarantees_no_descendants: bool = False,
) -> _ProcessGroupSignalResult:
    # Confirm immediately before signaling that this Popen still owns a
    # waitable leader.  A reaped leader no longer reserves its PID, so its
    # former PGID is unsafe to signal.
    leader_state = _process_leader_state(process)
    if leader_state is _ProcessLeaderState.UNANCHORED:
        return _ProcessGroupSignalResult(False, False)

    def observe_anchored_exit() -> bool:
        deadline = (
            time.monotonic() + PROCESS_SIGNAL_EXIT_RACE_TIMEOUT_SECONDS
        )
        while True:
            observed_state = _process_leader_state(process)
            if observed_state is not _ProcessLeaderState.LIVE_ANCHORED:
                return observed_state is _ProcessLeaderState.EXITED_ANCHORED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    try:
        os.killpg(process.pid, process_signal)
    except ProcessLookupError:
        # ESRCH can race with a natural leader exit between the anchored state
        # observation and killpg().  Keep the waitable leader as the PID
        # anchor, never retry the signal, and briefly observe whether it reaches
        # the exited-but-unreaped state that proves the group disappeared.
        return _ProcessGroupSignalResult(
            observe_anchored_exit(),
            False,
        )
    except PermissionError:
        # EPERM does not prove that the group is empty: an exited leader can
        # leave unsignalable descendants behind.  Only the fixed Stata sandbox
        # may opt into the natural-exit observation because its successfully
        # probed profile denies process-fork before launch.
        anchored_exit = observe_anchored_exit()
        return _ProcessGroupSignalResult(
            (
                anchored_exit
                and fork_denial_guarantees_no_descendants
            ),
            False,
            permission_denied_after_anchored_exit=anchored_exit,
        )
    return _ProcessGroupSignalResult(
        cleanup_confirmed=True,
        live_leader_signaled=(
            leader_state is _ProcessLeaderState.LIVE_ANCHORED
        ),
    )


@lru_cache(maxsize=1)
def _darwin_waitid_function():
    """Return Darwin waitid for Python builds that do not expose os.waitid."""

    if sys.platform != "darwin":
        raise RuntimeError("Darwin waitid fallback requested off macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        waitid = libc.waitid
    except AttributeError as error:
        raise RuntimeError(
            "This macOS runtime lacks non-reaping waitid support"
        ) from error
    waitid.argtypes = [
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_DarwinSiginfo),
        ctypes.c_int,
    ]
    waitid.restype = ctypes.c_int
    return waitid


def _leader_exited_with_darwin_waitid(pid: int) -> bool:
    info = _DarwinSiginfo()
    ctypes.set_errno(0)
    result = _darwin_waitid_function()(
        1,  # P_PID
        pid,
        ctypes.byref(info),
        0x01 | 0x04 | 0x20,  # WNOHANG | WEXITED | WNOWAIT
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return info.si_pid != 0


def _leader_exited_without_reaping(pid: int) -> bool:
    native_waitid = getattr(os, "waitid", None)
    if native_waitid is not None:
        return (
            native_waitid(
                os.P_PID,
                pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            is not None
        )
    if sys.platform == "darwin":
        return _leader_exited_with_darwin_waitid(pid)
    raise RuntimeError(
        "This runtime lacks the non-reaping waitid support required for "
        "process-group cleanup"
    )


def _process_leader_state(
    process: subprocess.Popen[str],
) -> _ProcessLeaderState:
    """Observe leader state without releasing its PID for group reuse."""

    if process.returncode is not None or getattr(
        process,
        "_skillpack_process_group_anchor_lost",
        False,
    ):
        return _ProcessLeaderState.UNANCHORED
    try:
        leader_exited = _leader_exited_without_reaping(process.pid)
    except OSError as error:
        if not isinstance(error, ChildProcessError) and error.errno != errno.ECHILD:
            raise
        setattr(process, "_skillpack_process_group_anchor_lost", True)
        return _ProcessLeaderState.UNANCHORED
    if leader_exited:
        return _ProcessLeaderState.EXITED_ANCHORED
    return _ProcessLeaderState.LIVE_ANCHORED


def _normalize_process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


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
    *,
    fork_denial_guarantees_no_descendants: bool = False,
) -> _ProcessStopResult:
    """Signal one anchored group before reaping its leader and collect output."""

    try:
        cleanup_confirmed = False
        leader_kill_sent = False
        permission_denied_after_anchored_exit = False
        diagnostic = ""
        if (
            _process_leader_state(process)
            is _ProcessLeaderState.UNANCHORED
        ):
            diagnostic = (
                "The process-group leader was already reaped; cleanup cannot "
                "be verified without risking a reused process-group ID."
            )
        else:
            # Signal the whole group before communicate() can reap its leader.
            # A single uncatchable signal leaves no escape interval between a
            # nominally graceful signal and the enforced group stop.
            signal_result = _signal_process_group(
                process,
                signal.SIGKILL,
                fork_denial_guarantees_no_descendants=(
                    fork_denial_guarantees_no_descendants
                ),
            )
            cleanup_confirmed = signal_result.cleanup_confirmed
            leader_kill_sent = signal_result.live_leader_signaled
            permission_denied_after_anchored_exit = (
                signal_result.permission_denied_after_anchored_exit
            )
            if not cleanup_confirmed:
                diagnostic = "Could not confirm process-group termination."
        try:
            stdout, stderr = process.communicate(
                timeout=STATA_PROCESS_CLEANUP_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as timeout:
            stdout = _normalize_process_text(timeout.output)
            stderr = _normalize_process_text(timeout.stderr)
            _close_process_pipes(process)
            reaped = _bounded_process_wait(process)
            timeout_note = (
                "Post-kill pipe closure timed out; closed local pipes and "
                "used a bounded process reap."
            )
            if not reaped:
                timeout_note += (
                    " Could not confirm process reap within the bounded "
                    "cleanup window."
                )
            diagnostic = " ".join(
                part for part in (diagnostic, timeout_note) if part
            )
            return _ProcessStopResult(
                stdout=stdout,
                stderr=stderr,
                diagnostic=diagnostic,
                cleanup_confirmed=False,
                leader_kill_sent=leader_kill_sent,
            )
        return _ProcessStopResult(
            stdout=_normalize_process_text(stdout),
            stderr=_normalize_process_text(stderr),
            diagnostic=diagnostic,
            cleanup_confirmed=cleanup_confirmed,
            leader_kill_sent=leader_kill_sent,
            permission_denied_after_anchored_exit=(
                permission_denied_after_anchored_exit
            ),
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
    with allow_detached_process():
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
    try:
        while True:
            if log_path.exists():
                log_text = read_text(log_path)
                marker_found = any(
                    line.strip() == completion_marker
                    for line in log_text.splitlines()
                )
                if marker_found:
                    leader_state = _process_leader_state(process)
                    if leader_state is _ProcessLeaderState.UNANCHORED:
                        raise RuntimeError(
                            "Lost ownership of the Stata process-group leader "
                            "before cleanup."
                        )
                    break
            leader_state = _process_leader_state(process)
            if leader_state is _ProcessLeaderState.UNANCHORED:
                raise RuntimeError(
                    "Lost ownership of the Stata process-group leader before "
                    "cleanup."
                )
            if leader_state is _ProcessLeaderState.EXITED_ANCHORED:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(0.1, remaining))
    except BaseException:
        _force_cleanup_process(process)
        raise

    stop_result = _stop_process_group(
        process,
        fork_denial_guarantees_no_descendants=True,
    )

    log_text = read_text(log_path) if log_path.exists() else ""
    marker_found = any(line.strip() == completion_marker for line in log_text.splitlines())
    diagnostics: list[str] = []
    process_returncode = process.returncode if process.returncode is not None else 1
    killed_after_marker = (
        marker_found
        and stop_result.leader_kill_sent
        and process_returncode == -int(signal.SIGKILL)
    )
    if not log_path.exists():
        diagnostics.append(f"Stata did not create the expected log {log_path.name}.")
    elif not marker_found:
        diagnostics.append("Stata log did not contain the exact completion marker.")

    if timed_out:
        effective_returncode = 124
        diagnostics.append(f"Stata timed out after {timeout_seconds} seconds.")
    elif not stop_result.cleanup_confirmed:
        effective_returncode = 1
    elif process_returncode != 0 and not killed_after_marker:
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


def _set_response_read_timeout(response: object, timeout_seconds: float) -> None:
    """Bound the next urllib read by the remaining total deadline."""

    candidates = [response]
    visited: set[int] = set()
    for _ in range(6):
        if not candidates:
            break
        candidate = candidates.pop(0)
        if id(candidate) in visited:
            continue
        visited.add(id(candidate))
        settimeout = getattr(candidate, "settimeout", None)
        if callable(settimeout):
            settimeout(timeout_seconds)
            return
        for attribute in ("fp", "raw", "_sock", "sock"):
            nested = getattr(candidate, attribute, None)
            if nested is not None:
                candidates.append(nested)
    raise RuntimeError(
        "Download response does not expose a bounded socket timeout"
    )


def _remaining_download_time(
    deadline: float,
    timeout_seconds: float,
    url: str,
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"Download timed out after {timeout_seconds:g} seconds: {url}"
        )
    return remaining


def _read_response_once(response: object, size: int) -> bytes:
    """Read at most one buffered/raw receive before rechecking the deadline."""

    read_once = getattr(response, "read1", None)
    if callable(read_once):
        return read_once(size)
    read = getattr(response, "read", None)
    if not callable(read):
        raise TypeError("Download response does not expose a read method")
    # A one-byte fallback prevents a generic buffered read from internally
    # resetting the socket inactivity timeout while filling a large request.
    return read(1)


def download_binary(
    url: str,
    dest: Path,
    timeout_seconds: float = 30,
    expected_sha256: str | None = None,
    *,
    max_bytes: int = DEFAULT_BINARY_DOWNLOAD_MAX_BYTES,
) -> str:
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 1
    ):
        raise ValueError("max_bytes must be a positive integer")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number")

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    deadline = time.monotonic() + timeout_seconds
    digest = hashlib.sha256()
    bytes_received = 0
    ensure_dir(dest.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=dest.parent,
        prefix=f".{dest.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(
                request,
                timeout=_remaining_download_time(
                    deadline,
                    timeout_seconds,
                    url,
                ),
            ) as response:
                while True:
                    remaining = _remaining_download_time(
                        deadline,
                        timeout_seconds,
                        url,
                    )
                    _set_response_read_timeout(response, remaining)
                    try:
                        chunk = _read_response_once(
                            response,
                            min(
                                BINARY_DOWNLOAD_CHUNK_BYTES,
                                max_bytes - bytes_received + 1,
                            )
                        )
                    except TimeoutError as error:
                        raise TimeoutError(
                            f"Download timed out after "
                            f"{timeout_seconds:g} seconds: {url}"
                        ) from error
                    _remaining_download_time(
                        deadline,
                        timeout_seconds,
                        url,
                    )
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError(
                            f"Binary download returned non-bytes data: {url}"
                        )
                    bytes_received += len(chunk)
                    if bytes_received > max_bytes:
                        raise ValueError(
                            f"Download exceeds {max_bytes} byte limit: {url}"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        actual_sha256 = digest.hexdigest()
        if (
            expected_sha256
            and actual_sha256.lower() != expected_sha256.lower()
        ):
            raise ValueError(
                f"SHA-256 mismatch for {url}: expected "
                f"{expected_sha256}, got {actual_sha256}"
            )

        os.replace(temporary_path, dest)
        return actual_sha256
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
