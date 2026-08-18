from __future__ import annotations

from contextlib import redirect_stdout
import io
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

        output = io.StringIO()
        with redirect_stdout(output):
            rendered_root = render_skills.render_all(
                output_root=cls.output_root
            )
        cls.library_output = output.getvalue()
        cls.rendered_root = rendered_root

    @classmethod
    def canonical_paths(cls) -> dict[str, tuple[str, dict]]:
        return {
            (
                f"{cls.config['skills'][skill_key]['folder']}/"
                f"{cls.config['skills'][skill_key]['route_dir']}/"
                f"{entry['slug']}.md"
            ): (skill_key, entry)
            for skill_key, _, entry in cls.entries
        }

    def test_complete_tree_has_exactly_the_expected_73_files(self) -> None:
        canonical = self.canonical_paths()
        root_files = {
            f"{skill['folder']}/{relative}"
            for skill in self.config["skills"].values()
            for relative in (
                "SKILL.md",
                "PROVENANCE.md",
                "agents/openai.yaml",
            )
        }
        aliases = {
            (
                f"{self.config['skills'][alias['from_skill']]['folder']}/"
                f"{alias['from_route']}"
            )
            for alias in self.config["route_aliases"]
        }
        expected = root_files | set(canonical) | aliases
        actual = {
            path.relative_to(self.output_root).as_posix()
            for path in self.output_root.rglob("*")
            if path.is_file()
        }

        self.assertEqual(63, len(canonical))
        self.assertEqual(
            {"stata-packages/packages/diagnostics.md"},
            aliases,
        )
        self.assertEqual(73, len(expected))
        self.assertEqual(expected, actual)

    def test_library_is_quiet_and_cli_prints_one_summary(self) -> None:
        self.assertEqual("", self.library_output)
        self.assertEqual(self.output_root.resolve(), self.rendered_root)

        with TemporaryDirectory(prefix="generated-tree-cli-") as temp_root:
            target = Path(temp_root) / "generated"
            output = io.StringIO()
            with redirect_stdout(output):
                result = render_skills.main(
                    ["--output-root", str(target)]
                )

        self.assertEqual(0, result)
        self.assertEqual(
            [f"Rendered 3 Stata skills to {target.resolve()}"],
            output.getvalue().splitlines(),
        )

    def test_leaf_references_are_concise_but_substantive(self) -> None:
        by_slug = {
            entry["slug"]: (skill_key, entry)
            for skill_key, _, entry in self.entries
        }
        legacy_targets = {
            Path(alias["from_route"]).stem: alias["to_slug"]
            for alias in self.config["route_aliases"]
        }

        for relative, (skill_key, entry) in self.canonical_paths().items():
            with self.subTest(reference=relative):
                text = (self.output_root / relative).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("\n## Routing terms\n", text)
                self.assertNotIn(
                    "\n## Supported aliases and names\n",
                    text,
                )
                self.assertNotIn("\n## Provenance\n", text)

                self.assertIn(entry["trigger"], text)
                self.assertIn(
                    f"- Mode: `{entry['validation_mode']}`",
                    text,
                )
                self.assertIn(
                    f"- Case: {entry['validation_case']}",
                    text,
                )
                for field in (
                    "commands",
                    "syntax_patterns",
                    "gotchas",
                    "assumptions",
                    "workflows",
                    "preflight_commands",
                    "install_commands",
                ):
                    for value in entry.get(field, []):
                        self.assertIn(value, text)

                final_workflow = entry["workflows"][-1]
                self.assertIn(
                    (
                        f"{len(entry['workflows'])}. {final_workflow}"
                        "\n\n## Validation"
                    ),
                    text,
                )

                for related_slug in entry.get("related_refs", []):
                    canonical_slug = legacy_targets.get(
                        related_slug,
                        related_slug,
                    )
                    target_skill_key, target_entry = by_slug[canonical_slug]
                    target_skill = self.config["skills"][
                        target_skill_key
                    ]
                    route = (
                        f"{target_skill['route_dir']}/"
                        f"{target_entry['slug']}.md"
                    )
                    if target_skill_key != skill_key:
                        route = f"{target_skill['name']}/{route}"
                        self.assertIn(
                            (
                                f"`{route}` — cross-skill: load the "
                                f"`{target_skill['name']}` skill first."
                            ),
                            text,
                        )
                    else:
                        self.assertIn(f"`{route}`", text)

    def test_installation_sections_are_explicit_and_safe(self) -> None:
        preflight_heading = (
            "## Installation preflight (read-only)"
        )
        install_heading = "## Optional: Install in an isolated directory"

        for relative, (_, entry) in self.canonical_paths().items():
            with self.subTest(reference=relative):
                text = (self.output_root / relative).read_text(
                    encoding="utf-8"
                )
                normalized = " ".join(text.split())
                if entry.get("preflight_commands"):
                    self.assertIn(preflight_heading, text)
                    self.assertIn(
                        "Run these read-only checks before proposing "
                        "installation:",
                        text,
                    )
                else:
                    self.assertNotIn(preflight_heading, text)

                if entry.get("install_commands"):
                    self.assertIn(install_heading, text)
                    self.assertIn("user authorization", normalized)
                    self.assertIn(
                        "Use only an isolated validation directory",
                        normalized,
                    )
                    self.assertIn(
                        (
                            "never use the user's normal `PLUS` or "
                            "`PERSONAL` paths"
                        ),
                        normalized,
                    )
                else:
                    self.assertNotIn(install_heading, text)

    def test_root_routing_and_metadata_remain_authoritative(self) -> None:
        entries_by_skill: dict[str, list[dict]] = {
            skill_key: []
            for skill_key in self.config["skills"]
        }
        for skill_key, _, entry in self.entries:
            entries_by_skill[skill_key].append(entry)

        for skill_key, skill in self.config["skills"].items():
            with self.subTest(skill=skill["name"]):
                folder = self.output_root / skill["folder"]
                root_text = (folder / "SKILL.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn(
                    "## Routing by terms, names, and task intent",
                    root_text,
                )
                self.assertIn(
                    "Supported aliases and commands",
                    root_text,
                )
                self.assertIn("## Provenance", root_text)
                self.assertIn("`PROVENANCE.md`", root_text)
                for entry in entries_by_skill[skill_key]:
                    self.assertIn(entry["trigger"], root_text)
                    for alias in entry["aliases"]:
                        self.assertIn(
                            alias.casefold(),
                            root_text.casefold(),
                        )
                    for term in entry["routing_terms"]:
                        self.assertIn(term, root_text)

                provenance = (folder / "PROVENANCE.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn("# Provenance index", provenance)
                self.assertIn("## Embedded lock identifiers", provenance)
                for entry in entries_by_skill[skill_key]:
                    route = (
                        f"{skill['route_dir']}/{entry['slug']}.md"
                    )
                    self.assertIn(f"`{route}`", provenance)

                metadata = yaml.safe_load(
                    (
                        folder / "agents" / "openai.yaml"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    skill["interface"],
                    metadata["interface"],
                )


if __name__ == "__main__":
    unittest.main()
