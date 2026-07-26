#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse

from runtime_guard import require_supported_runtime

require_supported_runtime()

from libskillpack import (
    CONTENT_ROOT,
    STATA_ADO_BASE,
    find_help_files_exact,
    iter_content_entries,
    load_skill_config,
    relative_to_stata,
    sha256_stata_help_source,
)
from refresh_locks import candidate_relative_path, publish_lock_candidate


CANDIDATE_TARGET = "stata-help-harvest"


def harvest_entry(skill_key: str, path: Path, entry: dict) -> tuple[dict, list[str]]:
    provenance = entry.get("provenance", {})
    exact_stems = provenance.get("local_help_topics", [])
    globs = provenance.get("local_help_globs", [])
    declared_files = provenance.get("local_help_files", [])
    resolved, missing_selectors = find_help_files_exact(exact_stems, globs)
    resolved_files = [relative_to_stata(source) for source in resolved]
    errors: list[str] = []

    package_only = provenance.get("package_only") is True
    upstream_only = provenance.get("upstream_only") is True
    if missing_selectors and not (package_only or upstream_only):
        errors.append(
            f"{path}: exact local help selectors matched nothing: "
            + ", ".join(missing_selectors)
        )
    if set(resolved_files) != set(declared_files):
        errors.append(
            f"{path}: curated local_help_files differ from exact/glob resolution"
        )

    resolved_sources: list[dict] = []
    for source in resolved:
        resolved_sources.append(
            {
                "path": relative_to_stata(source),
                "sha256": sha256_stata_help_source(
                    source,
                    help_root=STATA_ADO_BASE,
                ),
            }
        )

    report = {
        "skill": skill_key,
        "slug": entry.get("slug"),
        "content_file": str(path.relative_to(CONTENT_ROOT.parent)),
        "exact_stems": exact_stems,
        "globs": globs,
        "declared_files": declared_files,
        "resolved_files": resolved_files,
        "missing_selectors": missing_selectors,
        "resolved_sources": resolved_sources,
        "review_required": bool(errors),
    }
    return report, errors


def build_candidate_report() -> tuple[dict, list[str]]:
    config = load_skill_config()
    reports: list[dict] = []
    errors: list[str] = []
    for skill_key, path, entry in iter_content_entries(CONTENT_ROOT, config):
        report, entry_errors = harvest_entry(skill_key, path, entry)
        reports.append(report)
        errors.extend(entry_errors)
    return (
        {
            "schema_version": 1,
            "source_root": str(STATA_ADO_BASE),
            "entries": reports,
        },
        errors,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve exact reviewed Stata help selectors, write one fixed "
            "paths-and-hashes candidate report, and never rewrite curated "
            "content YAML."
        )
    )
    parser.parse_args(argv)
    if not STATA_ADO_BASE.exists():
        print(f"ERROR: Stata help root not found: {STATA_ADO_BASE}")
        return 1
    try:
        report, errors = build_candidate_report()
        destination = publish_lock_candidate(
            candidate_relative_path(CANDIDATE_TARGET),
            report,
        )
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}")
        return 1
    print(f"Wrote candidate report {destination}")
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
