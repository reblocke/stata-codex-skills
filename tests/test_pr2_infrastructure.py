from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import sys
import tomllib
import unittest
import unicodedata
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
import harvest_stata_help  # noqa: E402
import lint_skill_pack  # noqa: E402
import render_skills  # noqa: E402
import validate_skill_pack  # noqa: E402


def fullwidth_ascii(value: str) -> str:
    return "".join(
        chr(ord(character) + 0xFEE0)
        if "!" <= character <= "~"
        else character
        for character in value
    )


def insert_inside_ascii_words(value: str, marker: str) -> str:
    result: list[str] = []
    for index, character in enumerate(value):
        result.append(character)
        if (
            character.isascii()
            and character.isalnum()
            and index + 1 < len(value)
            and value[index + 1].isascii()
            and value[index + 1].isalnum()
        ):
            result.append(marker)
    return "".join(result)


def pad_meaningful_symbol_tokens(value: str, marker: str) -> str:
    return value.replace("C++", f"C++{marker}").replace("C#", f"C#{marker}")


def split_cplusplus_padding(value: str, marker: str) -> str:
    replacement = "C+# +" if marker == "#" else "C+ +"
    return value.replace("C++", replacement)


def attach_cplusplus_continuation_to_next_word(value: str) -> str:
    transitions = (
        ("C++, extern", "C+ +extern"),
        ("C++ backend", "C+ +backend"),
        ("C/C++ compilation", "C/C+ +compilation"),
    )
    for source, replacement in transitions:
        value = value.replace(
            insert_inside_ascii_words(source, "+"),
            insert_inside_ascii_words(replacement, "+"),
        )
    return value


def fuse_cplusplus_with_following_word(value: str) -> str:
    transitions = (
        ("C++, extern", "C++extern"),
        ("C++ backend", "C++backend"),
        ("C/C++ compilation", "C/C++compilation"),
    )
    for source, replacement in transitions:
        value = value.replace(
            insert_inside_ascii_words(source, "+"),
            insert_inside_ascii_words(replacement, "+"),
        )
    return value


def fuse_cplusplus_with_adjacent_words(value: str) -> str:
    transitions = (
        ("needs C++, extern", "needsC++extern"),
        ("vendored C++ backend", "vendoredC++backend"),
        (
            "separate C/C++ compilation",
            "separateC/C++compilation",
        ),
    )
    for source, replacement in transitions:
        value = value.replace(
            insert_inside_ascii_words(source, "+"),
            insert_inside_ascii_words(replacement, "+"),
        )
    return value


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
    def test_python_and_unicode_database_are_pinned(self) -> None:
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "==3.11.*",
            pyproject["project"]["requires-python"],
        )
        self.assertEqual(
            "3.11",
            (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual((3, 11), sys.version_info[:2])
        self.assertEqual("14.0.0", unicodedata.unidata_version)
        self.assertEqual("Cn", unicodedata.category("\U00011f02"))
        self.assertEqual("Cn", unicodedata.category("\U0001ccd6"))
        self.assertTrue(
            lint_skill_pack.copy_equivalent(
                "pre\U00011f02dict",
                "predict",
            )
        )
        self.assertEqual(
            "a",
            lint_skill_pack.nfkc_casefold("\U0001ccd6"),
        )

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

    def test_invalid_trigger_is_excluded_from_prompt_copy_matrix(self) -> None:
        config = libskillpack.load_skill_config()
        source = REPO_ROOT / "content" / "core" / "tables-reporting.yaml"
        entry = deepcopy(libskillpack.read_yaml(source))
        entry["trigger"] = "\ufdfa" * lint_skill_pack.MAX_COPY_TEXT_LENGTH

        with TemporaryDirectory(prefix="content-root-") as temp_root:
            content_root = Path(temp_root)
            for skill in config["skills"].values():
                (content_root / skill["content_dir"]).mkdir()
            with patch.multiple(
                lint_skill_pack,
                CONTENT_ROOT=content_root,
            ), patch.object(
                lint_skill_pack,
                "load_skill_config",
                return_value=config,
            ), patch.object(
                lint_skill_pack,
                "iter_content_entries",
                return_value=[("core", source, entry)],
            ), patch.object(
                lint_skill_pack,
                "lint_route_aliases",
                return_value=[],
            ), patch.object(
                lint_skill_pack,
                "lint_routing_collisions",
                return_value=[],
            ), patch.object(
                lint_skill_pack,
                "lint_prompt_cases",
                return_value=[],
            ) as lint_prompt_cases, patch.object(
                lint_skill_pack,
                "lint_upstream_lock",
                return_value=[],
            ), patch.object(
                lint_skill_pack,
                "lint_stata_help_lock",
                return_value=[],
            ), patch.object(
                lint_skill_pack,
                "lint_distribution_locks",
                return_value=[],
            ), patch.object(
                lint_skill_pack,
                "lint_manifests",
                return_value=[],
            ):
                errors = lint_skill_pack.lint_repo(check_generated=False)

        self.assertTrue(
            any("trigger normalizes beyond" in error for error in errors),
            errors,
        )
        self.assertEqual(
            {},
            lint_prompt_cases.call_args.kwargs["canonical_triggers"],
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

    def test_generic_routing_terms_reject_obfuscated_forms(self) -> None:
        config = libskillpack.load_skill_config()
        source = REPO_ROOT / "content" / "core" / "tables-reporting.yaml"
        base_entry = libskillpack.read_yaml(source)
        markers = (
            ".",
            "/",
            "_",
            "+",
            "#",
            "🔥",
            "\u200b",
            "\u200c",
            "\u200d",
            "\u0338",
        )

        for generic_term in sorted(lint_skill_pack.GENERIC_ROUTING_TERMS):
            variants = {
                generic_term,
                generic_term.upper(),
                fullwidth_ascii(generic_term),
                *(
                    insert_inside_ascii_words(generic_term, marker)
                    for marker in markers
                ),
            }
            for variant in variants:
                with self.subTest(generic_term=generic_term, variant=variant):
                    entry = deepcopy(base_entry)
                    entry["routing_terms"] = [variant]
                    errors = lint_skill_pack.lint_entry(
                        "core",
                        source,
                        entry,
                        config["skills"]["core"],
                    )
                    self.assertTrue(
                        any("too generic" in error for error in errors),
                        errors,
                    )

    def test_generic_content_rejects_every_literal_and_obfuscated_form(self) -> None:
        config = libskillpack.load_skill_config()
        source = REPO_ROOT / "content" / "core" / "tables-reporting.yaml"
        base_entry = libskillpack.read_yaml(source)
        markers = (
            ".",
            "/",
            "_",
            "+",
            "#",
            "🔥",
            "\u200b",
            "\u200c",
            "\u200d",
            "\u0338",
        )

        for generic_text in sorted(lint_skill_pack.GENERIC_TEXT):
            variants = {
                generic_text,
                generic_text.upper(),
                fullwidth_ascii(generic_text),
                *(
                    insert_inside_ascii_words(generic_text, marker)
                    for marker in markers
                ),
                generic_text.replace(" ", "\u200b"),
                generic_text.replace(" ", "\u200d"),
                generic_text.replace(" ", "_"),
            }
            for variant in variants:
                with self.subTest(generic_text=generic_text, variant=variant):
                    entry = deepcopy(base_entry)
                    entry["gotchas"] = [variant]
                    errors = lint_skill_pack.lint_entry(
                        "core",
                        source,
                        entry,
                        config["skills"]["core"],
                    )
                    self.assertTrue(
                        any("contains generic content" in error for error in errors),
                        errors,
                    )

                    entry = deepcopy(base_entry)
                    entry["validation_case"] = variant
                    errors = lint_skill_pack.lint_entry(
                        "core",
                        source,
                        entry,
                        config["skills"]["core"],
                    )
                    self.assertTrue(
                        any("validation_case is generic" in error for error in errors),
                        errors,
                    )

    def test_generic_validation_prefix_rejects_obfuscation(self) -> None:
        config = libskillpack.load_skill_config()
        source = REPO_ROOT / "content" / "core" / "tables-reporting.yaml"
        base_entry = libskillpack.read_yaml(source)
        prefix = "run a small batch mode example that exercises"
        for variant in (
            prefix,
            insert_inside_ascii_words(prefix, "\u200b"),
            insert_inside_ascii_words(prefix, "\u0338"),
            prefix.replace(" ", "\u200d"),
            fullwidth_ascii(prefix),
        ):
            with self.subTest(variant=variant):
                entry = deepcopy(base_entry)
                entry["validation_case"] = f"{variant} the command."
                errors = lint_skill_pack.lint_entry(
                    "core",
                    source,
                    entry,
                    config["skills"]["core"],
                )
                self.assertTrue(
                    any("validation_case is generic" in error for error in errors),
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
            "first Stata session, sysuse auto, inspect a .dta dataset",
            core_skill,
        )
        self.assertIn("Terms that require clarification", core_skill)
        packages_skill = first_snapshot["stata-packages/SKILL.md"].decode("utf-8")
        self.assertIn("Supported aliases and commands", packages_skill)
        self.assertIn("rdsensitivity", packages_skill)
        self.assertIn(
            "never override task intent or the clarification boundaries below",
            " ".join(packages_skill.split()),
        )
        rdrobust = first_snapshot[
            "stata-packages/packages/rdrobust.md"
        ].decode("utf-8")
        self.assertIn(
            "cross-skill: load the `stata-core` skill first",
            rdrobust,
        )
        self.assertNotIn("## Supported aliases and names", rdrobust)
        self.assertIn("- `rdplot`", rdrobust)
        self.assertNotIn("## Smoke test", rdrobust)


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

    def test_cases_are_independent_and_cover_all_actions(self) -> None:
        data = libskillpack.read_yaml(REPO_ROOT / "tests" / "prompts" / "cases.yaml")
        actions = {case["action"] for case in data["cases"]}

        self.assertEqual(2, data["schema_version"])
        self.assertEqual({"route", "clarify", "abstain"}, actions)
        self.assertFalse(
            any(
                "I need task-specific guidance" in case["prompt"]
                for case in data["cases"]
            )
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

    def test_common_unicode_prose_punctuation_is_a_routing_boundary(self) -> None:
        for prompt in (
            "Use—didregress.",
            "Use – didregress.",
            "Use “didregress”.",
            "Use «didregress».",
            "Use … didregress.",
            "Use•didregress.",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(
                    lint_skill_pack.contains_routing_term(
                        prompt,
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

    def test_integrity_normalization_keeps_c_cplusplus_and_csharp_distinct(
        self,
    ) -> None:
        terms = ("native C plugin", "native C++ plugin", "native C# plugin")
        for term in terms:
            self.assertTrue(lint_skill_pack.copy_equivalent(term, term))
            for other_term in terms:
                if other_term != term:
                    self.assertFalse(
                        lint_skill_pack.copy_equivalent(term, other_term)
                    )
        self.assertTrue(
            lint_skill_pack.copy_equivalent(
                "native C+++ plugin",
                "native C++ plugin",
            )
        )
        self.assertTrue(
            lint_skill_pack.copy_equivalent(
                "native C## plugin",
                "native C# plugin",
            )
        )
        self.assertTrue(
            lint_skill_pack.copy_equivalent(
                "native C+ # plugin",
                "native C# plugin",
            )
        )
        for signature in ("native C++ plugin", "native C# plugin"):
            self.assertFalse(
                lint_skill_pack.copy_equivalent(
                    "native C+# + plugin",
                    signature,
                )
            )
            self.assertFalse(
                lint_skill_pack.copy_startswith(
                    "native C+# + plugin with context",
                    signature,
                )
            )
        self.assertFalse(
            lint_skill_pack.copy_equivalent(
                "native C+++ plugin",
                "native C plugin",
            )
        )
        self.assertFalse(
            lint_skill_pack.copy_equivalent(
                "native C+ + plugin",
                "native C plugin",
            )
        )

    def test_cpp_trigger_symbol_padding_cannot_bypass_copy_checks(self) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        for marker in ("+", "#"):
            padded = pad_meaningful_symbol_tokens(
                insert_inside_ascii_words(trigger, marker),
                marker,
            )
            fuzzy = padded.replace(
                insert_inside_ascii_words("STL containers, ", marker),
                "",
            )
            with self.subTest(marker=marker):
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(padded, trigger)
                )
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(
                        f"Please help: {padded} Thanks.",
                        trigger,
                    )
                )
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(fuzzy, trigger)
                )
                self.assertEqual(
                    marker == "+",
                    lint_skill_pack.copy_equivalent(padded, trigger),
                )
                self.assertEqual(
                    marker == "+",
                    lint_skill_pack.copy_startswith(
                        f"{padded} Additional context.",
                        trigger,
                    ),
                )

    def test_cpp_trigger_split_symbol_padding_is_coalesced(self) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        for marker in ("+", "#"):
            split = split_cplusplus_padding(
                insert_inside_ascii_words(trigger, "+"),
                marker,
            )
            fuzzy = split.replace(
                insert_inside_ascii_words("Use when ", "+"),
                "",
                1,
            )
            with self.subTest(marker=marker):
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(split, trigger)
                )
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(
                        f"Please help: {split} Thanks.",
                        trigger,
                    )
                )
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(fuzzy, trigger)
                )
                self.assertEqual(
                    marker == "+",
                    lint_skill_pack.copy_equivalent(split, trigger),
                )
                self.assertEqual(
                    marker == "+",
                    lint_skill_pack.copy_startswith(
                        f"{split} Additional context.",
                        trigger,
                    ),
                )

    def test_cpp_continuation_attached_to_next_word_is_coalesced(self) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        attached = attach_cplusplus_continuation_to_next_word(
            insert_inside_ascii_words(trigger, "+")
        )
        fuzzy = attached.replace(
            insert_inside_ascii_words("Use when ", "+"),
            "",
            1,
        ).replace(
            insert_inside_ascii_words("STL containers, ", "+"),
            "",
            1,
        )

        self.assertEqual(
            "normalized exact",
            lint_skill_pack.trigger_copy_kind(attached, trigger),
        )
        self.assertEqual(
            "embedded normalized",
            lint_skill_pack.trigger_copy_kind(
                f"Please help: {attached} Thanks.",
                trigger,
            ),
        )
        self.assertIsNotNone(
            lint_skill_pack.trigger_copy_kind(fuzzy, trigger)
        )
        self.assertTrue(
            lint_skill_pack.copy_equivalent(attached, trigger)
        )
        self.assertTrue(
            lint_skill_pack.copy_startswith(
                f"{attached} Additional context.",
                trigger,
            )
        )

    def test_cpp_symbol_token_fused_with_following_word_is_split(self) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        fused = fuse_cplusplus_with_following_word(
            insert_inside_ascii_words(trigger, "+")
        )
        fuzzy = fused.replace(
            insert_inside_ascii_words("Use when ", "+"),
            "",
            1,
        )

        self.assertEqual(
            "normalized exact",
            lint_skill_pack.trigger_copy_kind(fused, trigger),
        )
        self.assertEqual(
            "embedded normalized",
            lint_skill_pack.trigger_copy_kind(
                f"Please help: {fused} Thanks.",
                trigger,
            ),
        )
        self.assertIsNotNone(
            lint_skill_pack.trigger_copy_kind(fuzzy, trigger)
        )
        self.assertTrue(lint_skill_pack.copy_equivalent(fused, trigger))
        self.assertTrue(
            lint_skill_pack.copy_startswith(
                f"{fused} Additional context.",
                trigger,
            )
        )

    def test_cpp_symbol_token_fused_with_both_adjacent_words_is_split(
        self,
    ) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        fused = fuse_cplusplus_with_adjacent_words(
            insert_inside_ascii_words(trigger, "+")
        )

        self.assertEqual(
            "normalized exact",
            lint_skill_pack.trigger_copy_kind(fused, trigger),
        )
        self.assertTrue(lint_skill_pack.copy_equivalent(fused, trigger))
        self.assertTrue(
            lint_skill_pack.copy_startswith(
                f"{fused} Additional context.",
                trigger,
            )
        )

    def test_mixed_cpp_padding_is_trigger_relative_but_not_equivalent(
        self,
    ) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        for replacement in ("C++#", "C+# +", "C# + +", "C+#+"):
            mixed = insert_inside_ascii_words(trigger, "+").replace(
                "C++",
                replacement,
            )
            fuzzy = mixed.replace(
                insert_inside_ascii_words("or investigation of ", "+"),
                "",
                1,
            )
            with self.subTest(replacement=replacement):
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(fuzzy, trigger)
                )
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(
                        f"Please help: {fuzzy} Thanks.",
                        trigger,
                    )
                )
                self.assertFalse(
                    lint_skill_pack.copy_equivalent(mixed, trigger)
                )
                self.assertFalse(
                    lint_skill_pack.copy_startswith(
                        f"{mixed} Additional context.",
                        trigger,
                    )
                )

    def test_split_symbolic_tokens_do_not_collapse_to_plain_c(self) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "plugin-sdk-basics.yaml"
        )["trigger"]
        for replacement in (
            "C+ +plugin SDK",
            "C + +plugin SDK",
            "C+ #plugin SDK",
            "C++plugin SDK",
        ):
            prompt = trigger.replace("C plugin SDK", replacement)
            with self.subTest(replacement=replacement):
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(prompt, trigger)
                )
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(
                        f"Please help: {prompt} Thanks.",
                        trigger,
                    )
                )
                self.assertFalse(
                    lint_skill_pack.copy_equivalent(
                        f"native {replacement}",
                        "native C plugin SDK",
                    )
                )
                self.assertFalse(
                    lint_skill_pack.copy_startswith(
                        f"native {replacement} with context",
                        "native C plugin SDK",
                    )
                )

        for prompt in (
            "Please explain how to compile a short C++ plugin for Stata.",
            "Help me diagnose a C# interop wrapper around a Stata plugin.",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(
                    lint_skill_pack.trigger_copy_kind(prompt, trigger)
                )

    def test_all_whitespace_fusion_preserves_symbol_meaning(self) -> None:
        cpp_trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        fused_cpp = insert_inside_ascii_words(
            cpp_trigger,
            "+",
        ).replace(" ", "")

        self.assertEqual(
            "normalized exact",
            lint_skill_pack.trigger_copy_kind(fused_cpp, cpp_trigger),
        )
        self.assertEqual(
            "embedded normalized",
            lint_skill_pack.trigger_copy_kind(
                f"Please{fused_cpp}Thanks",
                cpp_trigger,
            ),
        )
        self.assertTrue(
            lint_skill_pack.copy_equivalent(fused_cpp, cpp_trigger)
        )
        self.assertTrue(
            lint_skill_pack.copy_startswith(
                f"{fused_cpp}Additional",
                cpp_trigger,
            )
        )

        c_trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "plugin-sdk-basics.yaml"
        )["trigger"]
        fused_substitution = c_trigger.replace(
            "C plugin SDK",
            "C++ plugin SDK",
        ).replace(" ", "")
        self.assertIsNotNone(
            lint_skill_pack.trigger_copy_kind(
                fused_substitution,
                c_trigger,
            )
        )
        self.assertFalse(
            lint_skill_pack.copy_equivalent(
                fused_substitution,
                c_trigger,
            )
        )
        self.assertFalse(
            lint_skill_pack.copy_startswith(
                f"{fused_substitution}Additional",
                c_trigger,
            )
        )

    def test_short_multiword_fusions_align_by_membership_not_length(
        self,
    ) -> None:
        cpp_trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        fused_cpp = insert_inside_ascii_words(cpp_trigger, "+").replace(
            "p+l+u+g+i+n n+e+e+d+s C++",
            "p+l+u+g+i+n+n+e+e+d+s+C++",
        ).replace(
            "o+f n+a+m+e-m+a+n+g+l+i+n+g",
            "o+f+n+a+m+e-m+a+n+g+l+i+n+g",
        )
        self.assertEqual(
            "normalized exact",
            lint_skill_pack.trigger_copy_kind(fused_cpp, cpp_trigger),
        )
        self.assertTrue(
            lint_skill_pack.copy_equivalent(fused_cpp, cpp_trigger)
        )

        c_trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "plugin-sdk-basics.yaml"
        )["trigger"]
        fused_substitution = c_trigger.replace(
            "C plugin SDK",
            "C++pluginSDK",
        )
        self.assertIsNotNone(
            lint_skill_pack.trigger_copy_kind(
                fused_substitution,
                c_trigger,
            )
        )
        self.assertFalse(
            lint_skill_pack.copy_equivalent(
                fused_substitution,
                c_trigger,
            )
        )
        self.assertFalse(
            lint_skill_pack.copy_startswith(
                f"{fused_substitution} Additional context.",
                c_trigger,
            )
        )

    def test_raw_fuzzy_detection_survives_incidental_symbol_change(
        self,
    ) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        for replacement in ("C++", "C#", "C+ +", "C+ #", "C++#"):
            prompt = trigger.replace(
                'extern "C"',
                f'extern "{replacement}"',
            )
            with self.subTest(replacement=replacement):
                self.assertEqual(
                    "near-verbatim",
                    lint_skill_pack.trigger_copy_kind(prompt, trigger),
                )
                self.assertEqual(
                    "near-verbatim",
                    lint_skill_pack.trigger_copy_kind(
                        f"Please help: {prompt} Thanks.",
                        trigger,
                    ),
                )

    def test_raw_copy_evidence_survives_primary_symbol_substitution(
        self,
    ) -> None:
        trigger = libskillpack.read_yaml(
            REPO_ROOT / "content" / "plugins" / "cpp_plugins.yaml"
        )["trigger"]
        substitutions = (
            ("C++", "C#"),
            ("C++", "C"),
            ('extern "C"', 'extern "C++"'),
            ('extern "C"', 'extern "C#"'),
        )
        for original, replacement in substitutions:
            substituted = trigger.replace(original, replacement, 1)
            variants = (
                substituted,
                f"Please help: {substituted} Thanks.",
                substituted.replace("STL containers, ", "", 1),
                " ".join(reversed(substituted.split())),
            )
            for variant in variants:
                with self.subTest(
                    original=original,
                    replacement=replacement,
                    variant=variant[:80],
                ):
                    self.assertIsNotNone(
                        lint_skill_pack.trigger_copy_kind(variant, trigger)
                    )

    def test_fully_fused_near_copies_do_not_skip_similarity(self) -> None:
        config = libskillpack.load_skill_config()
        for skill_key, _, entry in libskillpack.iter_content_entries(
            REPO_ROOT / "content",
            config,
        ):
            trigger = entry["trigger"]
            words = trigger.split()
            removed = min(words[1:-1], key=len)
            fused_copy = trigger.replace(" ", "").replace(
                removed,
                "",
                1,
            )
            with self.subTest(skill=skill_key, slug=entry["slug"]):
                self.assertIsNotNone(
                    lint_skill_pack.trigger_copy_kind(
                        fused_copy,
                        trigger,
                    )
                )

    def test_allowed_adversarial_tokens_have_bounded_total_cost(self) -> None:
        prompt = " ".join("c+" * 48 for _ in range(42))
        self.assertLessEqual(
            len(prompt),
            lint_skill_pack.MAX_COPY_TEXT_LENGTH,
        )
        config = libskillpack.load_skill_config()
        triggers = [
            entry["trigger"]
            for _, _, entry in libskillpack.iter_content_entries(
                REPO_ROOT / "content",
                config,
            )
        ]

        started = monotonic()
        for trigger in triggers:
            lint_skill_pack.trigger_copy_kind(prompt, trigger)
        self.assertLess(monotonic() - started, 2.0)

    def test_worst_case_allowed_pair_has_bounded_similarity_cost(self) -> None:
        trigger = " ".join("a" for _ in range(2048))
        prompt = f"{trigger[:-1]}b"
        self.assertEqual(
            lint_skill_pack.MAX_COPY_TEXT_LENGTH - 1,
            len(trigger),
        )

        started = monotonic()
        self.assertEqual(
            "near-verbatim",
            lint_skill_pack.trigger_copy_kind(prompt, trigger),
        )
        self.assertLess(monotonic() - started, 1.0)

    def test_distributed_edits_remain_near_verbatim(self) -> None:
        config = libskillpack.load_skill_config()
        for skill_key, _, entry in libskillpack.iter_content_entries(
            REPO_ROOT / "content",
            config,
        ):
            trigger = entry["trigger"]
            compact = lint_skill_pack.copy_forms(trigger).compact
            substituted: list[str] = []
            inserted: list[str] = []
            deleted: list[str] = []
            changed = 0
            for index, character in enumerate(compact, start=1):
                replacement = "q" if character == "x" else "x"
                if index % 12 == 0 and character.isalnum():
                    substituted.append(replacement)
                    inserted.extend((character, replacement))
                    changed += 1
                else:
                    substituted.append(character)
                    inserted.append(character)
                    deleted.append(character)
            variants = {
                "substituted": "".join(substituted),
                "inserted": "".join(inserted),
                "deleted": "".join(deleted),
            }

            self.assertGreater(changed, 0)
            for mutation, prompt in variants.items():
                with self.subTest(
                    skill=skill_key,
                    slug=entry["slug"],
                    mutation=mutation,
                ):
                    self.assertGreaterEqual(
                        min(len(prompt), len(compact))
                        / max(len(prompt), len(compact)),
                        lint_skill_pack.COPY_SIMILARITY_THRESHOLD,
                    )
                    self.assertEqual(
                        "near-verbatim",
                        lint_skill_pack.trigger_copy_kind(prompt, trigger),
                    )

    def test_bit_parallel_edit_threshold_matches_reference_distance(
        self,
    ) -> None:
        def reference_distance(left: str, right: str) -> int:
            previous = list(range(len(right) + 1))
            for row, left_character in enumerate(left, start=1):
                current = [row]
                for column, right_character in enumerate(right, start=1):
                    current.append(
                        min(
                            previous[column] + 1,
                            current[column - 1] + 1,
                            previous[column - 1]
                            + (left_character != right_character),
                        )
                    )
                previous = current
            return previous[-1]

        alphabet = "abcde"
        for left_length in range(25):
            left = "".join(
                alphabet[
                    (index * index + index + left_length) % len(alphabet)
                ]
                for index in range(left_length)
            )
            for right_length in range(25):
                right = "".join(
                    alphabet[
                        (
                            index * index
                            + (3 * index)
                            + right_length
                        )
                        % len(alphabet)
                    ]
                    for index in range(right_length)
                )
                maximum_length = max(left_length, right_length)
                expected = reference_distance(left, right) <= (
                    maximum_length
                    * lint_skill_pack.COPY_MAX_EDIT_PERCENT
                    // 100
                )
                with self.subTest(
                    left_length=left_length,
                    right_length=right_length,
                ):
                    self.assertEqual(
                        expected,
                        lint_skill_pack.bounded_edit_similar(left, right),
                    )
                    self.assertEqual(
                        expected,
                        lint_skill_pack.bounded_edit_similar(right, left),
                    )

    def test_maximum_length_edit_fallback_has_bounded_cost(self) -> None:
        left = "a" * (lint_skill_pack.MAX_COPY_TEXT_LENGTH - 1)
        changed = (
            len(left) * lint_skill_pack.COPY_MAX_EDIT_PERCENT // 100
        ) + 1
        right = ("a" * (len(left) - changed)) + ("b" * changed)

        started = monotonic()
        self.assertFalse(
            lint_skill_pack.bounded_copy_near_verbatim(left, right)
        )
        self.assertLess(monotonic() - started, 1.0)

    def test_oversized_copy_token_has_bounded_cost(self) -> None:
        oversized = "a+" * 1600
        started = monotonic()
        self.assertFalse(
            lint_skill_pack.copy_equivalent(
                oversized,
                "native C++ plugin",
            )
        )
        self.assertLess(monotonic() - started, 2.0)

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

    def test_nfkc_casefold_is_idempotent_for_routing_edge_cases(self) -> None:
        for value in (
            "𝐃𝐈𝐃𝐑𝐄𝐆𝐑𝐄𝐒𝐒",
            "ｄｉｄｒｅｇｒｅｓｓ",
            "İdidregress",
            "didregress\u0338",
            "did\u200bregress",
            "did\u200cregress",
            "did\u200dregress",
            "C++",
            "C#",
        ):
            with self.subTest(value=value):
                once = lint_skill_pack.nfkc_casefold(value)
                self.assertEqual(
                    once,
                    lint_skill_pack.nfkc_casefold(once),
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

    def test_unicode_16_outlined_trigger_copy_is_detected_on_pinned_unicode_14(
        self,
    ) -> None:
        trigger = (
            "Use when the user asks for ordinary least squares, robust standard "
            "errors, factor-variable interactions, adjusted predictions, and "
            "fitted values."
        )
        outlined = "".join(
            chr(0x1CCD6 + ord(character.casefold()) - ord("a"))
            if character.isascii() and character.isalpha()
            else character
            for character in trigger
        )
        self.assertIsNotNone(
            lint_skill_pack.trigger_copy_kind(outlined, trigger)
        )

    def test_all_canonical_trigger_obfuscations_are_detected_without_fixture_false_positives(
        self,
    ) -> None:
        config = libskillpack.load_skill_config()
        entries = libskillpack.iter_content_entries(
            REPO_ROOT / "content",
            config,
        )
        triggers = {
            (
                f"{config['skills'][skill_key]['name']}/"
                f"{config['skills'][skill_key]['route_dir']}/"
                f"{entry['slug']}.md"
            ): entry["trigger"]
            for skill_key, _, entry in entries
        }
        markers = (
            ".",
            "-",
            "/",
            "_",
            "🔥",
            "\u200b",
            "\u200c",
            "\u200d",
            "\u0338",
            "\ufe0f",
        )
        integrity_symbols = ("+", "#")
        space_replacements = (
            "\t",
            "\n",
            "\u00a0",
            "\u200b",
            "\u200d",
            "_",
        )
        for route, trigger in triggers.items():
            variants = {
                trigger,
                trigger.upper(),
                fullwidth_ascii(trigger),
                *(
                    insert_inside_ascii_words(trigger, marker)
                    for marker in markers
                ),
                *(
                    trigger.replace(" ", replacement)
                    for replacement in space_replacements
                ),
            }
            variants.update(
                insert_inside_ascii_words(trigger, symbol)
                for symbol in integrity_symbols
            )
            variants.update(
                pad_meaningful_symbol_tokens(
                    insert_inside_ascii_words(trigger, symbol),
                    symbol,
                )
                for symbol in integrity_symbols
            )
            if "C++" in trigger:
                variants.update(
                    split_cplusplus_padding(
                        insert_inside_ascii_words(trigger, "+"),
                        symbol,
                    )
                    for symbol in integrity_symbols
                )
            for variant in variants:
                with self.subTest(route=route, variant=variant[:80]):
                    self.assertIsNotNone(
                        lint_skill_pack.trigger_copy_kind(variant, trigger)
                    )

        prompt_data = libskillpack.read_yaml(
            REPO_ROOT / "tests" / "prompts" / "cases.yaml"
        )
        for case in prompt_data["cases"]:
            for route, trigger in triggers.items():
                with self.subTest(case=case["id"], route=route):
                    self.assertIsNone(
                        lint_skill_pack.trigger_copy_kind(
                            case["prompt"],
                            trigger,
                        )
                    )

    def test_required_route_boundaries_and_alias_fixture_are_present(self) -> None:
        data = libskillpack.read_yaml(REPO_ROOT / "tests" / "prompts" / "cases.yaml")
        route_boundaries = {
            case["id"]: case
            for case in data["cases"]
            if case["action"] == "route" and case["boundary"] is True
        }
        required_boundaries = {
            "boundary-built-in-regression-diagnostics": (
                "stata-core",
                ("stata-core/references/regression-diagnostics.md",),
                ("stata-packages/packages/diagnostics.md",),
                ("vif", "heteroskedasticity", "built-in"),
            ),
            "boundary-reghdfe-package": (
                "stata-packages",
                ("stata-packages/packages/reghdfe.md",),
                ("stata-core/references/linear-regression.md",),
                ("reghdfe", "absorbed fixed effects", "clustered"),
            ),
            "boundary-built-in-didregress-versus-csdid": (
                "stata-core",
                ("stata-core/references/difference-in-differences.md",),
                ("stata-packages/packages/did.md",),
                ("didregress", "do not use csdid"),
            ),
            "boundary-csdid-versus-built-in-didregress": (
                "stata-packages",
                ("stata-packages/packages/did.md",),
                ("stata-core/references/difference-in-differences.md",),
                ("csdid", "staggered treatment", "do not substitute"),
            ),
            "boundary-manual-rd-versus-rdrobust": (
                "stata-core",
                ("stata-core/references/regression-discontinuity.md",),
                ("stata-packages/packages/rdrobust.md",),
                ("manually", "built-in regress", "do not use rdrobust"),
            ),
            "boundary-rdrobust-versus-manual-rd": (
                "stata-packages",
                ("stata-packages/packages/rdrobust.md",),
                ("stata-core/references/regression-discontinuity.md",),
                ("rdrobust", "rdbwselect", "do not replace"),
            ),
            "boundary-built-in-table-versus-estout": (
                "stata-core",
                ("stata-core/references/tables-reporting.md",),
                ("stata-packages/packages/estout.md",),
                ("built-in collect", "without estout"),
            ),
            "boundary-esttab-versus-built-in-table": (
                "stata-packages",
                ("stata-packages/packages/estout.md",),
                ("stata-core/references/tables-reporting.md",),
                ("esttab", "do not rewrite", "built-in"),
            ),
            "boundary-ivreghdfe-versus-ivregress-or-reghdfe": (
                "stata-packages",
                ("stata-packages/packages/ivreg2.md",),
                (
                    "stata-core/references/linear-regression.md",
                    "stata-packages/packages/reghdfe.md",
                ),
                ("ivreghdfe", "ivregress", "plain reghdfe"),
            ),
            "boundary-ado-programming-versus-native-plugin": (
                "stata-core",
                ("stata-core/references/advanced-programming.md",),
                ("stata-c-plugins/references/plugin-sdk-basics.md",),
                ("rclass", "program define", "no native c plugin"),
            ),
            "boundary-native-plugin-sdk": (
                "stata-c-plugins",
                ("stata-c-plugins/references/plugin-sdk-basics.md",),
                (
                    "stata-core/references/advanced-programming.md",
                    "stata-core/references/external-tools-integration.md",
                ),
                ("native c plugin", "stplugin.h", "stata_call"),
            ),
            "boundary-community-package-lifecycle": (
                "stata-packages",
                ("stata-packages/packages/package-management.md",),
                ("stata-c-plugins/references/packaging_and_help.md",),
                ("community ado package", "ado describe", "not native plugin"),
            ),
            "boundary-native-plugin-package-manifest": (
                "stata-c-plugins",
                ("stata-c-plugins/references/packaging_and_help.md",),
                ("stata-packages/packages/package-management.md",),
                ("compiled stata plugin", "stata.toc", "platform-specific binaries"),
            ),
        }

        self.assertEqual(set(required_boundaries), set(route_boundaries))
        for case_id, (
            skill,
            expected_refs,
            forbidden_routes,
            intent_terms,
        ) in required_boundaries.items():
            with self.subTest(case_id=case_id):
                case = route_boundaries[case_id]
                self.assertEqual(skill, case["expected_skill"])
                self.assertEqual(list(expected_refs), case["expected_refs"])
                self.assertEqual(list(forbidden_routes), case["forbidden_routes"])
                prompt = lint_skill_pack.normalized_routing_term(case["prompt"])
                prompt_tokens = set(prompt.split())
                for term in intent_terms:
                    normalized_term = lint_skill_pack.normalized_routing_term(term)
                    if " " in normalized_term:
                        self.assertIn(
                            f" {normalized_term} ",
                            f" {prompt} ",
                        )
                    else:
                        self.assertIn(normalized_term, prompt_tokens)
        alias_case = next(
            case
            for case in data["cases"]
            if case["id"] == "packages-rdrobust-alias-only"
        )
        self.assertEqual("route", alias_case["action"])
        self.assertFalse(alias_case["boundary"])
        self.assertEqual("stata-packages", alias_case["expected_skill"])
        self.assertEqual(
            ["stata-packages/packages/rdrobust.md"],
            alias_case["expected_refs"],
        )
        self.assertEqual([], alias_case["forbidden_routes"])
        normalized_prompt = lint_skill_pack.normalized_routing_term(
            alias_case["prompt"]
        )
        prompt_names = set(
            normalized_prompt.split()
        )
        required_alias_names = {"rdsensitivity", "rdrbounds", "rdmc", "rdms"}
        self.assertTrue(required_alias_names.issubset(prompt_names))
        for name in required_alias_names:
            for mutated_name in (
                f"α{name}",
                f"{name}β",
                f"α{name}β",
            ):
                with self.subTest(mutated_name=mutated_name):
                    mutated_names = set(
                        lint_skill_pack.normalized_routing_term(
                            mutated_name
                        ).split()
                    )
                    self.assertNotIn(name, mutated_names)
        primary_suite_names = {
            "rdrobust",
            "rdbwselect",
            "rdplot",
            "rddensity",
            "rdlocrand",
            "rdrandinf",
            "rdwinselect",
            "rdmulti",
        }
        normalized_forbidden_prompt = lint_skill_pack.normalized_copy_text(
            alias_case["prompt"]
        )
        for name in primary_suite_names:
            self.assertNotIn(name, normalized_forbidden_prompt)
        for mutated_name in (
            "ｒｄｒｏｂｕｓｔ",
            "rｄrobust",
            "𝐫𝐝𝐫𝐨𝐛𝐮𝐬𝐭",
            "rd\u200brobust",
            "rd\u200drobust",
            "rd.robust",
            "rd-robust",
            "rd/robust",
            "rd_robust",
            "rd+robust",
            "rd#robust",
            "rd🔥robust",
            "rd\u0338robust",
        ):
            with self.subTest(mutated_name=mutated_name):
                self.assertIn(
                    "rdrobust",
                    lint_skill_pack.normalized_copy_text(mutated_name),
                )
        self.assertNotIn(
            "rdrobust",
            lint_skill_pack.normalized_copy_text("rd robust"),
        )


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
