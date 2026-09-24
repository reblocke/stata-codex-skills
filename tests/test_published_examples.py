from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import re
import sys
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lint_skill_pack  # noqa: E402
import render_skills  # noqa: E402


class PublishedLoopTests(unittest.TestCase):
    def test_published_loop_bodies_are_the_executed_bodies(self) -> None:
        source = yaml.safe_load(
            (REPO_ROOT / "content/core/programming-basics.yaml").read_text()
        )
        smoke = source["smoke_test"]
        loops = source["syntax_patterns"][:2]
        self.assertEqual(2, len(loops))
        for body in loops:
            self.assertIn(body, smoke)
            self.assertIsNone(lint_skill_pack.INLINE_STATA_LOOP_RE.search(body))
        with TemporaryDirectory(prefix="published-loops-") as temp_root:
            root = render_skills.render_all(output_root=Path(temp_root) / "generated")
            rendered = (
                root / "stata-core/references/programming-basics.md"
            ).read_text()
        blocks = re.findall(r"### Pattern \d+\n\n```stata\n(.*?)\n```", rendered, re.S)
        self.assertEqual(source["syntax_patterns"], blocks)

    def test_restoring_inline_loop_is_detected(self) -> None:
        for line in (
            "foreach x in 1 2 { display `x' }",
            "forvalues x = 1/2 { display `x' }",
        ):
            self.assertIsNotNone(
                lint_skill_pack.INLINE_STATA_LOOP_RE.search(line)
            )


if __name__ == "__main__":
    unittest.main()
