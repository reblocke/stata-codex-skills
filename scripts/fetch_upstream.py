#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse

from libskillpack import (
    CONTENT_ROOT,
    MANIFEST_ROOT,
    RAW_ROOT,
    UPSTREAM_REPO_DIR,
    UPSTREAM_REPO_URL,
    iter_content_entries,
    load_skill_config,
    run_command,
    sha256_file,
    write_yaml,
)


UPSTREAM_ROOTS = {
    "core": Path("plugins/stata/skills/stata/references"),
    "packages": Path("plugins/stata/skills/stata/packages"),
    "plugins": Path("plugins/stata-c-plugins/skills/stata-c-plugins/references"),
}
MANIFEST_NAMES = {
    "core": "topic-map.yaml",
    "packages": "package-map.yaml",
    "plugins": "plugin-map.yaml",
}
CANDIDATE_REPORT = RAW_ROOT / "candidates" / "upstream-inventory.yaml"


def refresh_upstream_repo() -> None:
    if UPSTREAM_REPO_DIR.exists():
        result = run_command(
            ["git", "-C", str(UPSTREAM_REPO_DIR), "pull", "--ff-only"],
            timeout_seconds=120,
        )
    else:
        UPSTREAM_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(
            [
                "git",
                "clone",
                "--depth",
                "1",
                UPSTREAM_REPO_URL,
                str(UPSTREAM_REPO_DIR),
            ],
            timeout_seconds=120,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "Failed to refresh upstream repo")


def upstream_commit() -> str:
    result = run_command(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "rev-parse", "HEAD"],
        timeout_seconds=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "Could not read upstream commit")
    return result.stdout.strip()


def build_inventory() -> dict:
    inventory: dict[str, list[dict]] = {}
    for skill_key, relative_root in UPSTREAM_ROOTS.items():
        source_root = UPSTREAM_REPO_DIR / relative_root
        inventory[skill_key] = [
            {
                "path": str(path.relative_to(UPSTREAM_REPO_DIR)),
                "sha256": sha256_file(path),
            }
            for path in sorted(source_root.glob("*.md"))
            if path.name != "filing-issues.md"
        ]
    return {
        "schema_version": 1,
        "repository": UPSTREAM_REPO_URL,
        "commit": upstream_commit(),
        "inventory": inventory,
    }


def write_provenance_manifests() -> None:
    """Write non-executable provenance indexes from canonical content."""

    config = load_skill_config()
    grouped: dict[str, list[dict]] = {
        skill_key: [] for skill_key in config["skills"]
    }
    for skill_key, _, entry in iter_content_entries(CONTENT_ROOT, config):
        provenance = entry.get("provenance", {})
        grouped[skill_key].append(
            {
                "slug": entry.get("slug"),
                "upstream_files": provenance.get("upstream_files", []),
                "upstream_only": provenance.get("upstream_only", False),
            }
        )
    for skill_key, entries in grouped.items():
        entries.sort(key=lambda item: item["slug"])
        payload = {
            "schema_version": 2,
            "role": "provenance-lock-index",
            "source_authority": f"content/{config['skills'][skill_key]['content_dir']}",
            "upstream_lock": "locks/upstream.yaml",
            "entries": entries,
        }
        write_yaml(MANIFEST_ROOT / MANIFEST_NAMES[skill_key], payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the existing raw checkout without network access.",
    )
    parser.add_argument("--report", type=Path, default=CANDIDATE_REPORT)
    args = parser.parse_args(argv)
    try:
        if not args.offline:
            refresh_upstream_repo()
        if not UPSTREAM_REPO_DIR.exists():
            raise RuntimeError("Raw upstream checkout is missing; run without --offline once")
        report = build_inventory()
        write_yaml(args.report, report)
        write_provenance_manifests()
    except RuntimeError as error:
        print(f"ERROR: {error}")
        return 1
    print(f"Wrote candidate inventory {args.report}")
    print("Wrote provenance-only manifests; content remains publication authority")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
