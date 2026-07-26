#!/usr/bin/env python3
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import argparse
import re
import shutil

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


def render_all(
    output_root: Path = BUILD_ROOT,
    content_root: Path = CONTENT_ROOT,
    config_path: Path | None = None,
) -> None:
    config = load_skill_config(config_path) if config_path else load_skill_config()
    env = build_environment()
    grouped, by_slug = prepare_catalog(config, content_root)
    aliases_by_skill = prepared_route_aliases(config, by_slug)
    lock_index = embedded_lock_index()

    reference_template = env.get_template("reference.md.j2")
    skill_template = env.get_template("skill.md.j2")
    alias_template = env.get_template("alias.md.j2")
    provenance_template = env.get_template("provenance.md.j2")
    openai_template = env.get_template("openai.yaml.j2")

    for skill_key, skill in config["skills"].items():
        folder = output_root / skill["folder"]
        if folder.exists():
            shutil.rmtree(folder)
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
        print(f"Rendered {folder}")


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
