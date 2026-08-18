from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lint_skill_pack  # noqa: E402
import libskillpack  # noqa: E402


class DocumentationStyleProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = lint_skill_pack.load_documentation_style_profile()

    def test_reviewed_profile_is_complete_and_project_first(self) -> None:
        self.assertEqual(
            [],
            lint_skill_pack.lint_documentation_style_profile(self.profile),
        )
        self.assertEqual(
            "https://developers.google.com/style",
            self.profile["authority"]["url"],
        )
        self.assertTrue(self.profile["precedence"]["project_first"])

    def test_profile_requires_dated_authority_and_protected_contexts(self) -> None:
        profile = deepcopy(self.profile)
        profile["authority"]["reviewed_on"] = "August 18"
        profile["protected_contexts"].remove("inline-code")

        errors = lint_skill_pack.lint_documentation_style_profile(profile)

        self.assertTrue(any("reviewed_on" in error for error in errors))
        self.assertTrue(any("inline-code" in error for error in errors))

    def test_sentence_case_checks_initial_word_with_technical_exception(self) -> None:
        proper_names = lint_skill_pack.style_profile_names(self.profile)
        prefixes = lint_skill_pack.style_profile_lowercase_prefixes(self.profile)

        self.assertEqual(
            "stata",
            lint_skill_pack.sentence_case_error(
                "stata core skill",
                proper_names=proper_names,
                lowercase_initials=prefixes,
            ),
        )
        self.assertIsNone(
            lint_skill_pack.sentence_case_error(
                "reghdfe: High-dimensional fixed effects",
                proper_names=proper_names,
                lowercase_initials=prefixes,
            )
        )
        self.assertEqual(
            "Workflow",
            lint_skill_pack.sentence_case_error(
                "Stata core Workflow",
                proper_names=proper_names,
                lowercase_initials=prefixes,
            ),
        )
        self.assertEqual(
            "UPPERCASE",
            lint_skill_pack.sentence_case_error(
                "ALL UPPERCASE",
                proper_names=proper_names,
                lowercase_initials=prefixes,
            ),
        )
        self.assertIsNone(
            lint_skill_pack.sentence_case_error(
                "Stata GMM workflow",
                proper_names=proper_names,
                lowercase_initials=prefixes,
            )
        )

    def test_config_hard_fails_a_lowercase_initial_heading(self) -> None:
        config = deepcopy(libskillpack.load_skill_config())
        config["skills"]["core"]["heading"] = "stata core skill"

        errors = lint_skill_pack.lint_config(config, self.profile)

        self.assertTrue(any("unexpected 'stata'" in error for error in errors))


class MarkdownHardLintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = lint_skill_pack.load_documentation_style_profile()
        cls.path = REPO_ROOT / "sample.md"

    def lint(self, text: str) -> list[str]:
        return lint_skill_pack.lint_markdown_document(
            self.path,
            text,
            self.profile,
        )

    def test_requires_one_h1_without_heading_level_skips(self) -> None:
        errors = self.lint("## Start\n\n#### Details\n")

        self.assertTrue(any("exactly one level-1" in error for error in errors))
        self.assertTrue(any("level jumps" in error for error in errors))

    def test_rejects_empty_and_non_sentence_case_headings(self) -> None:
        errors = self.lint("# \n\n## Workflow Details\n")

        self.assertTrue(any("empty heading" in error for error in errors))
        self.assertTrue(any("unexpected 'Details'" in error for error in errors))

    def test_protects_frontmatter_fences_indented_inline_and_raw_code(self) -> None:
        text = """---
title: "# Hidden & see below"
---
# Stata guide

````stata
# Fake Heading
Use [click here](https://example.test/?a=1&b=2).
```
````

~~~text
## Another Fake Heading
~~~

    # Indented Fake Heading & see above

Use `A & B`, ``see below``, and `[click here](destination)` literally.
<code># Raw Heading & see below</code>
<pre>
## Raw block Heading & click here
</pre>

## Valid details
"""

        self.assertEqual([], self.lint(text))

    def test_protects_balanced_link_destinations_and_html_attributes(self) -> None:
        text = """# Stata guide

See the [Google style guide](https://example.test/a_(b)?x=1&y=2).
<a href="https://example.test/?x=1&y=2">Descriptive guide</a>
![Routing diagram](https://example.test/image?a=1&b=2)
<img src="https://example.test/image?a=1&b=2" alt="Routing diagram">
"""

        self.assertEqual([], self.lint(text))

    def test_rejects_vague_links_missing_alt_attribute_and_ampersands(self) -> None:
        text = """# Stata guide

Open [click here](https://example.test).
![](diagram.svg)
[here][guide]
![][diagram]
[![ ](nested.svg)](https://example.test)
<img src="diagram.svg">
Use A & B and see below.

[guide]: https://example.test/?a=1&b=2
[diagram]: diagram.svg
"""

        errors = self.lint(text)

        self.assertTrue(any("descriptive link text" in error for error in errors))
        self.assertEqual(1, sum("alt attribute" in error for error in errors))
        self.assertEqual(2, sum("descriptive link text" in error for error in errors))
        self.assertTrue(any("ampersand in prose" in error for error in errors))
        self.assertTrue(any("'see below'" in error for error in errors))

    def test_allows_decorative_empty_alt_slots_and_attributes(self) -> None:
        text = """# Stata guide

![](spacer.svg)
![][spacer]
<img src="spacer.svg" alt="">

[spacer]: spacer.svg
"""

        self.assertEqual([], self.lint(text))

    def test_template_heading_placeholders_are_structural_not_prose(self) -> None:
        text = """# {{ entry.title }}

## When to use

{{ entry.trigger }}
"""

        self.assertEqual([], self.lint(text))

    def test_protected_technical_token_can_lead_a_heading(self) -> None:
        self.assertEqual(
            [],
            self.lint("# `custom_command` workflow\n"),
        )

    def test_commonmark_and_google_failures_have_distinct_labels(self) -> None:
        errors = self.lint("## Title Case\n\nA & B.\n")

        self.assertTrue(any(error.startswith("[CommonMark/project]") for error in errors))
        self.assertTrue(any(error.startswith("[Google style]") for error in errors))

    def test_all_configured_source_documents_pass_hard_lint(self) -> None:
        paths, path_errors = lint_skill_pack.documentation_style_paths(
            self.profile,
            check_generated=False,
        )
        errors = list(path_errors)
        for path in paths:
            errors.extend(
                lint_skill_pack.lint_markdown_document(
                    path,
                    path.read_text(encoding="utf-8"),
                    self.profile,
                )
            )

        self.assertEqual([], errors)


class MarkdownAdvisoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = lint_skill_pack.load_documentation_style_profile()
        cls.path = REPO_ROOT / "sample.md"

    def test_advisories_report_but_do_not_fail_hard_lint(self) -> None:
        text = """# Stata guide

## Workflow

- The regression model was estimated with many inputs, several transformations, multiple interactions, clustered errors, extensive diagnostics, and additional checks that make this deliberately long sentence difficult to scan.
- Run reghdfe after review.
- Run `reghdfe` after review.
"""

        self.assertEqual(
            [],
            lint_skill_pack.lint_markdown_document(
                self.path,
                text,
                self.profile,
            ),
        )
        advisories = lint_skill_pack.markdown_style_advisories(
            self.path,
            text,
            self.profile,
            commands=("reghdfe",),
        )

        self.assertTrue(any("numbered list" in item for item in advisories))
        self.assertTrue(any("sentence has" in item for item in advisories))
        self.assertTrue(any("passive voice" in item for item in advisories))
        self.assertEqual(
            1,
            sum("code font" in item for item in advisories),
        )


if __name__ == "__main__":
    unittest.main()
