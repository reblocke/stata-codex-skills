from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock


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
            "ALL",
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
        for invalid in ("OVERVIEW", "Title: SUBTITLE", "Title: subtitle"):
            with self.subTest(invalid=invalid):
                self.assertIsNotNone(
                    lint_skill_pack.sentence_case_error(
                        invalid,
                        proper_names=proper_names,
                        lowercase_initials=prefixes,
                    )
                )
        for valid in (
            "Survival with Kaplan–Meier estimates",
            "Use GitHub Actions",
            "macOS setup",
        ):
            with self.subTest(valid=valid):
                self.assertIsNone(
                    lint_skill_pack.sentence_case_error(
                        valid,
                        proper_names=proper_names,
                        lowercase_initials=prefixes,
                    )
                )

        self.assertIsNone(
            lint_skill_pack.sentence_case_error(
                " ".join(["Stata"] * 4000),
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

    def test_image_alt_text_participates_in_heading_sentence_case(self) -> None:
        for heading in (
            "# ![Workflow Details](image.svg)\n",
            '# <img src="image.svg" alt="Workflow Details">\n',
        ):
            with self.subTest(heading=heading):
                errors = self.lint(heading)
                self.assertTrue(
                    any("unexpected 'Details'" in error for error in errors)
                )
        for dynamic_heading in (
            "# ![{{ title }}](image.svg)\n",
            '# <img src="image.svg" alt="{{ title }}">\n',
        ):
            with self.subTest(dynamic_heading=dynamic_heading):
                self.assertEqual([], self.lint(dynamic_heading))

    def test_image_alt_uses_commonmark_accessibility_semantics(self) -> None:
        links = """# Stata guide

[![`here`](image.svg)](guide.md)
[![<em>here</em>](image.svg)](guide.md)
[![&ZeroWidthSpace;here](image.svg)](guide.md)
[![<span></span>](image.svg)](guide.md)
"""
        errors = self.lint(links)
        self.assertEqual(4, sum("descriptive link" in error for error in errors))

        for heading in (
            "# ![&ZeroWidthSpace;](image.svg)\n",
            "# ![` `](image.svg)\n",
            "# ![<span></span>](image.svg)\n",
        ):
            with self.subTest(heading=heading):
                errors = self.lint(heading)
                self.assertTrue(any("empty heading" in error for error in errors))
                self.assertFalse(any("unexpected 'span'" in error for error in errors))

    def test_image_alt_text_participates_in_prose_checks(self) -> None:
        text = """# Stata guide

![A & B](image.svg)
![Please see below](image.svg)
<img src="image.svg" alt="A & B">
<img src="image.svg" alt="Please see below">
"""

        errors = self.lint(text)

        self.assertEqual(2, sum("ampersand in prose" in error for error in errors))
        self.assertEqual(2, sum("'see below'" in error for error in errors))

        multiline = """# Stata guide

![First line
A & B and please see below](image.svg)
"""
        errors = self.lint(multiline)
        self.assertTrue(
            any(
                "sample.md:4:" in error and "ampersand in prose" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:4:" in error and "'see below'" in error
                for error in errors
            )
        )

        for hardbreak in ("  \n", "\\\n"):
            with self.subTest(hardbreak=hardbreak):
                text = (
                    "# Stata guide\n\n"
                    f"![First{hardbreak}A & B and please see below](image.svg)\n"
                )
                errors = self.lint(text)
                self.assertTrue(
                    any(
                        "sample.md:4:" in error
                        and "ampersand in prose" in error
                        for error in errors
                    )
                )
                self.assertTrue(
                    any(
                        "sample.md:4:" in error and "'see below'" in error
                        for error in errors
                    )
                )

        nested = """# Stata guide

![![First
A & B](inner.svg)](outer.svg)
"""
        self.assertTrue(
            any(
                "sample.md:4:" in error and "ampersand in prose" in error
                for error in self.lint(nested)
            )
        )

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
See the [Google style guide](https://example.test/?x=1&amp;y=2).
<a href="https://example.test/?x=1&y=2">Descriptive guide</a>
![Routing diagram](https://example.test/image?a=1&b=2)
<img src="https://example.test/image?a=1&b=2" alt="Routing diagram">
"""

        self.assertEqual([], self.lint(text))

    def test_entity_masking_preserves_link_parsing_without_raw_ampersands(self) -> None:
        text = """# Stata guide

Open [here](https://example.test/?x=1&amp;y=2).
Use A &amp; B in prose.
"""

        errors = self.lint(text)

        self.assertEqual(1, sum("descriptive link text" in error for error in errors))
        self.assertFalse(any("ampersand in prose" in error for error in errors))

    def test_entity_masking_does_not_create_markdown_blocks(self) -> None:
        for entity in ("&lt;", "&#60;"):
            with self.subTest(entity=entity):
                errors = self.lint(f"# Stata guide\n\n{entity}) A &amp; B\n")
                self.assertFalse(
                    any("ampersand in prose" in error for error in errors)
                )

    def test_invalid_or_fragmented_entities_do_not_hide_raw_ampersands(self) -> None:
        invalid = (
            "A &bogus; B.",
            "A &notit; B.",
            "A &<span>amp;</span> B.",
            "A &<!-- split -->amp; B.",
        )
        for prose in invalid:
            with self.subTest(prose=prose):
                errors = self.lint(f"# Stata guide\n\n{prose}\n")
                self.assertTrue(any("ampersand in prose" in error for error in errors))

        for html in (
            "<div>A &notit; B.</div>",
            '<img src="image.svg" alt="A &notit; B">',
            "<div>A &#12345678; B.</div>",
        ):
            with self.subTest(html=html):
                errors = self.lint(f"# Stata guide\n\n{html}\n")
                self.assertTrue(any("ampersand in prose" in error for error in errors))

        self.assertFalse(
            any(
                "ampersand in prose" in error
                for error in self.lint("# Stata guide\n\nA &not; B.\n")
            )
        )

    def test_entity_encoded_visible_text_remains_semantic(self) -> None:
        text = """# Stata guide

Open [h&#101;re](guide.md).
<a href="guide.md">h&#101;re</a>

## Workflow &#68;etails
"""

        errors = self.lint(text)

        self.assertEqual(2, sum("descriptive link text" in error for error in errors))
        self.assertTrue(any("unexpected 'Details'" in error for error in errors))
        self.assertFalse(any("ampersand in prose" in error for error in errors))

    def test_vague_links_cannot_hide_behind_format_controls_or_punctuation(self) -> None:
        labels = (
            "h&ZeroWidthSpace;ere",
            "here.",
            "click here!",
            "(here)",
            "“here”",
            "read more…",
            "🔗 here",
            "➡ here",
            "here™",
        )
        text = "# Stata guide\n\n" + "\n".join(
            f"[{label}](guide.md)" for label in labels
        )

        errors = self.lint(text)

        self.assertEqual(
            len(labels),
            sum("descriptive link text" in error for error in errors),
        )

    def test_entity_decoded_newlines_stay_on_the_source_line(self) -> None:
        text = """# Stata guide

A &NewLine; see below.
<a href="guide.md">&NewLine;here</a>
"""

        errors = self.lint(text)

        self.assertTrue(
            any(
                "sample.md:3:" in error and "see below" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:4:" in error and "descriptive link" in error
                for error in errors
            )
        )

    def test_combining_marks_do_not_make_empty_labels_visible(self) -> None:
        errors = self.lint("# &#x301;\n\n[&#xfe0f;](guide.md)\n")

        self.assertTrue(any("empty heading" in error for error in errors))
        self.assertTrue(any("descriptive link" in error for error in errors))

    def test_inline_code_cannot_open_a_raw_html_protection_region(self) -> None:
        text = """# Stata guide

Use `<script>` literally.

## Title Case

Use A & B and see below.
"""

        errors = self.lint(text)

        self.assertTrue(any("unexpected 'Case'" in error for error in errors))
        self.assertTrue(any("ampersand in prose" in error for error in errors))
        self.assertTrue(any("'see below'" in error for error in errors))

    def test_multiline_prose_diagnostics_use_the_source_line(self) -> None:
        text = """# Stata guide

First sentence.
Use A & B.
Please see below.
"""

        errors = self.lint(text)

        self.assertTrue(
            any(
                "sample.md:4:" in error and "ampersand" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:5:" in error and "see below" in error
                for error in errors
            )
        )

    def test_multiline_link_and_image_diagnostics_use_the_source_line(self) -> None:
        text = """# Stata guide

First sentence.
Open [here](guide.md).
<img src="diagram.svg">
"""

        errors = self.lint(text)

        self.assertTrue(
            any(
                "sample.md:4:" in error and "descriptive link" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:5:" in error and "alt attribute" in error
                for error in errors
            )
        )

        multiline_label = "# Stata guide\n\nOpen [\nhere](guide.md).\n"
        self.assertTrue(
            any(
                "sample.md:4:" in error and "descriptive link" in error
                for error in self.lint(multiline_label)
            )
        )

    def test_line_mapping_accounts_for_multiline_code_and_html_links(self) -> None:
        markdown = """# Stata guide

Use `literal
code` exactly.
Open [here](guide.md).
<img src="diagram.svg">
"""
        errors = self.lint(markdown)
        self.assertTrue(
            any(
                "sample.md:5:" in error and "descriptive link" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:6:" in error and "alt attribute" in error
                for error in errors
            )
        )

        html = """# Stata guide

<a href="guide.md">
here
</a>
"""
        errors = self.lint(html)
        self.assertTrue(
            any(
                "sample.md:4:" in error and "descriptive link" in error
                for error in errors
            )
        )

    def test_line_mapping_handles_code_closers_after_backslashes(self) -> None:
        text = r"""# Stata guide

Use `a
\` then.
Open [here](guide.md).
"""

        errors = self.lint(text)

        self.assertTrue(
            any(
                "sample.md:5:" in error and "descriptive link" in error
                for error in errors
            )
        )

    def test_prose_line_mapping_skips_multiline_protected_contexts(self) -> None:
        code = """# Stata guide

Use `literal
code` exactly.
Use A & B.
Please see below.
"""
        errors = self.lint(code)
        self.assertTrue(
            any(
                "sample.md:5:" in error and "ampersand" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:6:" in error and "see below" in error
                for error in errors
            )
        )

        html = """# Stata guide

<span
 class="note">Use A & B and see below.</span>
"""
        errors = self.lint(html)
        self.assertTrue(
            any(
                "sample.md:4:" in error and "ampersand" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "sample.md:4:" in error and "see below" in error
                for error in errors
            )
        )

    def test_source_mapping_skips_hidden_link_syntax_and_html_backticks(self) -> None:
        link_title = """# Stata guide

Open [Google guide](guide.md
  "Long title").
Open [here](guide.md).
"""
        errors = self.lint(link_title)
        self.assertTrue(
            any(
                "sample.md:5:" in error and "descriptive link" in error
                for error in errors
            )
        )

        image_title = """# Stata guide

![Diagram](image.svg
  "Long title")
Open [here](guide.md).
"""
        errors = self.lint(image_title)
        self.assertTrue(
            any(
                "sample.md:5:" in error and "descriptive link" in error
                for error in errors
            )
        )

        link_backtick = """# Stata guide

[Google guide](<guide`x>) use `literal
code` exactly.
Open [here](guide.md).
"""
        errors = self.lint(link_backtick)
        self.assertTrue(
            any(
                "sample.md:5:" in error and "descriptive link" in error
                for error in errors
            )
        )

        html_attribute = """# Stata guide

<span title="`">x</span> use `literal
code` exactly.
Open [here](guide.md).
"""
        errors = self.lint(html_attribute)
        self.assertTrue(
            any(
                "sample.md:5:" in error and "descriptive link" in error
                for error in errors
            )
        )

    def test_jinja_controls_do_not_cross_protected_markdown_contexts(self) -> None:
        self.assertEqual([], self.lint("# Stata guide\n\n{# see below #}\n"))
        errors = self.lint('# [<img src=x alt="{# comment #}">](guide.md)\n')
        self.assertTrue(any("empty heading" in error for error in errors))
        self.assertTrue(any("descriptive link" in error for error in errors))

        for protected in (
            "{#\n# Hidden heading\n[here](guide.md)\n#}\n\n# Stata guide\n",
            '# Stata guide\n\n{% set x = "[here](guide.md)" %}\n',
            "# Stata guide\n\n{# hidden\nPlease see below & later\n#}\n",
        ):
            with self.subTest(protected=protected):
                self.assertEqual([], self.lint(protected))

        trailing = (
            "# Stata guide\n\n"
            '{% set x = "<script>" %} Please see below.\n'
        )
        self.assertTrue(
            any("'see below'" in error for error in self.lint(trailing))
        )

        for literal in (
            "# Stata guide\n\n\\{# Please see below #}\n",
            "# Stata guide\n\n&#123;# Please see below #}\n",
            "# Stata guide\n\n<span>&lbrace;# Please see below #}</span>\n",
            '# Stata guide\n\n<img alt="&#123;# Please see below #}" src=x>\n',
        ):
            with self.subTest(literal=literal):
                self.assertTrue(
                    any("'see below'" in error for error in self.lint(literal))
                )

        html_block = (
            "# Stata guide\n\n"
            "<div>\n{# Please see below & later #}\n</div>\n"
        )
        self.assertEqual([], self.lint(html_block))

        midline = (
            "# Stata guide\n\n"
            "Start {#\n\nPlease see below & later\n#} end.\n"
        )
        self.assertEqual([], self.lint(midline))

        protected_closer = (
            "# Stata guide\n\n"
            "{# Please see below [label](url#}) trailing.\n"
        )
        self.assertTrue(
            any("'see below'" in error for error in self.lint(protected_closer))
        )

        quoted_closer = (
            "# Stata guide\n\n"
            '{% set x = "%}<code>" %} Please see below & later.\n'
        )
        quoted_errors = self.lint(quoted_closer)
        self.assertTrue(any("'see below'" in error for error in quoted_errors))
        self.assertTrue(any("ampersand in prose" in error for error in quoted_errors))

        for case in (
            "# Stata guide\n\nUse `{# literal` exactly.\nPlease see below #}.\n",
            "# Stata guide\n\n<code>{# literal</code>\nPlease see below #}.\n",
            "# Stata guide\n\nOpen [Google guide](x{#) see below #}).\n",
            '# Stata guide\n\nUse <span title="{#"> see below #}">x</span>.\n',
        ):
            with self.subTest(case=case):
                self.assertTrue(
                    any("'see below'" in error for error in self.lint(case))
                )

        unclosed = self.lint("# Stata guide\n\n{# unclosed\nPlease see below.\n")
        self.assertTrue(any("'see below'" in error for error in unclosed))

        multiline_literal = (
            "# Stata guide\n\n"
            "Prefix `literal\n{# inside code\nend`\n"
            "Please see below & later.\n#}\n"
        )
        literal_errors = self.lint(multiline_literal)
        self.assertTrue(any("'see below'" in error for error in literal_errors))
        self.assertTrue(
            any("ampersand in prose" in error for error in literal_errors)
        )

        raw_errors = self.lint(
            "# Stata guide\n\n"
            "{% raw %}\n{# Please see below & later #}\n{% endraw %}\n"
        )
        self.assertTrue(any("Jinja raw blocks" in error for error in raw_errors))

    def test_jinja_source_validation_is_exact_and_respects_protected_contexts(
        self,
    ) -> None:
        for raw_source in (
            "{%- raw -%}\ntext\n{%- endraw -%}",
            "{%+ raw %}\ntext\n{% endraw %}",
            "<div>{% raw %}text{% endraw %}</div>",
            '<img alt="{% raw %}text{% endraw %}" src=x>',
        ):
            with self.subTest(raw_source=raw_source):
                errors = self.lint(f"# Stata guide\n\n{raw_source}\n")
                self.assertTrue(any("Jinja raw blocks" in error for error in errors))

        for ordinary_source in (
            '{% set x = "{% raw %}" %}',
            "{# {% raw %} #}",
        ):
            with self.subTest(ordinary_source=ordinary_source):
                errors = self.lint(f"# Stata guide\n\n{ordinary_source}\n")
                self.assertFalse(any("Jinja raw blocks" in error for error in errors))

        for protected_source in (
            "```jinja\n{% raw %}\n{# unclosed\n```",
            "Use `{# unclosed` exactly.",
            "Open [Google guide](path{#).",
            '<span title="{#">Text</span>',
            "<?target {#?>",
            '<!DOCTYPE note SYSTEM "{#">',
            '<!DOCTYPE note SYSTEM "{#',
            "<!A {#",
            "<![CDATA[{#]]>",
        ):
            with self.subTest(protected_source=protected_source):
                errors = self.lint(f"# Stata guide\n\n{protected_source}\n")
                self.assertFalse(
                    any("Jinja" in error and "unsupported" in error for error in errors)
                )

        unbalanced = self.lint(
            "# Stata guide\n\n<div>{# unclosed <img src=x></div>\n"
        )
        self.assertTrue(any("unbalanced Jinja" in error for error in unbalanced))

    def test_multiline_jinja_does_not_reparse_document_tails(self) -> None:
        block_rules = (
            lint_skill_pack.STYLE_MARKDOWN_PARSER.block.ruler.get_active_rules()
        )
        self.assertLess(
            block_rules.index("style_jinja"),
            block_rules.index("lheading"),
        )

        source = "# Stata guide\n\n" + "{#\nhidden\n#}\n\n" * 300
        original = lint_skill_pack.STYLE_MARKDOWN_PROBE_PARSER.inline.parse
        parsed_characters = 0

        def counted_parse(value, *args, **kwargs):
            nonlocal parsed_characters
            parsed_characters += len(value)
            return original(value, *args, **kwargs)

        with mock.patch.object(
            lint_skill_pack.STYLE_MARKDOWN_PROBE_PARSER.inline,
            "parse",
            side_effect=counted_parse,
        ):
            self.assertEqual([], self.lint(source))

        self.assertLess(parsed_characters, len(source) * 6)

        adjacent = "# Stata guide\n\n" + "{#\nhidden\n#}\n" * 800
        self.assertEqual([], self.lint(adjacent))

        nested_literal = (
            "# Stata guide\n\n"
            + "<code>" * 800
            + "{# hidden #}"
            + "</code>" * 800
            + "\n"
        )
        self.assertEqual([], self.lint(nested_literal))

        malformed_literal = (
            "# Stata guide\n\n"
            + "<code>" * 1600
            + "</kbd>" * 1600
            + "\n"
        )
        self.assertEqual([], self.lint(malformed_literal))

        for malformed_source in (
            "<" * 8000,
            "<!--" * 4000,
            "<?" * 8000,
            "<![CDATA[" * 2000,
        ):
            with self.subTest(prefix=malformed_source[:9]):
                self.assertEqual(
                    ((), ()),
                    lint_skill_pack._jinja_source_issues(malformed_source),
                )

    def test_jinja_source_mapping_preserves_labels_and_container_prefixes(
        self,
    ) -> None:
        for visible in (
            "[Label {#](guide.md)",
            "![Alt {#](image.svg)",
            "[Label {% raw %}text{% endraw %}](guide.md)",
        ):
            with self.subTest(visible=visible):
                errors = self.lint(f"# Stata guide\n\n{visible}\n")
                self.assertTrue(
                    any(
                        "unbalanced Jinja" in error or "Jinja raw blocks" in error
                        for error in errors
                    )
                )

        for protected in (
            "> Intro\n> `{#`",
            "> Intro\n> [Label](path{#)",
            '> Intro\n> <span title="{#">Text</span>',
            "- Intro\n  `{#`",
            "[Label `]` text](path{#)",
            '[Label <span title="]">text</span>](path{#)',
            "[ref]: path{#\n\n[Label][ref]",
            "[ref]: good\n[ref]: path{#",
            "![![Alt](inner{#)](outer)",
            "![Text ![Alt](inner{#) tail](outer)",
            "| Head |\n| --- |\n| [Label](path{#) \\| tail |",
            "| Head |\n| --- |\n| before \\| [Label](path{#) |",
            "\\{# literal",
        ):
            with self.subTest(protected=protected):
                errors = self.lint(f"# Stata guide\n\n{protected}\n")
                self.assertFalse(any("unbalanced Jinja" in error for error in errors))

        even_escape = self.lint("# Stata guide\n\n\\\\{# active\n")
        self.assertTrue(any("unbalanced Jinja" in error for error in even_escape))

        for pseudo_html in (
            "Text <? unfinished {#",
            "Text <![CDATA[ unfinished {#",
            "Text <!-- unfinished {#",
        ):
            with self.subTest(pseudo_html=pseudo_html):
                errors = self.lint(f"# Stata guide\n\n{pseudo_html}\n")
                self.assertTrue(any("unbalanced Jinja" in error for error in errors))

        for invalid_html in (
            "Text <span x=< {# > tail",
            "Text < nottag {# > tail",
        ):
            with self.subTest(invalid_html=invalid_html):
                errors = self.lint(f"# Stata guide\n\n{invalid_html}\n")
                self.assertTrue(any("unbalanced Jinja" in error for error in errors))

    def test_jinja_closers_only_hide_protected_destination_and_code_source(
        self,
    ) -> None:
        for exposed in (
            "Start {# [Label #}](guide.md) Please see below & later #}.",
            "Start {# ![Alt #}](image.svg) Please see below & later #}.",
        ):
            with self.subTest(exposed=exposed):
                errors = self.lint(f"# Stata guide\n\n{exposed}\n")
                self.assertTrue(any("'see below'" in error for error in errors))
                self.assertTrue(any("ampersand in prose" in error for error in errors))

        for literal_tag in ("code", "kbd", "pre", "script", "style"):
            hidden = (
                "# Stata guide\n\n<div>{# hidden "
                f"<{literal_tag}>#}}</{literal_tag}> "
                "Please see below & later #}</div>\n"
            )
            with self.subTest(literal_tag=literal_tag):
                self.assertEqual([], self.lint(hidden))

        indented = (
            "{# start\n\n    #}\n\n# Hidden heading\n"
            "Please see below & later.\n#}\n\n# Stata guide\n"
        )
        self.assertEqual([], self.lint(indented))

    def test_image_alt_boundaries_follow_rendered_commonmark_semantics(self) -> None:
        for protected in (
            '![Please see {{ target }} below](image.svg)',
            '<img alt="Please see {{ target }} below" src=image.svg>',
        ):
            with self.subTest(protected=protected):
                self.assertFalse(
                    any(
                        "'see below'" in error
                        for error in self.lint(f"# Stata guide\n\n{protected}\n")
                    )
                )

        hardbreak = self.lint(
            "# Stata guide\n\n![Please see  \nbelow](image.svg)\n"
        )
        self.assertFalse(any("'see below'" in error for error in hardbreak))
        softbreak = self.lint(
            "# Stata guide\n\n![Please see\nbelow](image.svg)\n"
        )
        self.assertTrue(any("'see below'" in error for error in softbreak))

        literal_alt = self.lint(
            "# Stata guide\n\n"
            '<code><img alt="Please see below & later" src=image.svg></code>\n'
        )
        self.assertTrue(any("'see below'" in error for error in literal_alt))
        self.assertTrue(any("ampersand in prose" in error for error in literal_alt))

    def test_protected_text_separates_phrases_but_hidden_markup_does_not(
        self,
    ) -> None:
        protected = """# Stata guide

Please see `the table` below.
Please see {{ target }} below.
"""
        self.assertFalse(any("'see below'" in error for error in self.lint(protected)))

        for hidden in (
            "Please see {#\nhidden\n#} below.",
            "Please see <!--\nhidden\n--> below.",
        ):
            with self.subTest(hidden=hidden):
                errors = self.lint(f"# Stata guide\n\n{hidden}\n")
                self.assertTrue(any("'see below'" in error for error in errors))

    def test_html_code_keeps_link_image_and_heading_structure_visible(self) -> None:
        errors = self.lint(
            "# Stata guide\n\n"
            "<code><a href=x>here</a><img src=x></code>\n"
            "<h1>Second Heading</h1>\n"
        )

        self.assertTrue(any("descriptive link" in error for error in errors))
        self.assertTrue(any("alt attribute" in error for error in errors))
        self.assertTrue(any("raw HTML headings" in error for error in errors))

        markdown_nested = self.lint(
            "# Stata guide\n\n"
            "<code>[here](guide.md)</code>\n"
            "<kbd>![Please see below & later](image.svg)</kbd>\n"
        )
        self.assertTrue(any("descriptive link" in error for error in markdown_nested))
        self.assertTrue(any("'see below'" in error for error in markdown_nested))
        self.assertTrue(any("ampersand in prose" in error for error in markdown_nested))

        self.assertEqual(
            [],
            self.lint(
                "# Stata guide\n\n"
                "<pre>[here](guide.md) Please see below & later</pre>\n"
            ),
        )

        nested_anchors = self.lint(
            "# Stata guide\n\n"
            "<a href=x>here<a href=y>Google guide</a></a>\n"
        )
        self.assertTrue(any("descriptive link" in error for error in nested_anchors))

    def test_sentence_case_checks_unicode_heading_words(self) -> None:
        for heading in (
            "# Stata Über",
            "# Stata Évaluation",
            "# Stata Résumé",
            "# Stata &Uuml;ber",
        ):
            with self.subTest(heading=heading):
                self.assertTrue(
                    any("sentence case" in error for error in self.lint(heading + "\n"))
                )

    def test_protected_link_labels_report_the_first_visible_source_line(self) -> None:
        for source in (
            "Open [`\nhere`](guide.md).",
            '[<img\n alt="here"\n src=x>](guide.md)',
        ):
            with self.subTest(source=source):
                errors = self.lint(f"# Stata guide\n\n{source}\n")
                self.assertTrue(
                    any(
                        "sample.md:4:" in error and "descriptive link" in error
                        for error in errors
                    )
                )

    def test_scans_list_continuation_prose_but_not_blockquoted_fences(self) -> None:
        prose = """# Stata guide

- Step
    Open [click here](https://example.test) & see below.
"""
        errors = self.lint(prose)

        self.assertEqual(1, sum("descriptive link text" in error for error in errors))
        self.assertEqual(1, sum("ampersand in prose" in error for error in errors))
        self.assertEqual(1, sum("'see below'" in error for error in errors))

        quoted_fence = """# Stata guide

> ````text
> ## Fake Heading
> Open [click here](https://example.test) & see below.
> ````
"""

        self.assertEqual([], self.lint(quoted_fence))

    def test_supports_setext_headings_and_multiline_code_spans(self) -> None:
        text = """Stata guide
===========

Use `literal
Please note & see below` exactly.

Valid details
-------------
"""

        self.assertEqual([], self.lint(text))

        sentence_case_errors = self.lint(
            "# Stata guide\n\nFirst line\nTitle Case\n----------\n"
        )
        self.assertTrue(
            any(
                "sample.md:4:" in error and "sentence case" in error
                for error in sentence_case_errors
            )
        )

        for source, expected_line in (
            ("`code\nx` Workflow\nDetails\n=======\n", 3),
            ("<code>code\nx</code> Workflow\nDetails\n=======\n", 3),
            ("{{ value }} Workflow\nDetails\n=======\n", 2),
        ):
            with self.subTest(source=source):
                errors = self.lint(source)
                self.assertTrue(
                    any(
                        f"sample.md:{expected_line}:" in error
                        and "unexpected 'Details'" in error
                        for error in errors
                    )
                )

    def test_commonmark_line_endings_are_normalized_before_source_mapping(
        self,
    ) -> None:
        for source in (
            "# Stata guide\r\r{# unclosed\r",
            "# Stata guide\r\n\r{# unclosed\n",
        ):
            with self.subTest(source=source):
                errors = self.lint(source)
                self.assertTrue(
                    any(
                        "sample.md:3:" in error and "unbalanced Jinja" in error
                        for error in errors
                    )
                )

        duplicate_word_errors = self.lint("Title\nTitle\n=====\n")
        self.assertTrue(
            any(
                "sample.md:2:" in error and "sentence case" in error
                for error in duplicate_word_errors
            )
        )

    def test_checks_shortcut_references_and_escaped_image_markers(self) -> None:
        text = """# Stata guide

Open [here] or \![click here](https://example.test/inline).

[here]: https://example.test/reference
"""

        errors = self.lint(text)

        self.assertEqual(2, sum("descriptive link text" in error for error in errors))

    def test_does_not_parse_markdown_inside_raw_html_attributes(self) -> None:
        text = """# Stata guide

<span data-note="[click here](internal)">Descriptive text</span>
"""

        self.assertEqual([], self.lint(text))

    def test_parses_html_anchors_and_img_attributes_quote_aware(self) -> None:
        valid = """# Stata guide

<img title="x > y" alt="Diagram" src="diagram.svg">
"""
        self.assertEqual([], self.lint(valid))

        invalid = """# Stata guide

<a href="guide.html">here</a>
<img
 src="diagram.svg">
"""
        errors = self.lint(invalid)
        self.assertEqual(1, sum("descriptive link text" in error for error in errors))
        self.assertEqual(1, sum("alt attribute" in error for error in errors))

        multiline_alt = """# Stata guide

<img
 alt="A & B"
 src="diagram.svg">
"""
        self.assertTrue(
            any(
                "sample.md:4:" in error and "ampersand in prose" in error
                for error in self.lint(multiline_alt)
            )
        )

        multiline_value = """# Stata guide

<img alt="First line
A & B" src="diagram.svg">
"""
        self.assertTrue(
            any(
                "sample.md:4:" in error and "ampersand in prose" in error
                for error in self.lint(multiline_value)
            )
        )

    def test_html_breaks_preserve_prose_and_link_word_boundaries(self) -> None:
        text = """# Stata guide

Please see<br>below.
<a href="guide.md">click<br>here</a>
[click<br>here](guide.md)
<div>Please see</div><div>below.</div>
"""

        errors = self.lint(text)

        self.assertEqual(2, sum("'see below'" in error for error in errors))
        self.assertEqual(2, sum("descriptive link" in error for error in errors))
        self.assertTrue(
            any(
                "unexpected 'Details'" in error
                for error in self.lint("# Workflow<br>Details\n")
            )
        )

    def test_rendered_line_breaks_do_not_hide_directional_phrases(self) -> None:
        for separator in ("\n", "  \n", "\\\n"):
            with self.subTest(separator=separator):
                errors = self.lint(
                    f"# Stata guide\n\nPlease see{separator}below.\n"
                )
                self.assertTrue(any("'see below'" in error for error in errors))

    def test_mismatched_literal_html_cannot_suppress_later_prose(self) -> None:
        errors = self.lint(
            "# Stata guide\n\n"
            "<code><kbd>x</code></kbd> Please see below.\n"
        )

        self.assertTrue(any("'see below'" in error for error in errors))

        fake_close = self.lint(
            "# Stata guide\n\n"
            '[<code><span title="</code>">{{ here }}</span></code>](guide.md)\n'
        )
        self.assertTrue(any("descriptive link" in error for error in fake_close))

    def test_malformed_html_declarations_cannot_crash_block_scanning(self) -> None:
        for malformed in (
            "<div>\n<![\n",
            "<div>\n<![x]\n",
            "<h1>\n<![## >\n",
        ):
            with self.subTest(malformed=malformed):
                errors = self.lint(f"# Stata guide\n\n{malformed}")
                self.assertIsInstance(errors, list)

        heading_errors = self.lint("# Stata guide\n\n<h1>\n<![## >\n")
        self.assertTrue(any("raw HTML headings" in error for error in heading_errors))

    def test_link_text_in_code_images_and_unclosed_html_is_checked(self) -> None:
        text = """# Stata guide

Open [`here`](guide.md).
Open [![here](image.svg)](guide.md).
<a href="guide.md"><code>here</code></a>
<a href="guide.md"><img src="image.svg" alt="here"></a>
<a href="guide.md">here
"""

        errors = self.lint(text)

        self.assertEqual(5, sum("descriptive link text" in error for error in errors))
        self.assertFalse(any("alt attribute" in error for error in errors))

    def test_cross_grammar_and_empty_link_labels_are_checked(self) -> None:
        text = """# Stata guide

[<code>here</code>](guide.md)
[<kbd>here</kbd>](guide.md)
[<img src="image.svg" alt="here">](guide.md)
[](guide.md)
[![](image.svg)](guide.md)
<a href="guide.md"></a>
<a href="guide.md"><img src="image.svg" alt=""></a>
"""

        errors = self.lint(text)

        self.assertEqual(7, sum("descriptive link text" in error for error in errors))
        self.assertFalse(any("alt attribute" in error for error in errors))

    def test_comment_only_and_empty_html_headings_are_empty(self) -> None:
        for heading in (
            "# <!-- comment -->\n",
            "# <span></span>\n",
            "# {# comment #}\n",
            "# &#8203;\n",
            "# &zwj;\n",
            "# &shy;\n",
        ):
            with self.subTest(heading=heading):
                errors = self.lint(heading)
                self.assertTrue(any("empty heading" in error for error in errors))

    def test_non_link_spacing_and_invalid_fence_info_follow_commonmark(self) -> None:
        text = """# Stata guide

[here] (guide.html) is ordinary prose.

```bad`info
## Real details
```
"""

        errors = self.lint(text)

        self.assertFalse(any("descriptive link text" in error for error in errors))
        self.assertFalse(any("exactly one level-1" in error for error in errors))

    def test_counts_atx_and_setext_h1s_together(self) -> None:
        errors = self.lint("# Stata guide\n\nOther\n=====\n")

        self.assertTrue(any("found 2" in error for error in errors))

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
        self.assertEqual(3, sum("descriptive link text" in error for error in errors))
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

    def test_advisory_report_tracks_total_separately_from_display_cap(self) -> None:
        report = lint_skill_pack._bounded_advisory_report(
            [f"candidate {index}" for index in range(302)],
            200,
        )

        self.assertEqual(200, len(report.items))
        self.assertEqual(302, report.total_count)


if __name__ == "__main__":
    unittest.main()
