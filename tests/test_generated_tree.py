from __future__ import annotations

import re
import subprocess
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
import render_skills  # noqa: E402


class GeneratedTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = TemporaryDirectory(
            prefix="generated-tree-test-"
        )
        cls.addClassCleanup(cls._temporary_directory.cleanup)
        cls.output_root = (
            Path(cls._temporary_directory.name) / "generated"
        )
        cls.config = libskillpack.load_skill_config()
        cls.entries = list(
            libskillpack.iter_content_entries(
                REPO_ROOT / "content",
                cls.config,
            )
        )

        render_skills.render_all(output_root=cls.output_root)

    def test_each_skill_bundles_the_standalone_runner_without_source_drift(self) -> None:
        source_bytes = (REPO_ROOT / "scripts" / "stata_runner.py").read_bytes()
        for skill in self.config["skills"].values():
            with self.subTest(skill=skill["folder"]):
                helper = self.output_root / skill["folder"] / "scripts" / "stata_runner.py"
                self.assertEqual(source_bytes, helper.read_bytes())
                result = subprocess.run(
                    [sys.executable, "-B", str(helper), "--help"],
                    cwd=self.output_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("--run-dir", result.stdout)

    def test_every_root_renders_reviewed_task_modes(self) -> None:
        for skill in self.config["skills"].values():
            root = (self.output_root / skill["folder"] / "SKILL.md").read_text()
            for mode, rule in self.config["workflow_modes"].items():
                with self.subTest(skill=skill["folder"], mode=mode):
                    self.assertIn(f"**{mode.replace('_', ' ')}**: {rule}", root)
            self.assertIn("do not install packages, execute data", root)

    def test_core_root_has_direct_routes_for_cross_category_tasks(self) -> None:
        root = (self.output_root / "stata-core/SKILL.md").read_text()
        for route in (
            "references/workflow-best-practices.md",
            "references/variables-operators.md",
            "references/advanced-programming.md",
        ):
            with self.subTest(route=route):
                self.assertIn(f"]({route})", root)

    def test_compact_categories_and_alias_lookup_preserve_routes(self) -> None:
        for skill_key, skill in self.config["skills"].items():
            folder = self.output_root / skill["folder"]
            root_text = (folder / "SKILL.md").read_text(encoding="utf-8")
            lookup = (folder / "routing/aliases.md").read_text(encoding="utf-8")
            self.assertIn("(routing/aliases.md)", root_text)
            self.assertIn("open its technical reference directly", root_text)
            for rule in self.config["common_contract"]:
                self.assertIn(rule, root_text)
            entries = [
                entry for key, _, entry in self.entries if key == skill_key
            ]
            sections = render_skills.routing_sections(skill, entries)
            for section in sections:
                with self.subTest(skill=skill_key, section=section["name"]):
                    self.assertIn(f"({section['route_path']})", root_text)
                    self.assertIn(section["guidance"], root_text)
                    index_text = (folder / section["route_path"]).read_text(
                        encoding="utf-8"
                    )
                    for entry in section["entries"]:
                        route = f"{skill['route_dir']}/{entry['slug']}.md"
                        self.assertIn(f"(../{route})", index_text)
                        self.assertIn(entry["title"], index_text)
                        self.assertNotIn(entry["trigger"], index_text)
                        self.assertNotIn(entry["trigger"], root_text)
                        cues = entry.get("route_cues") or entry["commands"][:3]
                        for cue in cues:
                            self.assertIn(cue, index_text)
                        for field in ("aliases", "commands", "routing_terms"):
                            for value in entry[field]:
                                self.assertIn(
                                    f"`{value.casefold()}`",
                                    lookup.casefold(),
                                    f"missing lookup term {value!r} for {route}",
                                )
                        self.assertIn(f"(../{route})", lookup)
                    section_routes = {
                        f"{skill_key}/{entry['slug']}"
                        for entry in section["entries"]
                    }
                    for boundary in self.config["routing_boundaries"]:
                        if section_routes.intersection(boundary["routes"]):
                            self.assertIn(boundary["guidance"], index_text)

            provenance = (folder / "PROVENANCE.md").read_text(encoding="utf-8")
            for entry in entries:
                route = f"{skill['route_dir']}/{entry['slug']}.md"
                self.assertIn(f"`{route}`", provenance)
            metadata = yaml.safe_load(
                (folder / "agents" / "openai.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(skill["interface"], metadata["interface"])

    def test_generated_markdown_links_resolve_inside_complete_skill_tree(self) -> None:
        for path in self.output_root.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if "://" in target or target.startswith("#"):
                    continue
                with self.subTest(file=path.name, target=target):
                    resolved = (path.parent / target.split("#", 1)[0]).resolve()
                    self.assertTrue(resolved.is_relative_to(self.output_root.resolve()))
                    self.assertTrue(resolved.is_file())

    def test_existing_tree_accepts_new_indexes_and_legacy_shape(self) -> None:
        import shutil

        with TemporaryDirectory(prefix="generated-index-upgrade-") as temp_root:
            target = Path(temp_root) / "generated"
            shutil.copytree(self.output_root, target)
            render_skills.preflight_existing_output_root(target)
            for skill in self.config["skills"].values():
                shutil.rmtree(target / skill["folder"] / "routing")
                shutil.rmtree(target / skill["folder"] / "scripts")
                (target / skill["folder"] / "references" / "unattended-execution.md").unlink()
                if skill["folder"] == "stata-packages":
                    (target / skill["folder"] / "references").rmdir()
            render_skills.preflight_existing_output_root(target)

    def test_unexpected_scripts_prevent_replacing_an_existing_tree(self) -> None:
        import shutil

        with TemporaryDirectory(prefix="generated-script-guard-") as temp_root:
            target = Path(temp_root) / "generated"
            shutil.copytree(self.output_root, target)
            extra = target / "stata-core" / "scripts" / "unrelated.py"
            extra.write_text("# preserve local work\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "scripts/ must contain only"):
                render_skills.preflight_existing_output_root(target)
            self.assertTrue(extra.is_file())

    def test_routing_indexes_do_not_allow_extra_directories_or_non_markdown(self) -> None:
        import shutil

        for mutation in ("extra-directory", "non-markdown"):
            with TemporaryDirectory(prefix="generated-index-guard-") as temp_root:
                target = Path(temp_root) / "generated"
                shutil.copytree(self.output_root, target)
                if mutation == "extra-directory":
                    (target / "stata-core" / "unrelated").mkdir()
                else:
                    (target / "stata-core" / "routing" / "private.csv").write_text(
                        "must be preserved"
                    )
                with self.assertRaises(ValueError):
                    render_skills.preflight_existing_output_root(target)

    def test_section_guidance_cannot_omit_or_invent_categories(self) -> None:
        from copy import deepcopy
        import lint_skill_pack

        for mutation in ("missing", "extra", "empty"):
            config = deepcopy(self.config)
            guidance = config["skills"]["core"]["section_guidance"]
            key = next(iter(guidance))
            if mutation == "missing":
                del guidance[key]
            elif mutation == "extra":
                guidance["Unconfigured category"] = "A useful description"
            else:
                guidance[key] = " "
            errors = lint_skill_pack.lint_config(config)
            self.assertTrue(any("section_guidance" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
