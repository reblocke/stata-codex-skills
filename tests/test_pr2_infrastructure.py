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
import validate_skill_pack  # noqa: E402


class DocumentationStyleTests(unittest.TestCase):
    def test_config_rejects_title_case_presentation_fields(self) -> None:
        config = deepcopy(libskillpack.load_skill_config())
        config["skills"]["core"]["heading"] = "Stata Core Skill"
        config["skills"]["core"]["section_order"][0] = (
            "Programming And Mata"
        )
        config["skills"]["core"]["interface"]["display_name"] = (
            "Stata Core"
        )

        errors = lint_skill_pack.lint_config(config)

        self.assertTrue(any("heading must use sentence case" in e for e in errors))
        self.assertTrue(any("section must use sentence case" in e for e in errors))
        self.assertTrue(any("display_name must use sentence case" in e for e in errors))

    def test_entry_rejects_title_case_titles(self) -> None:
        config = libskillpack.load_skill_config()
        skill = config["skills"]["core"]
        path, entry = next(
            (path, deepcopy(candidate))
            for skill_key, path, candidate in libskillpack.iter_content_entries(
                REPO_ROOT / "content", config
            )
            if skill_key == "core"
        )
        entry["title"] = "Reusable Stata Programs"

        errors = lint_skill_pack.lint_entry("core", path, entry, skill)

        self.assertTrue(any("title must use sentence case" in e for e in errors))


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


class RoutingPrecisionTests(unittest.TestCase):
    def test_normalized_collision_requires_exact_clarify_boundary(self) -> None:
        entries = [
            (
                "core",
                Path("content/core/first.yaml"),
                {"slug": "first", "routing_terms": ["Shared workflow"]},
            ),
            (
                "packages",
                Path("content/packages/second.yaml"),
                {"slug": "second", "routing_terms": ["shared-workflow"]},
            ),
        ]
        config = {"routing_boundaries": []}

        errors = lint_skill_pack.lint_routing_collisions(config, entries)

        self.assertTrue(any("undeclared normalized routing collision" in error for error in errors))

        config["routing_boundaries"] = [
            {
                "term": "shared workflow",
                "routes": ["core/first", "packages/second"],
                "action": "clarify",
                "guidance": "Ask which implementation the user wants.",
            }
        ]

        self.assertEqual(
            [],
            lint_skill_pack.lint_routing_collisions(config, entries),
        )

    def test_normalized_duplicate_and_generic_routing_terms_fail(self) -> None:
        config = libskillpack.load_skill_config()
        source = REPO_ROOT / "content" / "core" / "tables-reporting.yaml"
        entry = deepcopy(libskillpack.read_yaml(source))
        entry["routing_terms"] = ["Regression table", "regression-table", "predict"]

        errors = lint_skill_pack.lint_entry(
            "core",
            source,
            entry,
            config["skills"]["core"],
        )

        self.assertTrue(
            any("normalized duplicates" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("too generic" in error for error in errors),
            errors,
        )


class StructuredPromptTests(unittest.TestCase):
    @staticmethod
    def lint_cases(
        cases: list[dict],
        *,
        canonical_paths: set[str] | None = None,
        canonical_triggers: dict[str, str] | None = None,
        routing_boundaries: list[dict] | None = None,
    ) -> list[str]:
        with TemporaryDirectory(prefix="prompt-case-") as temp_root:
            prompt_path = Path(temp_root) / "cases.yaml"
            libskillpack.write_yaml(
                prompt_path,
                {
                    "schema_version": 2,
                    "cases": cases,
                },
            )
            config = {
                "skills": {
                    "core": {"name": "stata-core"},
                    "packages": {"name": "stata-packages"},
                },
                "routing_boundaries": routing_boundaries or [],
            }
            return lint_skill_pack.lint_prompt_cases(
                config,
                canonical_paths or set(),
                set(),
                prompt_path=prompt_path,
                canonical_triggers=canonical_triggers,
            )

    def test_malformed_case_reports_errors_instead_of_crashing(self) -> None:
        with TemporaryDirectory(prefix="prompt-case-invalid-") as temp_root:
            prompt_path = Path(temp_root) / "cases.yaml"
            libskillpack.write_yaml(
                prompt_path,
                {
                    "schema_version": 2,
                    "cases": [
                        {
                            "id": "invalid",
                            "action": "clarify",
                            "prompt": None,
                            "routing_term": "shared workflow",
                            "expected_skill": None,
                            "expected_refs": [{}],
                            "forbidden_routes": [None],
                            "boundary": True,
                        }
                    ],
                },
            )
            config = {
                "skills": {
                    "core": {"name": "stata-core"},
                },
                "routing_boundaries": [
                    {
                        "term": "shared workflow",
                    }
                ],
            }

            errors = lint_skill_pack.lint_prompt_cases(
                config,
                set(),
                set(),
                prompt_path=prompt_path,
            )

        self.assertTrue(any("prompt must be nonempty" in error for error in errors))
        self.assertTrue(any("expected_refs" in error for error in errors))
        self.assertTrue(any("forbidden_routes" in error for error in errors))

    def test_route_reference_must_belong_to_expected_skill(self) -> None:
        route = "stata-packages/packages/rdrobust.md"
        errors = self.lint_cases(
            [
                {
                    "id": "wrong-skill",
                    "prompt": "Estimate a robust bias-corrected discontinuity.",
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                }
            ],
            canonical_paths={route},
        )

        self.assertTrue(
            any(
                "does not belong to expected_skill 'stata-core'" in error
                for error in errors
            ),
            errors,
        )

    def test_clarify_and_abstain_require_boundary_true(self) -> None:
        errors = self.lint_cases(
            [
                {
                    "id": "clarify-without-boundary",
                    "prompt": "Which regression table workflow do you mean?",
                    "action": "clarify",
                    "routing_term": "regression table",
                    "expected_skill": None,
                    "expected_refs": [],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "abstain-without-boundary",
                    "prompt": "Summarize this unrelated article.",
                    "action": "abstain",
                    "expected_skill": None,
                    "expected_refs": [],
                    "forbidden_routes": [],
                    "boundary": False,
                },
            ],
            routing_boundaries=[{"term": "regression table"}],
        )

        self.assertTrue(
            any("clarify action requires boundary true" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("abstain action requires boundary true" in error for error in errors),
            errors,
        )

    def test_clarify_routing_term_requires_token_boundaries(self) -> None:
        case = {
            "id": "clarify-difference-in-differences",
            "prompt": "",
            "action": "clarify",
            "routing_term": "difference in differences",
            "expected_skill": None,
            "expected_refs": [],
            "forbidden_routes": [],
            "boundary": True,
        }
        routing_boundaries = [{"term": "difference in differences"}]

        accepted = self.lint_cases(
            [
                {
                    **case,
                    "prompt": (
                        "Do you mean difference-in-differences with built-in "
                        "tooling or a community estimator?"
                    ),
                }
            ],
            routing_boundaries=routing_boundaries,
        )

        self.assertFalse(
            any(
                "prompt must state its ambiguous routing_term" in error
                for error in accepted
            ),
            accepted,
        )
        for mutated_term in (
            "difference-in-differencesx",
            "difference-in-differences_x",
            "x_difference-in-differences",
            "x_difference-in-differences_x",
            "αdifference-in-differences",
            "difference-in-differencesβ",
            "αdifference-in-differencesβ",
        ):
            with self.subTest(mutated_term=mutated_term):
                errors = self.lint_cases(
                    [
                        {
                            **case,
                            "prompt": (
                                f"Do you mean {mutated_term} with built-in "
                                "tooling or a community estimator?"
                            ),
                        }
                    ],
                    routing_boundaries=routing_boundaries,
                )
                self.assertTrue(
                    any(
                        "prompt must state its ambiguous routing_term" in error
                        for error in errors
                    ),
                    errors,
                )

    def test_single_routing_term_rejects_identifier_attachment(self) -> None:
        self.assertTrue(
            lint_skill_pack.contains_routing_term(
                "Use didregress with panel data.",
                "didregress",
            )
        )
        self.assertTrue(
            lint_skill_pack.contains_routing_term(
                "Use 𝐃𝐈𝐃𝐑𝐄𝐆𝐑𝐄𝐒𝐒 with panel data.",
                "didregress",
            )
        )
        for mutated_term in (
            "didregressx",
            "didregress_x",
            "x_didregress",
            "x_didregress_x",
            "αdidregress",
            "didregressβ",
            "αdidregressβ",
            "édidregress",
            "İdidregress",
            "didregress\u0338",
            "\u200bdidregress",
            "\u200cdidregress",
            "\u200ddidregress",
            "didregress\u200b",
            "didregress\u200c",
            "didregress\u200d",
            "did\u200bregress",
            "did\u200cregress",
            "did\u200dregress",
            "did🔥regress",
        ):
            with self.subTest(mutated_term=mutated_term):
                self.assertFalse(
                    lint_skill_pack.contains_routing_term(
                        f"Use {mutated_term} with panel data.",
                        "didregress",
                    )
                )

    def test_routing_normalization_keeps_c_cplusplus_and_csharp_distinct(
        self,
    ) -> None:
        terms = ("native C plugin", "native C++ plugin", "native C# plugin")
        equivalents = {
            "native C plugin": (
                "Use a native C plugin.",
                "Use a native Ｃ plugin.",
                "Use a native 𝐂 plugin.",
            ),
            "native C++ plugin": (
                "Use a native C++ plugin.",
                "Use a native Ｃ＋＋ plugin.",
                "Use a native 𝐂++ plugin.",
            ),
            "native C# plugin": (
                "Use a native C# plugin.",
                "Use a native Ｃ＃ plugin.",
                "Use a native 𝐂# plugin.",
            ),
        }
        for term, prompts in equivalents.items():
            for prompt in prompts:
                with self.subTest(term=term, prompt=prompt):
                    self.assertTrue(
                        lint_skill_pack.contains_routing_term(prompt, term)
                    )
                    for other_term in terms:
                        if other_term != term:
                            self.assertFalse(
                                lint_skill_pack.contains_routing_term(
                                    prompt,
                                    other_term,
                                )
                            )
        for prompt in (
            "Use a native C+++ plugin.",
            "Use a native C++++ plugin.",
            "Use a native C + + plugin.",
            "Use a native Objective-C plugin.",
            "Use a native C/C++ plugin.",
            "Use a native C++17 plugin.",
        ):
            with self.subTest(prompt=prompt):
                for term in terms:
                    self.assertFalse(
                        lint_skill_pack.contains_routing_term(prompt, term)
                    )

    def test_oversized_prompt_token_fails_before_trigger_comparison(
        self,
    ) -> None:
        case = {
            "id": "oversized-token",
            "prompt": "a" * (lint_skill_pack.MAX_COPY_TOKEN_LENGTH + 1),
            "action": "abstain",
            "expected_skill": None,
            "expected_refs": [],
            "forbidden_routes": [],
            "boundary": True,
        }
        with patch.object(
            lint_skill_pack,
            "trigger_copy_kind",
        ) as trigger_copy_kind:
            errors = self.lint_cases(
                [case],
                canonical_triggers={"stata-core/example": "Example trigger"},
            )

        trigger_copy_kind.assert_not_called()
        self.assertTrue(
            any("token longer than" in error for error in errors),
            errors,
        )

    def test_normalization_expansion_fails_before_trigger_comparison(
        self,
    ) -> None:
        expanding = "\ufdfa" * lint_skill_pack.MAX_COPY_TEXT_LENGTH
        case = {
            "id": "normalization-expansion",
            "prompt": expanding,
            "action": "abstain",
            "expected_skill": None,
            "expected_refs": [],
            "forbidden_routes": [],
            "boundary": True,
        }
        with patch.object(
            lint_skill_pack,
            "trigger_copy_kind",
        ) as trigger_copy_kind:
            errors = self.lint_cases(
                [case],
                canonical_triggers={"stata-core/example": "Example trigger"},
            )

        trigger_copy_kind.assert_not_called()
        self.assertFalse(
            lint_skill_pack.copy_text_within_limits(expanding)
        )
        self.assertTrue(
            any("normalizes beyond" in error for error in errors),
            errors,
        )

    def test_trigger_copy_variants_fail(self) -> None:
        route = "stata-core/references/linear-regression.md"
        trigger = (
            "Use when the user asks for ordinary least squares, robust standard "
            "errors, factor-variable interactions, adjusted predictions, and "
            "fitted values."
        )
        fullwidth_trigger = "".join(
            chr(ord(character) + 0xFEE0)
            if "!" <= character <= "~"
            else character
            for character in trigger
        )
        errors = self.lint_cases(
            [
                {
                    "id": "normalized-exact-copy",
                    "prompt": trigger.upper() + "!",
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "embedded-copy",
                    "prompt": (
                        f"{trigger} Please also provide a complete reproducible "
                        "do-file with comments and output checks."
                    ),
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "noncontiguous-copy",
                    "prompt": trigger.replace(
                        ", ",
                        ", with a complete example for each choice, ",
                    ),
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "reordered-copy",
                    "prompt": " ".join(reversed(trigger.split())),
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "near-verbatim-copy",
                    "prompt": trigger.replace(
                        "ordinary least squares",
                        "OLS",
                    ),
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "fullwidth-copy",
                    "prompt": fullwidth_trigger,
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "zero-width-copy",
                    "prompt": trigger.replace(
                        "regression",
                        "reg\u200bression",
                    ),
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
                {
                    "id": "combining-mark-copy",
                    "prompt": trigger.replace(
                        "regression",
                        "reg\u0338ression",
                    ),
                    "action": "route",
                    "expected_skill": "stata-core",
                    "expected_refs": [route],
                    "forbidden_routes": [],
                    "boundary": False,
                },
            ],
            canonical_paths={route},
            canonical_triggers={route: trigger},
        )

        self.assertTrue(
            any("normalized exact copy" in error for error in errors),
            errors,
        )
        self.assertGreaterEqual(
            sum("copy of the canonical trigger" in error for error in errors),
            8,
            errors,
        )
        self.assertTrue(
            any("embedded normalized copy" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("near-verbatim copy" in error for error in errors),
            errors,
        )
        self.assertGreaterEqual(
            sum("high trigger-token coverage copy" in error for error in errors),
            1,
            errors,
        )
        self.assertGreaterEqual(
            sum("near-verbatim copy" in error for error in errors),
            2,
            errors,
        )


class StataTrackTests(unittest.TestCase):
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
                lock_root / "packages" / "sample.yaml",
                {
                    "schema_version": 1,
                    "slug": "sample",
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
                },
            )

            with patch.object(
                validate_skill_pack,
                "PACKAGE_LOCK_ROOT",
                lock_root / "packages",
            ):
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

    def test_runtime_lock_rejects_nonmapping_selected_lock(self) -> None:
        with TemporaryDirectory(prefix="package-lock-invalid-") as temp_root:
            lock_root = Path(temp_root) / "packages"
            lock_root.mkdir(parents=True)
            (lock_root / "sample.yaml").write_text("- invalid\n", encoding="utf-8")

            with patch.object(
                validate_skill_pack,
                "PACKAGE_LOCK_ROOT",
                lock_root,
            ):
                success, diagnostics = (
                    validate_skill_pack.verify_package_install_lock(
                        "sample",
                        Path(temp_root) / "plus",
                    )
                )

        self.assertFalse(success)
        self.assertIn("invalid package lock", diagnostics)

    def test_runtime_lock_rejects_unsafe_slug_before_path_lookup(self) -> None:
        success, diagnostics = validate_skill_pack.verify_package_install_lock(
            "../sample",
            Path("/unused"),
        )

        self.assertFalse(success)
        self.assertIn("Unsafe package lock slug", diagnostics)


if __name__ == "__main__":
    unittest.main()
