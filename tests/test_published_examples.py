from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lint_skill_pack  # noqa: E402
import render_skills  # noqa: E402
import validate_skill_pack  # noqa: E402
from libskillpack import iter_content_entries, load_skill_config, CONTENT_ROOT  # noqa: E402


class PublishedLoopTests(unittest.TestCase):
    def test_published_loop_bodies_are_the_executed_bodies(self) -> None:
        source = yaml.safe_load(
            (REPO_ROOT / "content/core/programming-basics.yaml").read_text()
        )
        loops = source["examples"][:2]
        self.assertEqual(2, len(loops))
        for example in loops:
            self.assertIsNone(
                lint_skill_pack.INLINE_STATA_LOOP_RE.search(example["code"])
            )
        with TemporaryDirectory(prefix="published-loops-") as temp_root:
            temporary_root = Path(temp_root)
            root = render_skills.render_all(output_root=temporary_root / "generated")
            rendered = (
                root / "stata-core/references/programming-basics.md"
            ).read_text()
            for index, example in enumerate(loops, start=1):
                section = rendered.split(f"### Example {index}\n", 1)[1]
                self.assertIn(f"- ID: `{example['id']}`.", section)
                code = section.split("```stata\n", 1)[1].split("\n```", 1)[0]
                self.assertEqual(example["code"], code)

            observed: list[str] = []

            def fake_run_stata_do(
                stata_binary: Path,
                do_file: Path,
                cwd: Path,
                completion_marker: str,
                timeout_seconds: int = 90,
            ) -> tuple[CompletedProcess[str], Path]:
                del stata_binary, do_file, timeout_seconds
                body = (cwd / "published-example.do").read_text()
                observed.append(body)
                example_id = cwd.parent.name
                log_path = cwd / "example.log"
                log_path.write_text(
                    f"PASS: programming-basics:{example_id}\n{completion_marker}\n"
                )
                return CompletedProcess(["stata"], 0, "", ""), log_path

            with patch.object(
                validate_skill_pack, "run_stata_do", side_effect=fake_run_stata_do
            ):
                success, _ = validate_skill_pack.validate_entry_examples(
                    Path("/fake/stata"), temporary_root, source
                )
            self.assertTrue(success)
            self.assertEqual([example["code"] + "\n" for example in loops], observed)

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

    def test_restoring_inline_loop_is_detected(self) -> None:
        path = REPO_ROOT / "content/core/programming-basics.yaml"
        entry = yaml.safe_load(path.read_text())
        skill = load_skill_config()["skills"]["core"]
        for line in (
            "foreach x in 1 2 { display `x' }",
            "forvalues x = 1/2 { display `x' }",
        ):
            self.assertIsNotNone(
                lint_skill_pack.INLINE_STATA_LOOP_RE.search(line)
            )
            mutated = deepcopy(entry)
            mutated["examples"][0]["code"] = line
            errors = lint_skill_pack.lint_entry("core", path, mutated, skill)
            self.assertTrue(any("inline foreach/forvalues" in error for error in errors))

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

    def test_expected_error_wrapper_checks_return_code_and_postconditions(self) -> None:
        entry = yaml.safe_load(
            (REPO_ROOT / "content/core/data-management.yaml").read_text()
        )
        example = next(
            item for item in entry["examples"]
            if item["id"] == "core-duplicate-key-error"
        )
        fixture = yaml.safe_load((REPO_ROOT / example["fixture"]).read_text())
        wrapper = validate_skill_pack.example_do_text(
            entry,
            example,
            fixture,
            Path("/private/run/published-example.do"),
            "current-run-marker",
        )
        self.assertIn('capture noisily do "published-example.do"', wrapper)
        self.assertIn("assert `codex_example_rc' == 459", wrapper)
        self.assertIn(fixture["assertions"], wrapper)
        self.assertIn('display "current-run-marker"', wrapper)


if __name__ == "__main__":
    unittest.main()
