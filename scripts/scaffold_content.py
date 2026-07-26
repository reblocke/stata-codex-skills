#!/usr/bin/env python3
"""Report content gaps without mutating reviewed content.

The pre-PR2 scaffold command merged harvested/default text into curated YAML.
That made generated candidates an accidental publication authority.  This
compatibility command is now deliberately read-only with respect to content/.
"""

from __future__ import annotations

from pathlib import Path
import argparse

from runtime_guard import require_supported_runtime

require_supported_runtime()

from libskillpack import (
    CONTENT_ROOT,
    RAW_ROOT,
    iter_content_entries,
    load_skill_config,
    write_yaml,
)


DEFAULT_REPORT = RAW_ROOT / "candidates" / "content-review-gaps.yaml"
EXPECTED_FIELDS = (
    "slug",
    "skill",
    "section",
    "order",
    "title",
    "trigger",
    "aliases",
    "routing_terms",
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
)


def build_gap_report() -> dict:
    config = load_skill_config()
    entries: list[dict] = []
    for skill_key, path, content in iter_content_entries(CONTENT_ROOT, config):
        missing = [field for field in EXPECTED_FIELDS if field not in content]
        empty = [
            field
            for field in (
                "aliases",
                "routing_terms",
                "syntax_patterns",
                "gotchas",
                "assumptions",
                "workflows",
            )
            if field in content and not content[field]
        ]
        entries.append(
            {
                "skill": skill_key,
                "slug": content.get("slug", path.stem),
                "content_file": str(path.relative_to(CONTENT_ROOT.parent)),
                "missing_fields": missing,
                "empty_curated_fields": empty,
                "review_required": bool(missing or empty),
            }
        )
    return {"schema_version": 1, "entries": entries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retained only to reject the former destructive behavior.",
    )
    args = parser.parse_args(argv)
    if args.force:
        print("ERROR: --force is retired; curated content is never rewritten.")
        return 2
    report = build_gap_report()
    write_yaml(args.report, report)
    review_count = sum(
        1 for entry in report["entries"] if entry["review_required"]
    )
    print(f"Wrote {args.report}; {review_count} entries require review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
