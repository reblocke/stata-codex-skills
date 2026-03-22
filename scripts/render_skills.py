#!/usr/bin/env python3
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import shutil
from jinja2 import Environment, FileSystemLoader
from libskillpack import BUILD_ROOT, CONTENT_ROOT, MANIFEST_ROOT, TEMPLATES_ROOT, read_yaml, write_text


SKILL_CONFIGS = {
    "core": {
        "folder": "stata-core",
        "route_dir": "references",
        "heading": "Stata Core Skill",
        "name": "stata-core",
        "description": "Use when tasks involve built-in Stata commands, do-files, data management, estimation, graphics, or Mata.",
        "summary": "This skill covers built-in Stata workflows. Read only the 1-3 reference files directly relevant to the current task.",
        "rules": [
            "Prefer built-in Stata commands before reaching for a community package.",
            "When running code from the terminal, use batch mode and inspect the generated log file.",
            "Preserve raw data and write cleaned or derived outputs separately.",
            "Validate merge results, missing-value handling, and stored results before chaining commands.",
        ],
        "critical_gotchas": [
            {"title": "Missing values", "body": "Numeric missing values sort above all real numbers, so always guard comparisons with !missing(...)."},
            {"title": "Comparison syntax", "body": "Use = for assignment and == for comparisons."},
            {"title": "by-groups", "body": "Use bysort unless you have already sorted on the grouping variables."},
            {"title": "Factor variables", "body": "Use i. for categorical variables and c. for continuous interactions."},
            {"title": "merge diagnostics", "body": "Inspect _merge before dropping it or asserting matched records."},
            {"title": "Variable creation", "body": "Use generate for new variables and replace for existing ones."},
        ],
    },
    "packages": {
        "folder": "stata-packages",
        "route_dir": "packages",
        "heading": "Stata Community Packages Skill",
        "name": "stata-packages",
        "description": "Use when tasks involve community-contributed Stata packages such as reghdfe, estout, rdrobust, synth, or xtabond2.",
        "summary": "This skill covers community-contributed Stata packages. Load only the specific package guide needed for the user's command or estimator.",
        "rules": [
            "Confirm the package and any dependencies are installed before writing code that depends on them.",
            "Use a temporary PLUS directory for automated package tests so the global Stata setup stays clean.",
            "Prefer the smallest reproducible example that exercises the package syntax you need.",
            "Do not load unrelated package guides into context.",
        ],
        "critical_gotchas": [],
    },
    "plugins": {
        "folder": "stata-c-plugins",
        "route_dir": "references",
        "heading": "Stata C Plugins Skill",
        "name": "stata-c-plugins",
        "description": "Use when tasks involve building, debugging, packaging, or validating Stata C or C++ plugins.",
        "summary": "This skill covers Stata plugin development. Load only the SDK or workflow references directly relevant to the current plugin task.",
        "rules": [
            "Plan the interface and validation path before writing plugin code.",
            "Prefer wrapping an existing C or C++ backend over reimplementing an algorithm from scratch.",
            "Catch every exception that could escape stata_call and validate return codes in batch mode.",
            "Treat plugin crashes as high risk because they terminate the Stata session.",
        ],
        "critical_gotchas": [],
    },
}


def build_environment() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES_ROOT)), trim_blocks=True, lstrip_blocks=True)


def manifest_for(kind: str) -> dict:
    name = {"core": "topic-map.yaml", "packages": "package-map.yaml", "plugins": "plugin-map.yaml"}[kind]
    return read_yaml(MANIFEST_ROOT / name)


def content_path_for(kind: str, slug: str) -> Path:
    return CONTENT_ROOT / kind / f"{slug}.yaml"


def render_kind(kind: str, env: Environment) -> None:
    config = SKILL_CONFIGS[kind]
    manifest = manifest_for(kind)
    folder = BUILD_ROOT / config["folder"]
    route_dir = folder / config["route_dir"]

    if folder.exists():
        shutil.rmtree(folder)
    route_dir.mkdir(parents=True, exist_ok=True)

    reference_template = env.get_template("reference.md.j2")
    skill_template = env.get_template("skill.md.j2")

    sections: OrderedDict[str, list[dict]] = OrderedDict()
    for meta in manifest.get("entries", []):
        entry = read_yaml(content_path_for(kind, meta["slug"]))
        entry["route_path"] = f"{config['route_dir']}/{meta['slug']}.md"
        sections.setdefault(meta["section"], []).append(entry)
        write_text(route_dir / f"{meta['slug']}.md", reference_template.render(entry=entry))

    section_payload = [{"name": name, "entries": entries} for name, entries in sections.items()]
    write_text(folder / "SKILL.md", skill_template.render(skill=config, sections=section_payload))
    print(f"Rendered {folder}")


def main() -> None:
    env = build_environment()
    for kind in ["core", "packages", "plugins"]:
        render_kind(kind, env)


if __name__ == "__main__":
    main()
