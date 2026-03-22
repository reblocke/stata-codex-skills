#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
from libskillpack import BUILD_ROOT, DEFAULT_SKILLS_DIR, copy_tree_fresh


SKILL_FOLDERS = ["stata-core", "stata-packages", "stata-c-plugins"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default=str(DEFAULT_SKILLS_DIR), help="Target Codex skills directory.")
    args = parser.parse_args()

    dest_root = Path(args.dest).expanduser()
    dest_root.mkdir(parents=True, exist_ok=True)

    for folder_name in SKILL_FOLDERS:
        source = BUILD_ROOT / folder_name
        if not source.exists():
            raise SystemExit(f"Missing generated skill folder: {source}")
        destination = dest_root / folder_name
        copy_tree_fresh(source, destination)
        print(f"Published {source} -> {destination}")


if __name__ == "__main__":
    main()
