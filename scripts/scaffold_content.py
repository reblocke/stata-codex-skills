#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
from libskillpack import (
    CONTENT_ROOT,
    MANIFEST_ROOT,
    RAW_ROOT,
    REPO_ROOT,
    extract_syntax_patterns,
    extract_warning_lines,
    read_text,
    read_yaml,
    write_yaml,
)


def default_workflows(kind: str, title: str) -> list[str]:
    if kind == "core":
        return [
            f"Start with the official help topics attached to {title}, then tailor the syntax to the current dataset.",
            "Prefer a small batch-mode smoke test before applying the command sequence to a large dataset.",
        ]
    if kind == "packages":
        return [
            "Confirm the package and any dependencies are installed on the active adopath before writing code that uses it.",
            "Pair the package command with a small reproducible example before folding it into a larger do-file.",
        ]
    return [
        "Plan the interface and the validation path before writing plugin code or wrapper ado-files.",
        "Use batch-mode Stata logs and compiler output together when debugging plugin failures.",
    ]


def default_gotchas(kind: str) -> list[str]:
    if kind == "core":
        return ["Check missing-value behavior, option defaults, and stored results before chaining commands."]
    if kind == "packages":
        return ["Package syntax and dependencies vary across versions; verify the installed help file before finalizing code."]
    return ["A plugin crash terminates the Stata session, so treat every memory access and return code as high risk."]


def scaffold_kind(kind: str, manifest_name: str, content_dir_name: str, force: bool) -> None:
    manifest = read_yaml(MANIFEST_ROOT / manifest_name)
    harvest_summary = read_yaml(RAW_ROOT / "stata-help" / "harvest-summary.yaml").get(kind, {})
    target_dir = CONTENT_ROOT / content_dir_name

    for entry in manifest.get("entries", []):
        slug = entry["slug"]
        existing_path = target_dir / f"{slug}.yaml"
        existing = read_yaml(existing_path) if existing_path.exists() else {}
        summary = harvest_summary.get(slug, {})
        normalized_path = summary.get("normalized_file")
        normalized_text = ""
        if normalized_path:
            normalized_text = read_text(REPO_ROOT / normalized_path)

        syntax_patterns = existing.get("syntax_patterns") or extract_syntax_patterns(normalized_text)
        gotchas = existing.get("gotchas") or extract_warning_lines(normalized_text) or default_gotchas(kind)
        workflows = existing.get("workflows") or default_workflows(kind, entry["title"])

        scaffold = {
            "slug": slug,
            "skill": kind,
            "section": entry["section"],
            "title": existing.get("title", entry["title"]),
            "trigger": existing.get("trigger", entry["trigger"]),
            "commands": existing.get("commands", entry.get("commands", [])),
            "source_topics": existing.get("source_topics", entry.get("source_topics", [])),
            "syntax_patterns": syntax_patterns,
            "gotchas": gotchas,
            "workflows": workflows,
            "validation_case": existing.get("validation_case", entry.get("validation_case")),
            "related_refs": existing.get("related_refs", entry.get("related_refs", [])),
            "install_commands": existing.get("install_commands", entry.get("install_commands", [])),
            "smoke_test": existing.get("smoke_test", entry.get("smoke_test")),
            "provenance": {
                "local_help_topics": entry.get("source_topics", []),
                "local_help_files": summary.get("local_help_files", []),
                "upstream_files": [entry["upstream_relpath"]] if entry.get("upstream_relpath") else [],
                "upstream_only": bool(entry.get("allow_upstream_only")),
            },
        }

        if not existing_path.exists() or force:
            write_yaml(existing_path, scaffold)
            print(f"Scaffolded {existing_path}")
        else:
            merged = scaffold | existing
            merged["provenance"] = existing.get("provenance", {}) | scaffold["provenance"]
            write_yaml(existing_path, merged)
            print(f"Refreshed metadata for {existing_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Overwrite existing content files.")
    args = parser.parse_args()

    scaffold_kind("core", "topic-map.yaml", "core", args.force)
    scaffold_kind("packages", "package-map.yaml", "packages", args.force)
    scaffold_kind("plugins", "plugin-map.yaml", "plugins", args.force)


if __name__ == "__main__":
    main()
