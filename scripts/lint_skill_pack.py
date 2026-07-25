#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from pathlib import Path
import argparse
import re
import sys
import tempfile

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    MANIFEST_ROOT,
    PROMPT_CASES_PATH,
    REPO_ROOT,
    UPSTREAM_REPO_URL,
    iter_content_entries,
    load_skill_config,
    read_text,
    read_yaml,
)


REQUIRED_FIELDS = {
    "slug",
    "skill",
    "section",
    "order",
    "title",
    "trigger",
    "aliases",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "assumptions",
    "workflows",
    "validation_case",
    "validation_mode",
    "related_refs",
    "install_commands",
    "smoke_test",
    "provenance",
}
LIST_FIELDS = {
    "aliases",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "assumptions",
    "workflows",
    "related_refs",
    "install_commands",
}
NONEMPTY_LIST_FIELDS = {
    "aliases",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "assumptions",
    "workflows",
}
VALIDATION_MODES = {"stata", "compilation", "manual-review"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GENERIC_TEXT = {
    "check missing-value behavior option defaults and stored results before chaining commands",
    "start with the official help topics attached to then tailor the syntax to the current dataset",
    "prefer a small batch-mode smoke test before applying the command sequence to a large dataset",
    "package syntax and dependencies vary across versions verify the installed help file before finalizing code",
    "confirm the package and any dependencies are installed on the active adopath before writing code that uses it",
    "pair the package command with a small reproducible example before folding it into a larger do-file",
    "plan the interface and the validation path before writing plugin code or wrapper ado-files",
    "use batch-mode stata logs and compiler output together when debugging plugin failures",
    "a plugin crash terminates the stata session so treat every memory access and return code as high risk",
}
UNSAFE_INSTALL_PATTERNS = (
    (re.compile(r"(?i),\s*replace\b"), "must not use replace by default"),
    (re.compile(r"(?i)\badoupdate\b"), "must not update the active environment"),
    (re.compile(r"(?i)\bhttps?://[^\s\"')]+/(?:main|master)(?:/|$)"), "must pin GitHub sources"),
    (re.compile(r"(?i)\bhttp://"), "must use HTTPS"),
    (re.compile(r"^\s*!"), "must not invoke a shell"),
    (re.compile(r"(?i)^\s*shell\b"), "must not invoke a shell"),
)
READ_ONLY_PREFLIGHT_RE = re.compile(
    r"(?i)^(?:"
    r"which(?:\s|$)|"
    r"ado\s+describe(?:\s|$)|"
    r"adopath(?:\s|$)|"
    r"sysdir(?:\s|$)|"
    r"help(?:\s|$)|"
    r"search(?:\s|$)|"
    r"findit(?:\s|$)|"
    r"ssc\s+describe(?:\s|$)|"
    r"net\s+describe(?:\s|$)"
    r")"
)
MANIFEST_EXECUTABLE_FIELDS = {
    "section",
    "title",
    "trigger",
    "commands",
    "validation_case",
    "install_commands",
    "smoke_test",
}
TRACK_METADATA_FILES = {"stata.trk", "backup.trk"}


def normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def uncaptured_command(command: str) -> str:
    return re.sub(
        r"(?i)^\s*(?:(?:capture|quietly|noisily)\s+)+",
        "",
        command,
    ).strip()


def lint_config(config: dict) -> list[str]:
    errors: list[str] = []
    if config.get("schema_version") != 1:
        errors.append("config/skills.yaml: schema_version must be 1")
    expected_resolution = {
        "exact_stems_field": "provenance.local_help_topics",
        "explicit_globs_field": "provenance.local_help_globs",
        "resolved_files_field": "provenance.local_help_files",
        "source_topics_role": (
            "Reader-facing topic labels only; never used for filesystem matching."
        ),
    }
    if config.get("source_resolution") != expected_resolution:
        errors.append(
            "config/skills.yaml: source_resolution must make provenance selectors authoritative"
        )
    skills = config.get("skills")
    if not isinstance(skills, dict) or not skills:
        return [*errors, "config/skills.yaml: skills must be a nonempty mapping"]

    names: set[str] = set()
    folders: set[str] = set()
    content_dirs: set[str] = set()
    for skill_key, skill in skills.items():
        if not isinstance(skill, dict):
            errors.append(f"config/skills.yaml: skill {skill_key} must be a mapping")
            continue
        for field in (
            "name",
            "folder",
            "content_dir",
            "route_dir",
            "heading",
            "description",
            "summary",
            "section_order",
            "validation_modes",
            "rules",
            "critical_gotchas",
            "interface",
        ):
            if field not in skill:
                errors.append(f"config/skills.yaml: skill {skill_key} missing {field}")
        for field in ("heading", "description", "summary"):
            if not is_nonempty_string(skill.get(field)):
                errors.append(
                    f"config/skills.yaml: skill {skill_key} {field} must be nonempty"
                )
        for field, seen in (
            ("name", names),
            ("folder", folders),
            ("content_dir", content_dirs),
        ):
            value = skill.get(field)
            if not is_nonempty_string(value):
                errors.append(f"config/skills.yaml: skill {skill_key} {field} must be nonempty")
            elif value in seen:
                errors.append(f"config/skills.yaml: duplicate {field} {value}")
            else:
                seen.add(value)
        for field in ("folder", "content_dir", "route_dir"):
            value = skill.get(field)
            if isinstance(value, str) and (
                Path(value).is_absolute()
                or Path(value).name != value
                or value in {".", ".."}
            ):
                errors.append(
                    f"config/skills.yaml: skill {skill_key} {field} "
                    "must be one safe directory name"
                )
        sections = skill.get("section_order")
        if (
            not isinstance(sections, list)
            or not sections
            or any(not is_nonempty_string(section) for section in sections)
            or len(sections) != len(set(sections))
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} section_order must contain unique names"
            )
        modes = skill.get("validation_modes")
        if (
            not isinstance(modes, list)
            or not modes
            or not set(modes).issubset(VALIDATION_MODES)
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} has invalid validation_modes"
            )
        rules = skill.get("rules")
        if (
            not isinstance(rules, list)
            or not rules
            or any(not is_nonempty_string(rule) for rule in rules)
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} rules must be a "
                "nonempty string list"
            )
        gotchas = skill.get("critical_gotchas")
        if not isinstance(gotchas, list) or any(
            not isinstance(item, dict)
            or not is_nonempty_string(item.get("title"))
            or not is_nonempty_string(item.get("body"))
            for item in gotchas if isinstance(gotchas, list)
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} critical_gotchas "
                "must contain title/body mappings"
            )
        interface = skill.get("interface")
        if not isinstance(interface, dict) or any(
            not is_nonempty_string(interface.get(field))
            for field in ("display_name", "short_description", "default_prompt")
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} interface metadata "
                "is incomplete"
            )
    return errors


def lint_provenance(
    source_label: str,
    skill_key: str,
    provenance: object,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(provenance, dict):
        return [f"{source_label}: provenance must be a mapping"]
    for field in (
        "local_help_topics",
        "local_help_globs",
        "local_help_files",
        "upstream_files",
        "upstream_only",
        "package_only",
        "reviewed",
    ):
        if field not in provenance:
            errors.append(f"{source_label}: provenance missing {field}")
    if provenance.get("reviewed") is not True:
        errors.append(f"{source_label}: provenance.reviewed must be true")
    if not isinstance(provenance.get("upstream_only"), bool):
        errors.append(f"{source_label}: provenance.upstream_only must be boolean")
    if not isinstance(provenance.get("package_only"), bool):
        errors.append(f"{source_label}: provenance.package_only must be boolean")

    topics = provenance.get("local_help_topics", [])
    globs = provenance.get("local_help_globs", [])
    files = provenance.get("local_help_files", [])
    upstream = provenance.get("upstream_files", [])
    for field, values in (
        ("local_help_topics", topics),
        ("local_help_globs", globs),
        ("local_help_files", files),
        ("upstream_files", upstream),
    ):
        if not isinstance(values, list) or any(not is_nonempty_string(item) for item in values):
            errors.append(f"{source_label}: provenance.{field} must be a string list")
    if isinstance(topics, list):
        for topic in topics:
            if isinstance(topic, str) and (
                any(token in topic for token in ("*", "?", "[", "]", "/", "\\"))
                or topic.endswith(".sthlp")
            ):
                errors.append(
                    f"{source_label}: local help topic {topic!r} is not an exact .sthlp stem"
                )
    if isinstance(globs, list):
        for pattern in globs:
            if isinstance(pattern, str) and (
                not pattern.endswith(".sthlp")
                or not any(token in pattern for token in ("*", "?", "["))
                or Path(pattern).is_absolute()
                or ".." in Path(pattern).parts
            ):
                errors.append(
                    f"{source_label}: local_help_glob {pattern!r} must be a safe explicit .sthlp glob"
                )
    if isinstance(files, list):
        if len(files) != len(set(files)):
            errors.append(f"{source_label}: duplicate local_help_files")
        for value in files:
            if isinstance(value, str) and (
                not value.startswith("ado/base/")
                or not value.endswith(".sthlp")
                or Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                errors.append(f"{source_label}: unsafe local help path {value!r}")
    if isinstance(upstream, list):
        for value in upstream:
            if isinstance(value, str) and (
                Path(value).is_absolute() or ".." in Path(value).parts
            ):
                errors.append(f"{source_label}: unsafe upstream path {value!r}")

    source_bases = sum(
        (
            bool(files),
            provenance.get("upstream_only") is True,
            provenance.get("package_only") is True,
        )
    )
    if source_bases != 1:
        errors.append(
            f"{source_label}: exactly one source basis is required "
            "(local help, upstream_only, or package_only)"
        )
    if provenance.get("package_only") is True and skill_key != "packages":
        errors.append(f"{source_label}: package_only is valid only for package entries")
    return errors


def lint_entry(
    skill_key: str,
    path: Path,
    entry: object,
    skill: dict,
) -> list[str]:
    source_label = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
    if not isinstance(entry, dict):
        return [f"{source_label}: content must be a mapping"]
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(entry))
    for field in missing:
        errors.append(f"{source_label}: missing field {field}")
    if missing:
        return errors

    slug = entry.get("slug")
    if not is_nonempty_string(slug) or not re.fullmatch(
        r"[a-z0-9]+(?:[-_][a-z0-9]+)*", slug
    ):
        errors.append(f"{source_label}: invalid slug")
    elif path.stem != slug:
        errors.append(f"{source_label}: filename must match slug {slug}")
    if entry.get("skill") != skill_key:
        errors.append(f"{source_label}: skill must be {skill_key}")
    if entry.get("section") not in skill["section_order"]:
        errors.append(f"{source_label}: unrecognized section {entry.get('section')!r}")
    if not isinstance(entry.get("order"), int) or isinstance(entry.get("order"), bool):
        errors.append(f"{source_label}: order must be an integer")
    for field in ("title", "trigger", "validation_case"):
        if not is_nonempty_string(entry.get(field)):
            errors.append(f"{source_label}: {field} must be nonempty")
    for field in LIST_FIELDS:
        value = entry.get(field)
        if not isinstance(value, list) or any(not is_nonempty_string(item) for item in value):
            errors.append(f"{source_label}: {field} must be a string list")
        elif field in NONEMPTY_LIST_FIELDS and not value:
            errors.append(f"{source_label}: {field} must not be empty")
        elif len(value) != len(set(value)):
            errors.append(f"{source_label}: {field} contains duplicates")
    preflight = entry.get("preflight_commands", [])
    if not isinstance(preflight, list) or any(
        not is_nonempty_string(item) for item in preflight
    ):
        errors.append(f"{source_label}: preflight_commands must be a string list")
    elif any(
        not READ_ONLY_PREFLIGHT_RE.match(uncaptured_command(command))
        for command in preflight
    ):
        errors.append(
            f"{source_label}: preflight_commands must contain only read-only "
            "discovery commands"
        )

    mode = entry.get("validation_mode")
    if mode not in skill["validation_modes"]:
        errors.append(
            f"{source_label}: validation_mode {mode!r} is not allowed for {skill_key}"
        )
    smoke_test = entry.get("smoke_test")
    if smoke_test is not None and not is_nonempty_string(smoke_test):
        errors.append(f"{source_label}: smoke_test must be null or a nonempty string")
    if mode == "stata" and not is_nonempty_string(smoke_test):
        errors.append(f"{source_label}: stata validation requires smoke_test")
    if mode == "manual-review" and smoke_test:
        errors.append(f"{source_label}: manual-review entries must not claim an executable smoke_test")
    if mode == "compilation":
        case = normalized_text(str(entry.get("validation_case", "")))
        if not any(word in case for word in ("compile", "build")) or not any(
            word in case for word in ("assert", "verify", "confirm", "pass", "fail")
        ):
            errors.append(
                f"{source_label}: compilation validation_case must state a compile assertion"
            )
    if mode == "manual-review":
        case = normalized_text(str(entry.get("validation_case", "")))
        if not any(word in case for word in ("review", "inspect", "audit", "document", "compare")):
            errors.append(
                f"{source_label}: manual-review validation_case must state the review action"
            )

    for field in ("syntax_patterns", "gotchas", "assumptions", "workflows"):
        for value in entry.get(field, []):
            if not isinstance(value, str):
                continue
            normalized = normalized_text(value)
            if normalized in GENERIC_TEXT or len(normalized.split()) < 3:
                errors.append(f"{source_label}: {field} contains generic content {value!r}")
    validation_case = normalized_text(str(entry.get("validation_case", "")))
    if (
        validation_case in GENERIC_TEXT
        or validation_case.startswith("run a small batch mode example that exercises")
    ):
        errors.append(f"{source_label}: validation_case is generic")

    for command in [*preflight, *entry.get("install_commands", [])]:
        for pattern, message in UNSAFE_INSTALL_PATTERNS:
            if pattern.search(command):
                errors.append(f"{source_label}: install/preflight command {message}: {command}")

    errors.extend(
        lint_provenance(source_label, skill_key, entry.get("provenance"))
    )
    return errors


def lint_route_aliases(
    config: dict,
    slug_to_skill: dict[str, str],
    route_paths: set[str],
) -> list[str]:
    errors: list[str] = []
    aliases = config.get("route_aliases", [])
    if not isinstance(aliases, list):
        return ["config/skills.yaml: route_aliases must be a list"]
    seen: set[tuple[str, str]] = set()
    for alias in aliases:
        if not isinstance(alias, dict):
            errors.append("config/skills.yaml: route alias must be a mapping")
            continue
        for field in (
            "from_skill",
            "from_route",
            "aliases",
            "task_intent",
            "to_skill",
            "to_slug",
        ):
            if field not in alias:
                errors.append(f"config/skills.yaml: route alias missing {field}")
        key = (alias.get("from_skill"), alias.get("from_route"))
        if key in seen:
            errors.append(f"config/skills.yaml: duplicate route alias {key}")
        seen.add(key)
        if alias.get("from_skill") not in config["skills"]:
            errors.append(f"config/skills.yaml: unknown alias source skill {key[0]}")
        else:
            from_route = alias.get("from_route")
            source_route_dir = config["skills"][alias["from_skill"]]["route_dir"]
            if (
                not is_nonempty_string(from_route)
                or Path(str(from_route)).is_absolute()
                or ".." in Path(str(from_route)).parts
                or Path(str(from_route)).suffix != ".md"
                or Path(str(from_route)).parent != Path(source_route_dir)
            ):
                errors.append(
                    f"config/skills.yaml: alias route {from_route!r} must be "
                    f"a safe Markdown path under {source_route_dir}/"
                )
            qualified_from = (
                f"{config['skills'][alias['from_skill']]['name']}/"
                f"{from_route}"
            )
            if qualified_from in route_paths:
                errors.append(f"config/skills.yaml: alias shadows canonical route {key}")
        target_slug = alias.get("to_slug")
        if target_slug not in slug_to_skill:
            errors.append(f"config/skills.yaml: alias target {target_slug!r} does not exist")
        elif slug_to_skill[target_slug] != alias.get("to_skill"):
            errors.append(f"config/skills.yaml: alias target skill mismatch for {target_slug}")
        if not isinstance(alias.get("aliases"), list) or not alias.get("aliases"):
            errors.append(f"config/skills.yaml: alias {key} requires aliases")
        if not is_nonempty_string(alias.get("task_intent")):
            errors.append(f"config/skills.yaml: alias {key} requires task_intent")
    return errors


def lint_prompt_cases(
    config: dict,
    canonical_paths: set[str],
    alias_paths: set[str],
    prompt_path: Path = PROMPT_CASES_PATH,
) -> list[str]:
    errors: list[str] = []
    data = read_yaml(prompt_path)
    if data.get("schema_version") != 1 or not isinstance(data.get("cases"), list):
        return [f"{prompt_path}: expected schema_version 1 and cases list"]
    if prompt_path == PROMPT_CASES_PATH:
        from render_prompt_cases import BOUNDARY_CASES, canonical_cases

        expected_cases = [*canonical_cases(), *BOUNDARY_CASES]
        if data["cases"] != expected_cases:
            errors.append(
                f"{prompt_path}: structured cases are stale; "
                "run scripts/render_prompt_cases.py"
            )
    valid_skills = {skill["name"] for skill in config["skills"].values()}
    ids: set[str] = set()
    covered: Counter[str] = Counter()
    boundary_by_skill: Counter[str] = Counter()
    for index, case in enumerate(data["cases"]):
        label = f"{prompt_path}: case {index + 1}"
        if not isinstance(case, dict):
            errors.append(f"{label} must be a mapping")
            continue
        for field in (
            "id",
            "prompt",
            "expected_skill",
            "expected_refs",
            "forbidden_routes",
        ):
            if field not in case:
                errors.append(f"{label} missing {field}")
        case_id = case.get("id")
        if not is_nonempty_string(case_id) or case_id in ids:
            errors.append(f"{label} has invalid or duplicate id")
        else:
            ids.add(case_id)
        if not is_nonempty_string(case.get("prompt")):
            errors.append(f"{label} prompt must be nonempty")
        skill_name = case.get("expected_skill")
        if skill_name not in valid_skills:
            errors.append(f"{label} expected_skill is unknown")
        expected = case.get("expected_refs")
        forbidden = case.get("forbidden_routes")
        if not isinstance(expected, list) or not expected:
            errors.append(f"{label} expected_refs must be nonempty")
            expected = []
        if not isinstance(forbidden, list):
            errors.append(f"{label} forbidden_routes must be a list")
            forbidden = []
        for route in expected:
            if route not in canonical_paths:
                errors.append(f"{label} expected ref {route!r} is not canonical")
            else:
                covered[route] += 1
            if route in forbidden:
                errors.append(f"{label} route {route!r} is both expected and forbidden")
        for route in forbidden:
            if route not in canonical_paths | alias_paths:
                errors.append(f"{label} forbidden route {route!r} is unknown")
        if case.get("boundary") is True:
            if not forbidden:
                errors.append(f"{label} boundary case requires forbidden_routes")
            if skill_name in valid_skills:
                boundary_by_skill[skill_name] += 1
    for route in sorted(canonical_paths):
        if not covered[route]:
            errors.append(f"{prompt_path}: canonical route lacks prompt coverage: {route}")
    for skill_name in sorted(valid_skills):
        if not boundary_by_skill[skill_name]:
            errors.append(f"{prompt_path}: {skill_name} lacks a boundary case")
    return errors


def lint_upstream_lock(entries: list[tuple[str, Path, dict]]) -> list[str]:
    path = LOCK_ROOT / "upstream.yaml"
    lock = read_yaml(path)
    errors: list[str] = []
    if lock.get("schema_version") != 1:
        return [f"{path}: schema_version must be 1"]
    repository = lock.get("repository", {})
    if not isinstance(repository, dict):
        return [f"{path}: repository must be a mapping"]
    commit = str(repository.get("commit", ""))
    expected_commit = str(repository.get("expected_commit", ""))
    if not COMMIT_RE.fullmatch(commit):
        errors.append(f"{path}: repository.commit must be a full Git SHA")
    if not COMMIT_RE.fullmatch(expected_commit):
        errors.append(f"{path}: repository.expected_commit must be a full Git SHA")
    elif expected_commit != commit:
        errors.append(
            f"{path}: repository commit drift requires explicit lock review"
        )
    if repository.get("url") != UPSTREAM_REPO_URL:
        errors.append(
            f"{path}: repository.url must exactly match the configured upstream "
            f"repository {UPSTREAM_REPO_URL}"
        )
    files = lock.get("files")
    if not isinstance(files, dict):
        return [*errors, f"{path}: files must be a mapping"]
    declared = {
        upstream_path
        for _, _, entry in entries
        for upstream_path in entry.get("provenance", {}).get("upstream_files", [])
    }
    if set(files) != declared:
        missing = sorted(declared - set(files))
        extra = sorted(set(files) - declared)
        if missing:
            errors.append(f"{path}: missing declared upstream files: {', '.join(missing)}")
        if extra:
            errors.append(f"{path}: orphan upstream files: {', '.join(extra)}")
    for file_path, metadata in files.items():
        if not isinstance(metadata, dict) or not SHA256_RE.fullmatch(
            str(metadata.get("sha256", ""))
        ):
            errors.append(f"{path}: {file_path} requires sha256")
    return errors


def lint_stata_help_lock(entries: list[tuple[str, Path, dict]]) -> list[str]:
    path = LOCK_ROOT / "stata-help.yaml"
    lock = read_yaml(path)
    errors: list[str] = []
    if lock.get("schema_version") != 1:
        return [f"{path}: schema_version must be 1"]
    release = lock.get("stata_release")
    if not isinstance(release, dict):
        errors.append(f"{path}: stata_release must be a mapping")
    else:
        for field in ("edition", "bundle_identifier", "bundle_version", "executable"):
            if not is_nonempty_string(release.get(field)):
                errors.append(f"{path}: stata_release missing {field}")
    selectors = lock.get("selectors")
    files = lock.get("files")
    if not isinstance(selectors, dict) or not isinstance(files, dict):
        return [*errors, f"{path}: selectors and files must be mappings"]

    expected_keys: set[str] = set()
    declared_files: set[str] = set()
    for skill_key, _, entry in entries:
        provenance = entry.get("provenance", {})
        local_files = provenance.get("local_help_files", [])
        if not local_files:
            continue
        key = f"{skill_key}/{entry.get('slug')}"
        expected_keys.add(key)
        selected = selectors.get(key)
        if not isinstance(selected, dict):
            errors.append(f"{path}: missing selector lock for {key}")
            continue
        expected_topics = provenance.get("local_help_topics", [])
        expected_globs = provenance.get("local_help_globs", [])
        if selected.get("exact_stems") != expected_topics:
            errors.append(f"{path}: exact_stems drift for {key}")
        if selected.get("globs", []) != expected_globs:
            errors.append(f"{path}: globs drift for {key}")
        if selected.get("files") != local_files:
            errors.append(f"{path}: resolved files drift for {key}")
        declared_files.update(local_files)
    if set(selectors) != expected_keys:
        extra = sorted(set(selectors) - expected_keys)
        if extra:
            errors.append(f"{path}: orphan selector locks: {', '.join(extra)}")
    if set(files) != declared_files:
        missing = sorted(declared_files - set(files))
        extra = sorted(set(files) - declared_files)
        if missing:
            errors.append(f"{path}: missing help file hashes: {', '.join(missing)}")
        if extra:
            errors.append(f"{path}: orphan help file hashes: {', '.join(extra)}")
    for file_path, metadata in files.items():
        if not isinstance(metadata, dict) or not SHA256_RE.fullmatch(
            str(metadata.get("sha256", ""))
        ):
            errors.append(f"{path}: {file_path} requires sha256")
    return errors


def lint_distribution_locks(
    entries: list[tuple[str, Path, dict]],
) -> list[str]:
    errors: list[str] = []
    package_path = LOCK_ROOT / "packages.yaml"
    lock = read_yaml(package_path)
    packages = lock.get("packages")
    if lock.get("schema_version") != 1 or not isinstance(packages, dict):
        errors.append(f"{package_path}: invalid or missing package lock schema")
    else:
        required = {
            entry["slug"]
            for skill_key, _, entry in entries
            if skill_key == "packages" and entry.get("install_commands")
        }
        missing = sorted(required - set(packages))
        extra = sorted(set(packages) - required)
        if missing:
            errors.append(
                f"{package_path}: missing installable packages: {', '.join(missing)}"
            )
        if extra:
            errors.append(
                f"{package_path}: orphan package locks: {', '.join(extra)}"
            )
        for slug, package in packages.items():
            if not isinstance(package, dict):
                errors.append(f"{package_path}: {slug} must be a mapping")
                continue
            distributions = package.get("distributions")
            if not isinstance(distributions, list) or not distributions:
                errors.append(
                    f"{package_path}: {slug} distributions must be a nonempty list"
                )
                continue
            seen_descriptors: set[str] = set()
            for index, distribution in enumerate(distributions):
                label = f"{package_path}: {slug} distribution {index + 1}"
                if not isinstance(distribution, dict):
                    errors.append(f"{label} must be a mapping")
                    continue
                for field in ("source", "descriptor", "distribution_date", "files"):
                    if field not in distribution:
                        errors.append(f"{label} missing {field}")
                if not re.match(
                    r"^https?://", str(distribution.get("source", ""))
                ):
                    errors.append(f"{label} source must be an HTTP(S) provenance URL")
                descriptor = distribution.get("descriptor")
                if not is_nonempty_string(descriptor) or descriptor in seen_descriptors:
                    errors.append(f"{label} descriptor must be unique and nonempty")
                else:
                    seen_descriptors.add(descriptor)
                if not re.fullmatch(
                    r"\d{8}", str(distribution.get("distribution_date", ""))
                ):
                    errors.append(f"{label} distribution_date must be YYYYMMDD")
                file_map = distribution.get("files", {})
                if not isinstance(file_map, dict) or not file_map:
                    errors.append(f"{label} files must be nonempty")
                else:
                    for relative, sha in file_map.items():
                        if (
                            Path(relative).is_absolute()
                            or ".." in Path(relative).parts
                            or relative in TRACK_METADATA_FILES
                        ):
                            errors.append(f"{label} unsafe locked path {relative}")
                        if not SHA256_RE.fullmatch(str(sha)):
                            errors.append(f"{label} {relative} invalid sha256")
            generated_files = package.get("generated_files", {})
            if not isinstance(generated_files, dict):
                errors.append(f"{package_path}: {slug} generated_files must be a mapping")
            else:
                for relative, sha in generated_files.items():
                    if (
                        Path(relative).is_absolute()
                        or ".." in Path(relative).parts
                        or relative in TRACK_METADATA_FILES
                    ):
                        errors.append(
                            f"{package_path}: {slug} unsafe generated path {relative}"
                        )
                    if not SHA256_RE.fullmatch(str(sha)):
                        errors.append(
                            f"{package_path}: {slug}/{relative} invalid generated sha256"
                        )

    sdk_path = LOCK_ROOT / "plugin-sdk.yaml"
    lock = read_yaml(sdk_path)
    sources = lock.get("sources")
    if lock.get("schema_version") != 1 or not isinstance(sources, list) or not sources:
        errors.append(f"{sdk_path}: invalid or missing plugin SDK lock schema")
    else:
        names: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                errors.append(f"{sdk_path}: source must be a mapping")
                continue
            name = source.get("filename")
            if (
                not is_nonempty_string(name)
                or Path(str(name)).name != name
                or name in names
            ):
                errors.append(f"{sdk_path}: duplicate or invalid filename")
            else:
                names.add(name)
            if not str(source.get("url", "")).startswith("https://"):
                errors.append(f"{sdk_path}: {name} URL must use HTTPS")
            if not SHA256_RE.fullmatch(str(source.get("sha256", ""))):
                errors.append(f"{sdk_path}: {name} invalid sha256")
    return errors


def lint_manifests(entries: list[tuple[str, Path, dict]]) -> list[str]:
    errors: list[str] = []
    by_skill: dict[str, set[str]] = {}
    for skill_key, _, entry in entries:
        by_skill.setdefault(skill_key, set()).add(entry.get("slug"))
    names = {
        "core": "topic-map.yaml",
        "packages": "package-map.yaml",
        "plugins": "plugin-map.yaml",
    }
    for skill_key, filename in names.items():
        path = MANIFEST_ROOT / filename
        data = read_yaml(path)
        if data.get("schema_version") != 2 or data.get("role") != "provenance-lock-index":
            errors.append(f"{path}: manifest must be schema v2 provenance-lock-index")
            continue
        manifest_entries = data.get("entries")
        if not isinstance(manifest_entries, list):
            errors.append(f"{path}: entries must be a list")
            continue
        slugs: set[str] = set()
        for entry in manifest_entries:
            if not isinstance(entry, dict):
                errors.append(f"{path}: entry must be a mapping")
                continue
            forbidden = MANIFEST_EXECUTABLE_FIELDS & set(entry)
            if forbidden:
                errors.append(
                    f"{path}: executable fields are forbidden: {', '.join(sorted(forbidden))}"
                )
            slugs.add(entry.get("slug"))
        if slugs != by_skill.get(skill_key, set()):
            errors.append(f"{path}: provenance entries do not match canonical content slugs")
    return errors


def tree_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def lint_generated_drift() -> list[str]:
    if not BUILD_ROOT.exists():
        return []
    from render_skills import render_all

    with tempfile.TemporaryDirectory(prefix="stata-render-check-") as temp_root:
        expected_root = Path(temp_root) / "generated"
        try:
            render_all(output_root=expected_root)
        except Exception as error:
            return [f"generated render failed: {error}"]
        expected = tree_snapshot(expected_root)
        actual = tree_snapshot(BUILD_ROOT)
    if expected == actual:
        return []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        path for path in set(expected) & set(actual) if expected[path] != actual[path]
    )
    details: list[str] = []
    if missing:
        details.append(f"missing={','.join(missing)}")
    if extra:
        details.append(f"extra={','.join(extra)}")
    if changed:
        details.append(f"changed={','.join(changed)}")
    return [f"build/generated is stale ({'; '.join(details)})"]


def lint_repo(check_generated: bool = True) -> list[str]:
    errors: list[str] = []
    config = load_skill_config()
    errors.extend(lint_config(config))
    if errors:
        return errors

    configured_dirs = {
        skill["content_dir"] for skill in config.get("skills", {}).values()
    }
    actual_dirs = {
        path.name for path in CONTENT_ROOT.iterdir() if path.is_dir()
    }
    if actual_dirs != configured_dirs:
        errors.append(
            "content/: configured content directories do not match filesystem directories"
        )
    orphan_files = [
        path
        for path in CONTENT_ROOT.rglob("*")
        if path.is_file() and path.suffix != ".yaml"
    ]
    for path in orphan_files:
        errors.append(f"{path}: orphan non-YAML content file")

    entries = iter_content_entries(CONTENT_ROOT, config)
    slugs: dict[str, str] = {}
    orders: dict[str, set[int]] = {
        skill_key: set() for skill_key in config["skills"]
    }
    route_paths: set[str] = set()
    repeated_text: Counter[str] = Counter()
    for skill_key, path, entry in entries:
        skill = config["skills"][skill_key]
        errors.extend(lint_entry(skill_key, path, entry, skill))
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str):
            if slug in slugs:
                errors.append(f"{path}: duplicate global slug {slug} also used by {slugs[slug]}")
            else:
                slugs[slug] = skill_key
            route_paths.add(f"{skill['name']}/{skill['route_dir']}/{slug}.md")
        order = entry.get("order")
        if isinstance(order, int) and not isinstance(order, bool):
            if order in orders[skill_key]:
                errors.append(f"{path}: duplicate order {order} within {skill_key}")
            orders[skill_key].add(order)
        for field in ("gotchas", "assumptions", "workflows"):
            for value in entry.get(field, []) if isinstance(entry.get(field), list) else []:
                if isinstance(value, str):
                    repeated_text[normalized_text(value)] += 1

    legacy_targets = {
        Path(alias.get("from_route", "")).stem: alias.get("to_slug")
        for alias in config.get("route_aliases", [])
        if isinstance(alias, dict)
    }
    for _, path, entry in entries:
        if not isinstance(entry, dict):
            continue
        for related in entry.get("related_refs", []):
            target = legacy_targets.get(related, related)
            if target not in slugs:
                errors.append(f"{path}: unresolved related reference {related}")

    for text, count in repeated_text.items():
        if text and count >= 3:
            errors.append(
                f"content/: repeated generic-looking text occurs {count} times: {text!r}"
            )

    alias_route_paths = {
        f"{config['skills'][alias['from_skill']]['name']}/{alias['from_route']}"
        for alias in config.get("route_aliases", [])
        if isinstance(alias, dict) and alias.get("from_skill") in config["skills"]
    }
    errors.extend(lint_route_aliases(config, slugs, route_paths))
    errors.extend(lint_prompt_cases(config, route_paths, alias_route_paths))
    errors.extend(lint_upstream_lock(entries))
    errors.extend(lint_stata_help_lock(entries))
    errors.extend(lint_distribution_locks(entries))
    errors.extend(lint_manifests(entries))
    if check_generated and not errors:
        errors.extend(lint_generated_drift())
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-generated-check",
        action="store_true",
        help="Validate sources and locks without comparing an existing build directory.",
    )
    args = parser.parse_args(argv)
    errors = lint_repo(check_generated=not args.no_generated_check)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
