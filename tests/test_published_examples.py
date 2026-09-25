from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lint_skill_pack  # noqa: E402
import render_skills  # noqa: E402
from libskillpack import iter_content_entries, load_skill_config, CONTENT_ROOT  # noqa: E402


class PublishedLoopTests(unittest.TestCase):
    def test_all_migrated_rendered_code_matches_reviewed_yaml(self) -> None:
        config = load_skill_config()
        with TemporaryDirectory(prefix="rendered-example-contract-") as temp_root:
            root = render_skills.render_all(output_root=Path(temp_root) / "generated")
            for skill_key, _, entry in iter_content_entries(CONTENT_ROOT, config):
                if not entry.get("examples"):
                    continue
                skill = config["skills"][skill_key]
                base = root / skill["folder"] / skill["route_dir"]
                for index, example in enumerate(entry["examples"], start=1):
                    with self.subTest(example=example["id"]):
                        if entry.get("recipes"):
                            recipe = next(
                                item for item in entry["recipes"]
                                if example["id"] in item["example_ids"]
                            )
                            reference = base / f"{entry['slug']}-{recipe['slug']}.md"
                            local_index = recipe["example_ids"].index(example["id"]) + 1
                        else:
                            reference = base / f"{entry['slug']}.md"
                            local_index = index
                        rendered = reference.read_text()
                        section = rendered.split(f"### Example {local_index}\n", 1)[1]
                        self.assertIn(f"- ID: `{example['id']}`.", section)
                        code = section.split(f"```{example['language']}\n", 1)[1].split("\n```", 1)[0]
                        self.assertEqual(example["code"], code)

    def test_example_schema_rejects_duplicate_unresolved_and_conflicting_authority(self) -> None:
        path = REPO_ROOT / "content/core/programming-basics.yaml"
        original = yaml.safe_load(path.read_text())
        skill = load_skill_config()["skills"]["core"]

        duplicate = deepcopy(original)
        duplicate["examples"][1]["id"] = duplicate["examples"][0]["id"]
        duplicate["examples"][1]["test_id"] = duplicate["examples"][0]["test_id"]
        errors = lint_skill_pack.lint_entry("core", path, duplicate, skill)
        self.assertTrue(any("duplicate id" in error for error in errors))
        self.assertTrue(any("duplicate test_id" in error for error in errors))

        unresolved = deepcopy(original)
        unresolved["examples"][0]["test_id"] = "missing-example"
        unresolved["examples"][0]["fixture"] = "tests/stata/examples/missing-example.yaml"
        errors = lint_skill_pack.lint_entry("core", path, unresolved, skill)
        self.assertTrue(any("fixture is missing" in error for error in errors))

        conflict = deepcopy(original)
        conflict["syntax_patterns"] = ["display 1"]
        errors = lint_skill_pack.lint_entry("core", path, conflict, skill)
        self.assertTrue(any("conflict with legacy" in error for error in errors))

        unclassified = deepcopy(original)
        del unclassified["examples"][0]["kind"]
        errors = lint_skill_pack.lint_entry("core", path, unclassified, skill)
        self.assertTrue(any("invalid kind" in error for error in errors))

    def test_legacy_blocks_require_kind_and_language(self) -> None:
        path = REPO_ROOT / "content/core/basics-getting-started.yaml"
        entry = yaml.safe_load(path.read_text())
        del entry["pattern_languages"]
        errors = lint_skill_pack.lint_entry(
            "core", path, entry, load_skill_config()["skills"]["core"]
        )
        self.assertTrue(any("pattern_languages" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
