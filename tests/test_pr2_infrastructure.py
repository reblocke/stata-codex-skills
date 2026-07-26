from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
import harvest_stata_help  # noqa: E402
import lint_skill_pack  # noqa: E402
import render_skills  # noqa: E402
import validate_skill_pack  # noqa: E402


class ExactHelpResolutionTests(unittest.TestCase):
    def test_exact_stem_does_not_pull_prefix_neighbor(self) -> None:
        with TemporaryDirectory(prefix="stata-help-") as temp_root:
            help_root = Path(temp_root)
            base = help_root / "r"
            base.mkdir()
            exact = base / "regress.sthlp"
            neighbor = base / "regress_postestimation.sthlp"
            exact.write_text("exact", encoding="utf-8")
            neighbor.write_text("neighbor", encoding="utf-8")

            libskillpack.help_index.cache_clear()
            with patch.object(libskillpack, "STATA_ADO_BASE", help_root):
                resolved, missing = libskillpack.find_help_files_exact(["regress"])
            libskillpack.help_index.cache_clear()

            self.assertEqual([], missing)
            self.assertEqual([exact], resolved)

    def test_explicit_glob_is_the_only_way_to_expand(self) -> None:
        with TemporaryDirectory(prefix="stata-help-") as temp_root:
            help_root = Path(temp_root)
            base = help_root / "r"
            base.mkdir()
            first = base / "regress.sthlp"
            second = base / "regress_postestimation.sthlp"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")

            libskillpack.help_index.cache_clear()
            with patch.object(libskillpack, "STATA_ADO_BASE", help_root):
                resolved, missing = libskillpack.find_help_files_exact(
                    [],
                    ["r/regress*.sthlp"],
                )
            libskillpack.help_index.cache_clear()

            self.assertEqual([], missing)
            self.assertEqual([first, second], resolved)

    def test_known_false_matches_are_not_resolved_by_neighbor_names(self) -> None:
        with TemporaryDirectory(prefix="stata-help-") as temp_root:
            help_root = Path(temp_root)
            for relative in (
                "r/rdrobust.sthlp",
                "r/replace_vars.sthlp",
                "c/coefplot_legacy.sthlp",
            ):
                path = help_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("neighbor", encoding="utf-8")

            libskillpack.help_index.cache_clear()
            with patch.object(libskillpack, "STATA_ADO_BASE", help_root):
                resolved, missing = libskillpack.find_help_files_exact(
                    ["rd", "replace", "coefplot"]
                )
            libskillpack.help_index.cache_clear()

            self.assertEqual([], resolved)
            self.assertEqual(["rd", "replace", "coefplot"], missing)

    def test_missing_help_requires_explicit_package_or_upstream_only_flag(self) -> None:
        source = REPO_ROOT / "content" / "packages" / "sample.yaml"
        entry = {
            "slug": "sample",
            "provenance": {
                "local_help_topics": ["missing"],
                "local_help_globs": [],
                "local_help_files": [],
                "package_only": False,
                "upstream_only": False,
            },
        }
        with patch.object(
            harvest_stata_help,
            "find_help_files_exact",
            return_value=([], ["missing"]),
        ):
            _, errors = harvest_stata_help.harvest_entry(
                "packages",
                source,
                entry,
            )
            entry["provenance"]["package_only"] = True
            _, allowed_errors = harvest_stata_help.harvest_entry(
                "packages",
                source,
                entry,
            )

        self.assertTrue(errors)
        self.assertEqual([], allowed_errors)


class ContentSchemaTests(unittest.TestCase):
    def test_nested_canonical_yaml_is_discovered(self) -> None:
        with TemporaryDirectory(prefix="content-tree-") as temp_root:
            content_root = Path(temp_root)
            nested = content_root / "core" / "families" / "sample.yaml"
            nested.parent.mkdir(parents=True)
            nested.write_text("slug: sample\n", encoding="utf-8")
            config = {"skills": {"core": {"content_dir": "core"}}}

            entries = libskillpack.iter_content_entries(content_root, config)

        self.assertEqual(
            [("core", nested, {"slug": "sample"})],
            entries,
        )

    def test_mutating_preflight_command_is_rejected(self) -> None:
        config = libskillpack.load_skill_config()
        source = REPO_ROOT / "content" / "packages" / "asdoc.yaml"
        entry = deepcopy(libskillpack.read_yaml(source))
        entry["preflight_commands"] = ["capture ssc install asdoc"]

        errors = lint_skill_pack.lint_entry(
            "packages",
            source,
            entry,
            config["skills"]["packages"],
        )

        self.assertTrue(
            any("read-only discovery commands" in error for error in errors),
            errors,
        )

    def test_package_preflight_runs_uncaptured_after_install(self) -> None:
        entry = {
            "slug": "sample",
            "install_commands": ["ssc install sample"],
            "preflight_commands": [
                "quietly capture noisily which sample",
                "capture ado describe sample",
            ],
            "smoke_test": "sample",
        }

        text = validate_skill_pack.package_do_text(
            entry,
            Path("/tmp/isolated-plus"),
            "EXACT-MARKER",
        )

        self.assertNotIn("capture", text)
        self.assertLess(text.index("ssc install sample"), text.index("which sample"))
        self.assertLess(text.index("ado describe sample"), text.index("\nsample\n"))

    def test_upstream_lock_repository_must_match_configured_repository(self) -> None:
        with TemporaryDirectory(prefix="upstream-lock-url-") as temp_root:
            lock_root = Path(temp_root)
            (lock_root / "upstream.yaml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "repository": {
                            "url": "https://example.invalid/other.git",
                            "commit": "a" * 40,
                            "expected_commit": "a" * 40,
                        },
                        "files": {},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with patch.multiple(
                lint_skill_pack,
                LOCK_ROOT=lock_root,
                UPSTREAM_REPO_URL="https://example.invalid/configured.git",
            ):
                errors = lint_skill_pack.lint_upstream_lock([])

        self.assertTrue(
            any(
                "repository.url must exactly match the configured upstream repository"
                in error
                for error in errors
            ),
            errors,
        )


class DeterministicRenderTests(unittest.TestCase):
    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_current_content_renders_deterministically_with_alias_and_metadata(self) -> None:
        with TemporaryDirectory(prefix="render-a-") as first_root, TemporaryDirectory(
            prefix="render-b-"
        ) as second_root:
            first = Path(first_root)
            second = Path(second_root)
            render_skills.render_all(output_root=first)
            render_skills.render_all(output_root=second)
            first_snapshot = self.snapshot(first)
            second_snapshot = self.snapshot(second)

        self.assertEqual(first_snapshot, second_snapshot)
        self.assertEqual(73, len(first_snapshot))
        alias = first_snapshot[
            "stata-packages/packages/diagnostics.md"
        ].decode("utf-8")
        self.assertIn(
            "stata-core/references/regression-diagnostics.md",
            alias,
        )
        metadata = yaml.safe_load(
            first_snapshot["stata-core/agents/openai.yaml"].decode("utf-8")
        )
        self.assertEqual("Stata Core", metadata["interface"]["display_name"])
        core_skill = first_snapshot["stata-core/SKILL.md"].decode("utf-8")
        self.assertIn(
            "Stata basics, open dataset, inspect data, browse data, save dataset, "
            "sysuse, use, describe, summarize, list, browse, save, clear",
            core_skill,
        )
        rdrobust = first_snapshot[
            "stata-packages/packages/rdrobust.md"
        ].decode("utf-8")
        self.assertIn(
            "cross-skill: load the `stata-core` skill first",
            rdrobust,
        )


class StructuredPromptTests(unittest.TestCase):
    def test_required_cross_skill_boundaries_are_present(self) -> None:
        data = libskillpack.read_yaml(REPO_ROOT / "tests" / "prompts" / "cases.yaml")
        case_ids = {case["id"] for case in data["cases"]}
        required = {
            "boundary-built-in-didregress-versus-csdid",
            "boundary-csdid-versus-built-in-didregress",
            "boundary-manual-rd-versus-rdrobust",
            "boundary-rdrobust-versus-manual-rd",
            "boundary-built-in-table-versus-estout",
            "boundary-esttab-versus-built-in-table",
            "boundary-ivreghdfe-versus-ivregress-or-reghdfe",
            "boundary-ado-programming-versus-native-plugin",
            "boundary-native-plugin-sdk",
        }

        self.assertTrue(required.issubset(case_ids))


class StataTrackTests(unittest.TestCase):
    def test_parser_preserves_multiple_distribution_records(self) -> None:
        track = "\n".join(
            [
                "S https://example.test/one",
                "N one.pkg",
                "d Distribution-Date: 20260102",
                "f o/one.ado",
                "e",
                "S https://example.test/two",
                "N two.pkg",
                "d Distribution-Date: 20260304",
                "f t/two.ado",
                "f t/two.sthlp",
                "e",
            ]
        )

        modules = validate_skill_pack.parse_stata_track(track)

        self.assertEqual(["one.pkg", "two.pkg"], [item["descriptor"] for item in modules])
        self.assertEqual(
            ["t/two.ado", "t/two.sthlp"],
            modules[1]["files"],
        )

    def test_runtime_lock_ignores_tracking_metadata_but_rejects_unknown_files(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="package-lock-") as temp_root:
            root = Path(temp_root)
            lock_root = root / "locks"
            plus = root / "plus"
            package_file = plus / "s" / "sample.ado"
            package_file.parent.mkdir(parents=True)
            package_file.write_text("program sample\nend\n", encoding="utf-8")
            (plus / "stata.trk").write_text(
                "\n".join(
                    [
                        "S https://example.test/sample",
                        "N sample.pkg",
                        "d Distribution-Date: 20260102",
                        "f s/sample.ado",
                        "f s/sample.ado",
                        "e",
                    ]
                ),
                encoding="utf-8",
            )
            (plus / "backup.trk").write_text(
                "mutable installer state",
                encoding="utf-8",
            )
            libskillpack.write_yaml(
                lock_root / "packages.yaml",
                {
                    "schema_version": 1,
                    "packages": {
                        "sample": {
                            "distributions": [
                                {
                                    "source": "https://example.test/sample",
                                    "descriptor": "sample.pkg",
                                    "distribution_date": "20260102",
                                    "files": {
                                        "s/sample.ado": libskillpack.sha256_file(
                                            package_file
                                        )
                                    },
                                }
                            ],
                            "generated_files": {},
                        }
                    },
                },
            )

            with patch.object(validate_skill_pack, "LOCK_ROOT", lock_root):
                success, diagnostics = (
                    validate_skill_pack.verify_package_install_lock(
                        "sample",
                        plus,
                    )
                )
                self.assertTrue(success, diagnostics)

                (plus / "unexpected.txt").write_text("drift", encoding="utf-8")
                success, diagnostics = (
                    validate_skill_pack.verify_package_install_lock(
                        "sample",
                        plus,
                    )
                )

            self.assertFalse(success)
            self.assertIn("unexpected installed files", diagnostics)


if __name__ == "__main__":
    unittest.main()
