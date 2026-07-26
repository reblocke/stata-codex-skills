#!/usr/bin/env python3
"""Render twice in clean temporary roots and require byte-identical trees."""

from __future__ import annotations

from pathlib import Path
import tempfile

from release_state import tree_digest, validate_complete_skill_tree
from render_skills import _retained_workspace_scope, render_all


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="stata-render-double-")).resolve()
    with _retained_workspace_scope(root, "deterministic-render"):
        first = root / "first"
        second = root / "second"
        render_all(output_root=first)
        render_all(output_root=second)
        errors = [
            *validate_complete_skill_tree(first),
            *validate_complete_skill_tree(second),
        ]
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        first_digest = tree_digest(first)
        second_digest = tree_digest(second)
        if first_digest != second_digest:
            print(
                "ERROR: clean renders are not byte-identical: "
                f"{first_digest} != {second_digest}"
                )
            return 1
    print(f"Deterministic double render passed: {first_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
