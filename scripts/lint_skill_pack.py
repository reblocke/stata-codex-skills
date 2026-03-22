#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import sys
import yaml
from libskillpack import BUILD_ROOT, CONTENT_ROOT, MANIFEST_ROOT, read_text, read_yaml


REQUIRED_FIELDS = [
    "title",
    "trigger",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "workflows",
    "validation_case",
    "related_refs",
    "provenance",
]

KIND_CONFIG = {
    "core": {"manifest": "topic-map.yaml", "folder": "stata-core", "route_dir": "references"},
    "packages": {"manifest": "package-map.yaml", "folder": "stata-packages", "route_dir": "packages"},
    "plugins": {"manifest": "plugin-map.yaml", "folder": "stata-c-plugins", "route_dir": "references"},
}


def parse_frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


def lint_repo() -> list[str]:
    errors: list[str] = []
    all_slugs: set[str] = set()
    content_cache: list[tuple[str, str, dict]] = []

    for kind, config in KIND_CONFIG.items():
        manifest = read_yaml(MANIFEST_ROOT / config["manifest"])
        if not manifest.get("entries"):
            errors.append(f"{config['manifest']}: missing entries")
            continue

        generated_skill_path = BUILD_ROOT / config["folder"] / "SKILL.md"
        if not generated_skill_path.exists():
            errors.append(f"{generated_skill_path}: missing generated SKILL.md")
        else:
            frontmatter = parse_frontmatter(read_text(generated_skill_path))
            if not frontmatter.get("name") or not frontmatter.get("description"):
                errors.append(f"{generated_skill_path}: invalid frontmatter")
            if len(read_text(generated_skill_path).splitlines()) > 500:
                errors.append(f"{generated_skill_path}: exceeds 500-line budget")

        for entry in manifest["entries"]:
            slug = entry["slug"]
            if slug in all_slugs:
                errors.append(f"duplicate slug across manifests: {slug}")
            all_slugs.add(slug)

            content_path = CONTENT_ROOT / kind / f"{slug}.yaml"
            if not content_path.exists():
                errors.append(f"{content_path}: missing content file")
                continue
            data = read_yaml(content_path)
            content_cache.append((kind, slug, data))
            for field in REQUIRED_FIELDS:
                if field not in data:
                    errors.append(f"{content_path}: missing field {field}")
            provenance = data.get("provenance", {})
            if not provenance.get("upstream_only") and not provenance.get("local_help_files"):
                errors.append(f"{content_path}: lacks harvested local help files and is not marked upstream-only")

            generated_ref = BUILD_ROOT / config["folder"] / config["route_dir"] / f"{slug}.md"
            if not generated_ref.exists():
                errors.append(f"{generated_ref}: missing generated reference file")

    for kind, slug, data in content_cache:
        for related in data.get("related_refs", []):
            if related not in all_slugs:
                errors.append(f"{CONTENT_ROOT / kind / f'{slug}.yaml'}: unknown related ref {related}")

    return errors


def main() -> None:
    errors = lint_repo()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        sys.exit(1)
    print("Lint passed")


if __name__ == "__main__":
    main()
