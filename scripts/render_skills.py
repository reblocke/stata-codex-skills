#!/usr/bin/env python3
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import argparse
import os
import re
import shutil
import tempfile
import uuid

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    TEMPLATES_ROOT,
    content_entries_by_skill,
    load_skill_config,
    read_yaml,
    sha256_file,
    write_text,
)
from release_state import SKILL_FOLDERS


class RenderTransactionError(RuntimeError):
    """A render failed and requires explicit recovery from a preserved backup."""


REQUIRED_SKILL_KEYS = ("core", "packages", "plugins")
REQUIRED_SKILLS = dict(zip(REQUIRED_SKILL_KEYS, SKILL_FOLDERS, strict=True))


def validate_required_skills(config: dict) -> None:
    """Keep an accidental config edit from publishing a partial skill pack."""

    skills = config.get("skills")
    if not isinstance(skills, dict):
        raise ValueError(
            "skill configuration must define exactly core, packages, and plugins"
        )
    observed_keys = set(skills)
    expected_keys = set(REQUIRED_SKILL_KEYS)
    if observed_keys != expected_keys:
        observed = ", ".join(sorted(str(key) for key in observed_keys)) or "<none>"
        raise ValueError(
            "skill configuration must define exactly core, packages, and "
            f"plugins; observed {observed}"
        )
    for key, expected_name in REQUIRED_SKILLS.items():
        skill = skills[key]
        if not isinstance(skill, dict):
            raise ValueError(f"skill configuration for {key} must be a mapping")
        if (
            skill.get("name") != expected_name
            or skill.get("folder") != expected_name
        ):
            raise ValueError(
                f"skill {key} must retain name and folder {expected_name}"
            )


def build_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_ROOT)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def normalized_markdown(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    compact = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return compact + "\n"


def canonical_route(skill: dict, slug: str) -> str:
    return f"{skill['route_dir']}/{slug}.md"


def prepare_catalog(
    config: dict,
    content_root: Path = CONTENT_ROOT,
) -> tuple[dict[str, list[dict]], dict[str, tuple[str, dict]]]:
    grouped = content_entries_by_skill(content_root, config)
    by_slug: dict[str, tuple[str, dict]] = {}

    for skill_key, entries in grouped.items():
        skill = config["skills"][skill_key]
        section_rank = {
            section: index for index, section in enumerate(skill["section_order"])
        }
        entries.sort(
            key=lambda entry: (
                section_rank.get(entry.get("section"), len(section_rank)),
                entry.get("order", 10**9),
                entry.get("slug", ""),
            )
        )
        for entry in entries:
            slug = entry["slug"]
            if slug in by_slug:
                raise ValueError(f"duplicate canonical slug: {slug}")
            routing_aliases: list[str] = []
            seen_aliases: set[str] = set()
            for alias in [*entry["aliases"], *entry["commands"]]:
                normalized_alias = alias.casefold()
                if normalized_alias not in seen_aliases:
                    seen_aliases.add(normalized_alias)
                    routing_aliases.append(alias)
            entry["routing_aliases"] = routing_aliases
            entry.setdefault("preflight_commands", [])
            entry.setdefault("install_commands", [])
            entry.setdefault("smoke_test", None)
            entry["route_path"] = canonical_route(skill, slug)
            entry["skill_name"] = skill["name"]
            entry["provenance_index_path"] = "../PROVENANCE.md"
            by_slug[slug] = (skill_key, entry)

    legacy_targets = {
        Path(alias["from_route"]).stem: alias["to_slug"]
        for alias in config.get("route_aliases", [])
    }
    for _, entries in grouped.items():
        for entry in entries:
            related_routes: list[dict] = []
            for related_slug in entry.get("related_refs", []):
                canonical_slug = legacy_targets.get(related_slug, related_slug)
                target = by_slug.get(canonical_slug)
                if target is None:
                    raise ValueError(
                        f"{entry['slug']}: unresolved related reference {related_slug}"
                    )
                target_skill_key, target_entry = target
                target_skill = config["skills"][target_skill_key]
                cross_skill = target_skill_key != entry["skill"]
                related_routes.append(
                    {
                        "slug": canonical_slug,
                        "skill_name": target_skill["name"],
                        "route_path": target_entry["route_path"],
                        "qualified_path": (
                            f"{target_skill['name']}/{target_entry['route_path']}"
                            if cross_skill
                            else target_entry["route_path"]
                        ),
                        "cross_skill": cross_skill,
                        "instruction": (
                            f" — cross-skill: load the `{target_skill['name']}` "
                            "skill first."
                            if cross_skill
                            else ""
                        ),
                    }
                )
            entry["related_routes"] = related_routes

    return grouped, by_slug


def prepared_route_aliases(
    config: dict,
    by_slug: dict[str, tuple[str, dict]],
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {
        skill_key: [] for skill_key in config.get("skills", {})
    }
    for raw_alias in config.get("route_aliases", []):
        alias = dict(raw_alias)
        target_skill_key, target_entry = by_slug[alias["to_slug"]]
        if target_skill_key != alias["to_skill"]:
            raise ValueError(
                f"alias {alias['from_route']}: target skill does not match {alias['to_slug']}"
            )
        target_skill = config["skills"][target_skill_key]
        alias["target_skill_name"] = target_skill["name"]
        alias["target_route"] = target_entry["route_path"]
        alias["target_slug"] = target_entry["slug"]
        grouped[alias["from_skill"]].append(alias)
    for aliases in grouped.values():
        aliases.sort(key=lambda item: item["from_route"])
    return grouped


def embedded_lock_index() -> list[dict]:
    index: list[dict] = []
    specifications = (
        ("upstream.yaml", "Upstream repository"),
        ("stata-help.yaml", "Local Stata help"),
        ("packages.yaml", "Community package distributions"),
        ("plugin-sdk.yaml", "Plugin SDK"),
    )
    for filename, label in specifications:
        path = LOCK_ROOT / filename
        if not path.is_file():
            continue
        data = read_yaml(path)
        if filename == "upstream.yaml":
            identity = f"commit `{data.get('repository', {}).get('commit', 'unknown')}`"
        elif filename == "stata-help.yaml":
            release = data.get("stata_release", {})
            identity = (
                f"{release.get('edition', 'unknown')} "
                f"{release.get('bundle_version', 'unknown')}"
            )
        elif filename == "packages.yaml":
            identity = f"{len(data.get('packages', {}))} locked package workflows"
        else:
            identity = f"{len(data.get('sources', []))} locked SDK sources"
        index.append(
            {
                "label": label,
                "identity": identity,
                "sha256": sha256_file(path),
            }
        )
    return index


def _render_tree(
    output_root: Path,
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> None:
    env = build_environment()
    lock_index = embedded_lock_index()

    reference_template = env.get_template("reference.md.j2")
    skill_template = env.get_template("skill.md.j2")
    alias_template = env.get_template("alias.md.j2")
    provenance_template = env.get_template("provenance.md.j2")
    openai_template = env.get_template("openai.yaml.j2")

    for skill_key, skill in config["skills"].items():
        folder = output_root / skill["folder"]
        route_dir = folder / skill["route_dir"]
        route_dir.mkdir(parents=True, exist_ok=True)

        entries = grouped[skill_key]
        sections: OrderedDict[str, list[dict]] = OrderedDict(
            (section, []) for section in skill["section_order"]
        )
        for entry in entries:
            sections[entry["section"]].append(entry)
            write_text(
                route_dir / f"{entry['slug']}.md",
                normalized_markdown(reference_template.render(entry=entry)),
            )

        route_aliases = aliases_by_skill[skill_key]
        for alias in route_aliases:
            alias_path = folder / alias["from_route"]
            write_text(
                alias_path,
                normalized_markdown(alias_template.render(alias=alias)),
            )

        section_payload = [
            {"name": name, "entries": section_entries}
            for name, section_entries in sections.items()
            if section_entries
        ]
        write_text(
            folder / "SKILL.md",
            normalized_markdown(
                skill_template.render(
                    skill=skill,
                    sections=section_payload,
                    route_aliases=route_aliases,
                )
            ),
        )
        write_text(
            folder / "PROVENANCE.md",
            normalized_markdown(
                provenance_template.render(
                    skill=skill,
                    entries=entries,
                    lock_index=lock_index,
                )
            ),
        )
        write_text(
            folder / "agents" / "openai.yaml",
            openai_template.render(interface=skill["interface"]).strip() + "\n",
        )


def _expected_rendered_files(
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> set[Path]:
    expected: list[Path] = []
    for skill_key, skill in config["skills"].items():
        folder = Path(skill["folder"])
        expected.extend(
            (
                folder / "SKILL.md",
                folder / "PROVENANCE.md",
                folder / "agents" / "openai.yaml",
            )
        )
        expected.extend(
            folder / skill["route_dir"] / f"{entry['slug']}.md"
            for entry in grouped[skill_key]
        )
        expected.extend(
            folder / alias["from_route"] for alias in aliases_by_skill[skill_key]
        )
    unique = set(expected)
    if len(unique) != len(expected):
        raise ValueError("render configuration maps multiple entries to one output path")
    return unique


def validate_rendered_tree(
    output_root: Path,
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> None:
    """Reject incomplete, unexpected, empty, or malformed staged output."""

    expected = _expected_rendered_files(config, grouped, aliases_by_skill)
    actual = {
        path.relative_to(output_root)
        for path in output_root.rglob("*")
        if path.is_file()
    }
    errors: list[str] = []
    missing = sorted(str(path) for path in expected - actual)
    extra = sorted(str(path) for path in actual - expected)
    if missing:
        errors.append(f"missing files: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected files: {', '.join(extra)}")

    expected_directories: set[Path] = set()
    for relative in expected:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    actual_directories = {
        path.relative_to(output_root)
        for path in output_root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    missing_directories = sorted(
        str(path) for path in expected_directories - actual_directories
    )
    extra_directories = sorted(
        str(path) for path in actual_directories - expected_directories
    )
    if missing_directories:
        errors.append(f"missing directories: {', '.join(missing_directories)}")
    if extra_directories:
        errors.append(f"unexpected directories: {', '.join(extra_directories)}")

    unsafe_links = sorted(
        str(path.relative_to(output_root))
        for path in output_root.rglob("*")
        if path.is_symlink()
    )
    if unsafe_links:
        errors.append(f"symbolic links are not publishable: {', '.join(unsafe_links)}")

    empty = sorted(
        str(relative)
        for relative in expected & actual
        if not (output_root / relative).read_bytes()
    )
    if empty:
        errors.append(f"empty files: {', '.join(empty)}")

    for skill in config["skills"].values():
        metadata_path = (
            output_root / skill["folder"] / "agents" / "openai.yaml"
        )
        if metadata_path.relative_to(output_root) not in actual:
            continue
        metadata = read_yaml(metadata_path)
        if metadata.get("interface") != skill["interface"]:
            errors.append(
                f"{metadata_path.relative_to(output_root)}: interface metadata "
                "does not match configuration"
            )

    if errors:
        raise ValueError("staged render validation failed: " + "; ".join(errors))


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _replace_rendered_tree(staged_root: Path, output_root: Path) -> None:
    """Replace output_root and restore its prior state if the swap fails."""

    backup_root: Path | None = None
    if output_root.exists() or output_root.is_symlink():
        backup_root = output_root.parent / (
            f".{output_root.name}.backup-{uuid.uuid4().hex}"
        )
        os.replace(output_root, backup_root)

    try:
        os.replace(staged_root, output_root)
    except BaseException as swap_error:
        if backup_root is not None and backup_root.exists():
            try:
                os.replace(backup_root, output_root)
            except BaseException as restore_error:
                raise RenderTransactionError(
                    "Rendered-tree swap failed and the prior tree could not be "
                    f"restored. The prior tree remains at {backup_root}: "
                    f"{restore_error}"
                ) from swap_error
        raise
    else:
        if backup_root is not None:
            try:
                _remove_path(backup_root)
            except OSError as cleanup_error:
                print(
                    "WARNING: rendered tree was committed, but the prior-tree "
                    f"backup could not be removed: {backup_root}: {cleanup_error}"
                )


def render_all(
    output_root: Path = BUILD_ROOT,
    content_root: Path = CONTENT_ROOT,
    config_path: Path | None = None,
) -> None:
    """Render, validate, and transactionally replace a complete skill tree."""

    output_root = Path(output_root)
    if output_root.parent == output_root:
        raise ValueError("refusing to render over a filesystem root")
    config = load_skill_config(config_path) if config_path else load_skill_config()
    validate_required_skills(config)
    grouped, by_slug = prepare_catalog(config, content_root)
    aliases_by_skill = prepared_route_aliases(config, by_slug)

    output_root.parent.mkdir(parents=True, exist_ok=True)
    staged_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_root.name}.stage-",
            dir=output_root.parent,
        )
    )
    try:
        _render_tree(staged_root, config, grouped, aliases_by_skill)
        validate_rendered_tree(
            staged_root,
            config,
            grouped,
            aliases_by_skill,
        )
        _replace_rendered_tree(staged_root, output_root)
    finally:
        _remove_path(staged_root)

    for skill in config["skills"].values():
        print(f"Rendered {output_root / skill['folder']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=BUILD_ROOT,
        help="Render into this directory instead of build/generated.",
    )
    args = parser.parse_args(argv)
    render_all(output_root=args.output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
