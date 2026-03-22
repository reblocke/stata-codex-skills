#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import shutil
from libskillpack import (
    MANIFEST_ROOT,
    RAW_ROOT,
    STATA_ROOT,
    ensure_dir,
    find_help_files_for_topic,
    read_yaml,
    relative_to_stata,
    strip_smcl_markup,
    write_text,
    write_yaml,
)


def harvest_manifest(manifest_path: Path, skill_key: str) -> dict[str, dict]:
    manifest = read_yaml(manifest_path)
    summary: dict[str, dict] = {}
    original_root = RAW_ROOT / "stata-help" / "original" / skill_key
    normalized_root = RAW_ROOT / "stata-help" / "normalized" / skill_key

    for entry in manifest.get("entries", []):
        slug = entry["slug"]
        matched_files = []
        for topic in entry.get("source_topics", []):
            matched_files.extend(find_help_files_for_topic(topic))
        unique_matches = []
        seen = set()
        for path in matched_files:
            if path not in seen:
                unique_matches.append(path)
                seen.add(path)

        raw_dest_dir = ensure_dir(original_root / slug)
        normalized_parts = []
        harvested_relpaths = []
        for src in unique_matches:
            dest = raw_dest_dir / src.name
            shutil.copy2(src, dest)
            harvested_relpaths.append(relative_to_stata(src))
            normalized_parts.append(
                f"## Source: {relative_to_stata(src)}\n\n{strip_smcl_markup(src.read_text(encoding='utf-8', errors='ignore'))}"
            )

        normalized_relpath = f"raw/stata-help/normalized/{skill_key}/{slug}.md"
        if normalized_parts:
            write_text(RAW_ROOT / "stata-help" / "normalized" / skill_key / f"{slug}.md", "\n\n".join(normalized_parts))

        summary[slug] = {
            "source_topics": entry.get("source_topics", []),
            "local_help_files": harvested_relpaths,
            "normalized_file": normalized_relpath if normalized_parts else None,
            "upstream_only": bool(entry.get("allow_upstream_only")),
        }
    return summary


def main() -> None:
    summary = {
        "core": harvest_manifest(MANIFEST_ROOT / "topic-map.yaml", "core"),
        "packages": harvest_manifest(MANIFEST_ROOT / "package-map.yaml", "packages"),
        "plugins": harvest_manifest(MANIFEST_ROOT / "plugin-map.yaml", "plugins"),
        "stata_root": str(STATA_ROOT),
    }
    write_yaml(RAW_ROOT / "stata-help" / "harvest-summary.yaml", summary)
    print("Wrote harvest summary")


if __name__ == "__main__":
    main()
