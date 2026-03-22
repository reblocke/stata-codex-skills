from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_ROOT = REPO_ROOT / "content"
MANIFEST_ROOT = REPO_ROOT / "manifests"
RAW_ROOT = REPO_ROOT / "raw"
BUILD_ROOT = REPO_ROOT / "build" / "generated"
TEMPLATES_ROOT = REPO_ROOT / "templates"
TESTS_ROOT = REPO_ROOT / "tests"
UPSTREAM_REPO_URL = "https://github.com/dylantmoore/stata-skill.git"
UPSTREAM_REPO_DIR = RAW_ROOT / "upstream" / "stata-skill"
STATA_ROOT = Path("/Applications/Stata")
STATA_ADO_BASE = STATA_ROOT / "ado" / "base"
DEFAULT_SKILLS_DIR = Path.home() / ".codex" / "skills"


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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


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


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@lru_cache(maxsize=1)
def help_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if not STATA_ADO_BASE.exists():
        return index
    for path in STATA_ADO_BASE.rglob("*"):
        if path.suffix.lower() not in {".sthlp", ".hlp"}:
            continue
        index.setdefault(path.stem.lower(), []).append(path)
    return index


def find_help_files_for_topic(topic: str) -> list[Path]:
    if not STATA_ADO_BASE.exists():
        return []
    topic = topic.strip()
    if not topic:
        return []
    if "*" in topic or "?" in topic:
        return sorted(STATA_ADO_BASE.rglob(topic))

    exact = sorted(help_index().get(topic.lower(), []))
    if exact:
        return exact

    wanted = normalized_key(topic)
    matches: list[Path] = []
    for stem, paths in help_index().items():
        normalized_stem = normalized_key(stem)
        if normalized_stem == wanted or normalized_stem.startswith(wanted) or wanted in normalized_stem:
            matches.extend(paths)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(matches):
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


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


def run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
    )


def copy_tree_fresh(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


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


def run_stata_do(
    stata_binary: Path,
    do_file: Path,
    cwd: Path,
    completion_marker: str = "VALIDATION COMPLETE",
    timeout_seconds: int = 300,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    process = subprocess.Popen(
        [str(stata_binary), "-b", "do", str(do_file)],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    candidate_logs = [
        cwd / f"{do_file.stem}.log",
        REPO_ROOT / f"{do_file.stem}.log",
        do_file.parent / f"{do_file.stem}.log",
    ]
    log_path = candidate_logs[0]
    marker_found = False
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        for candidate in candidate_logs:
            if candidate.exists():
                log_path = candidate
                break
        if log_path.exists() and completion_marker in read_text(log_path):
            marker_found = True
            break
        if process.poll() is not None:
            break
        time.sleep(1)

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    stdout, stderr = process.communicate()
    effective_returncode = 0 if marker_found else (process.returncode if process.returncode is not None else 1)
    result = subprocess.CompletedProcess(process.args, effective_returncode, stdout, stderr)
    return result, log_path


def has_stata_error(log_text: str) -> bool:
    return bool(re.search(r"(?m)^r\([0-9]+\);", log_text))


def download_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request) as response:
        return response.read().decode("utf-8", "ignore")


def download_binary(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request) as response:
        ensure_dir(dest.parent)
        dest.write_bytes(response.read())
