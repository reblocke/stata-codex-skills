#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from seed_data import (
    CORE_RELATED_REFS,
    CORE_SECTIONS,
    CORE_SOURCE_TOPICS,
    CORE_UPSTREAM_ONLY,
    PACKAGE_METADATA,
    PACKAGE_SECTIONS,
    PACKAGE_UPSTREAM_ONLY,
    PLUGIN_METADATA,
    PLUGIN_SECTIONS,
    section_for,
)
from libskillpack import (
    MANIFEST_ROOT,
    UPSTREAM_REPO_DIR,
    UPSTREAM_REPO_URL,
    parse_markdown_title,
    pretty_slug,
    run_command,
    write_yaml,
)


def refresh_upstream_repo() -> None:
    if UPSTREAM_REPO_DIR.exists():
        result = run_command(["git", "-C", str(UPSTREAM_REPO_DIR), "pull", "--ff-only"])
    else:
        UPSTREAM_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(UPSTREAM_REPO_DIR)])
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or "Failed to refresh upstream repo")


def default_trigger(kind: str, title: str, commands: list[str]) -> str:
    if kind == "core":
        return f"Use when the user needs built-in Stata help with {title.lower()}."
    if kind == "packages":
        return f"Use when the user asks about {pretty_slug(commands[0]).lower()} or a related community-package workflow."
    return f"Use when the task needs {title.lower()} for a Stata plugin project."


def default_validation_case(title: str, commands: list[str]) -> str:
    if commands:
        return f"Run a small batch-mode example that exercises {commands[0]} and inspect the resulting log."
    return f"Run a small batch-mode example that exercises the workflow described in {title}."


def core_entries() -> list[dict]:
    ref_dir = UPSTREAM_REPO_DIR / "plugins" / "stata" / "skills" / "stata" / "references"
    entries: list[dict] = []
    for path in sorted(ref_dir.glob("*.md")):
        if path.stem == "filing-issues":
            continue
        slug = path.stem
        title = parse_markdown_title(path)
        commands = CORE_SOURCE_TOPICS.get(slug, [slug.replace("-", "_")])
        entries.append(
            {
                "slug": slug,
                "title": title,
                "section": section_for(slug, CORE_SECTIONS),
                "trigger": default_trigger("core", title, commands),
                "commands": commands,
                "source_topics": commands,
                "upstream_relpath": str(path.relative_to(UPSTREAM_REPO_DIR)),
                "related_refs": CORE_RELATED_REFS.get(slug, []),
                "validation_case": default_validation_case(title, commands),
                "allow_upstream_only": slug in CORE_UPSTREAM_ONLY,
            }
        )
    return entries


def package_entries() -> list[dict]:
    package_dir = UPSTREAM_REPO_DIR / "plugins" / "stata" / "skills" / "stata" / "packages"
    entries: list[dict] = []
    for path in sorted(package_dir.glob("*.md")):
        slug = path.stem
        meta = PACKAGE_METADATA[slug]
        title = parse_markdown_title(path)
        entries.append(
            {
                "slug": slug,
                "title": title,
                "section": section_for(slug, PACKAGE_SECTIONS),
                "trigger": default_trigger("packages", title, meta["commands"]),
                "commands": meta["commands"],
                "source_topics": meta["source_topics"],
                "upstream_relpath": str(path.relative_to(UPSTREAM_REPO_DIR)),
                "related_refs": meta.get("related_refs", []),
                "validation_case": meta.get("validation_case", default_validation_case(title, meta["commands"])),
                "allow_upstream_only": slug in PACKAGE_UPSTREAM_ONLY,
                "install_commands": meta.get("install_commands", []),
                "smoke_test": meta.get("smoke_test"),
            }
        )
    return entries


def plugin_entries() -> list[dict]:
    ref_dir = UPSTREAM_REPO_DIR / "plugins" / "stata-c-plugins" / "skills" / "stata-c-plugins" / "references"
    entries: list[dict] = []

    sdk = PLUGIN_METADATA["plugin-sdk-basics"].copy()
    sdk["slug"] = "plugin-sdk-basics"
    sdk["section"] = section_for("plugin-sdk-basics", PLUGIN_SECTIONS)
    sdk["trigger"] = default_trigger("plugins", sdk["title"], sdk["commands"])
    entries.append(sdk)

    for path in sorted(ref_dir.glob("*.md")):
        slug = path.stem
        meta = PLUGIN_METADATA[slug]
        title = parse_markdown_title(path)
        entries.append(
            {
                "slug": slug,
                "title": title,
                "section": section_for(slug, PLUGIN_SECTIONS),
                "trigger": default_trigger("plugins", title, meta["commands"]),
                "commands": meta["commands"],
                "source_topics": meta["source_topics"],
                "upstream_relpath": str(path.relative_to(UPSTREAM_REPO_DIR)),
                "related_refs": meta.get("related_refs", []),
                "validation_case": meta.get("validation_case", default_validation_case(title, meta["commands"])),
                "allow_upstream_only": meta.get("allow_upstream_only", True),
            }
        )
    return entries


def write_manifest(name: str, entries: list[dict]) -> None:
    path = MANIFEST_ROOT / f"{name}-map.yaml"
    payload = {
        "skill": name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "upstream_repo": UPSTREAM_REPO_URL,
        "entry_count": len(entries),
        "entries": entries,
    }
    write_yaml(path, payload)
    print(f"Wrote {path} with {len(entries)} entries")


def main() -> None:
    refresh_upstream_repo()
    write_manifest("topic", core_entries())
    write_manifest("package", package_entries())
    write_manifest("plugin", plugin_entries())


if __name__ == "__main__":
    main()
