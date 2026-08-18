#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from html import unescape
from html.entities import html5 as HTML5_ENTITIES
from html.parser import HTMLParser
from pathlib import Path
import argparse
import re
import stat
import sys
import tempfile
import unicodedata

from markdown_it import MarkdownIt
from markdown_it.token import Token

from runtime_guard import require_supported_runtime

require_supported_runtime()

from jinja2 import Environment, TemplateSyntaxError
from markdown_it.helpers.parse_link_label import parseLinkLabel
from markdown_it.rules_inline.state_inline import StateInline

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    MANIFEST_ROOT,
    PACKAGE_LOCK_ROOT,
    PROMPT_CASES_PATH,
    REPO_ROOT,
    UPSTREAM_REPO_URL,
    is_safe_slug,
    iter_content_entries,
    load_skill_config,
    read_text,
    read_yaml,
)


REQUIRED_FIELDS = {
    "slug",
    "skill",
    "section",
    "order",
    "title",
    "trigger",
    "aliases",
    "routing_terms",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "assumptions",
    "workflows",
    "validation_case",
    "validation_mode",
    "related_refs",
    "install_commands",
    "smoke_test",
    "provenance",
}
LIST_FIELDS = {
    "aliases",
    "routing_terms",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "assumptions",
    "workflows",
    "related_refs",
    "install_commands",
}
NONEMPTY_LIST_FIELDS = {
    "aliases",
    "routing_terms",
    "commands",
    "source_topics",
    "syntax_patterns",
    "gotchas",
    "assumptions",
    "workflows",
}
VALIDATION_MODES = {"stata", "compilation", "manual-review"}
STYLE_WORD_RE = re.compile(r"[^\W\d_][\w+.-]*", re.UNICODE)
DEFAULT_STYLE_ALLOWED_CAPITALIZED_WORDS = {
    "C",
    "C++",
    "Carlo",
    "Cox",
    "GMM",
    "H2O",
    "Heckman",
    "Java",
    "Kaplan-Meier",
    "Mata",
    "Monte",
    "Office",
    "Python",
    "R",
    "SDK",
    "Stata",
    "Unicode",
    "Word",
}
DOCUMENTATION_STYLE_PATH = REPO_ROOT / "config" / "documentation-style.yaml"
STYLE_PROFILE_URL = "https://developers.google.com/style"
STYLE_PROFILE_DATE_RE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
STYLE_REQUIRED_PROTECTED_CONTEXTS = {
    "yaml-frontmatter",
    "fenced-code",
    "indented-code",
    "inline-code",
    "raw-html-code",
    "raw-html-tags",
    "jinja-control",
    "link-destinations",
    "exact-technical-identifiers",
}
STYLE_HTML_LITERAL_TAGS = {"code", "kbd", "pre", "script", "style"}
STYLE_HTML_BREAK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "caption",
    "colgroup",
    "dd",
    "details",
    "div",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hgroup",
    "hr",
    "li",
    "legend",
    "main",
    "menu",
    "nav",
    "ol",
    "p",
    "section",
    "summary",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
STYLE_ENTITY_RE = re.compile(
    r"&#(?:[xX][0-9A-Fa-f]{1,6}|[0-9]{1,7});"
    r"|&(?P<named>[A-Za-z][A-Za-z0-9]{1,31});"
)
STYLE_SOURCE_START = "style_source_start"
STYLE_SOURCE_END = "style_source_end"
STYLE_PROTECTED_BOUNDARY = " ⟂ "
STYLE_JINJA_PAIRS = (
    ("{#", "#}", "style_jinja_control"),
    ("{%", "%}", "style_jinja_control"),
    ("{{", "}}", "style_jinja_value"),
)
STYLE_JINJA_OPENER_RE = re.compile(r"{[#%{]")


STYLE_MARKDOWN_PARSER = MarkdownIt("commonmark", {"html": True}).enable("table")
STYLE_JINJA_ENVIRONMENT = Environment()


def _install_style_jinja_rules(parser: MarkdownIt) -> None:
    """Protect balanced Jinja spans before CommonMark interprets their contents."""

    literal_pattern = re.compile(
        r"<\s*(?P<closing>/)?\s*"
        r"(?P<tag>code|kbd|pre|script|style)\b"
        r"(?P<body>(?:[^>\"']+|\"[^\"]*\"|'[^']*')*)>\s*",
        re.IGNORECASE,
    )

    def inside_html_literal(state: object) -> bool:
        token_count, stack = getattr(
            state,
            "_style_html_literal_cache",
            (0, None),
        )
        if stack is None or token_count > len(state.tokens):
            token_count = 0
            stack = _StyleLiteralStack()
        for token in state.tokens[token_count:]:
            if token.type != "html_inline":
                continue
            match = literal_pattern.fullmatch(token.content)
            if match is None:
                continue
            tag = match.group("tag").casefold()
            if match.group("closing"):
                stack.close(tag)
            elif not match.group("body").rstrip().endswith("/"):
                stack.push(tag)
        state._style_html_literal_cache = (len(state.tokens), stack)
        return bool(stack)

    def protected_inline_spans(value: str) -> tuple[tuple[int, int], ...]:
        tokens: list[Token] = []
        STYLE_MARKDOWN_PROBE_PARSER.inline.parse(
            value,
            STYLE_MARKDOWN_PROBE_PARSER,
            {},
            tokens,
        )
        spans: list[tuple[int, int]] = []
        literal_stack = _StyleLiteralStack()
        for token in tokens:
            span_start = token.meta.get(STYLE_SOURCE_START)
            span_end = token.meta.get(STYLE_SOURCE_END)
            if (
                isinstance(span_start, int)
                and isinstance(span_end, int)
                and span_end >= span_start
            ):
                spans.extend(
                    _relative_inline_protected_spans(
                        value,
                        [token],
                    )
                )
            if token.type != "html_inline":
                continue
            match = literal_pattern.fullmatch(token.content)
            if match is None:
                continue
            absolute_start = span_start if isinstance(span_start, int) else 0
            absolute_end = (
                span_end
                if isinstance(span_end, int)
                else len(token.content)
            )
            tag = match.group("tag").casefold()
            if match.group("closing"):
                opening = literal_stack.close(tag)
                if opening is not None:
                    spans.append((opening, absolute_end))
            elif not match.group("body").rstrip().endswith("/"):
                literal_stack.push(tag, absolute_start)
        spans.extend(
            (opening, len(value))
            for _, opening in literal_stack.openings()
        )

        merged: list[list[int]] = []
        for begin, end in sorted(spans):
            if merged and begin <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([begin, end])
        return tuple((begin, end) for begin, end in merged)

    def find_closer(
        value: str,
        start: int,
        closer: str,
        protected: tuple[tuple[int, int], ...],
    ) -> int:
        position = start
        low = 0
        high = len(protected)
        while low < high:
            middle = (low + high) // 2
            if protected[middle][1] <= start:
                low = middle + 1
            else:
                high = middle
        span_index = low
        quote = ""
        while position < len(value):
            character = value[position]
            if closer != "#}" and quote:
                if character == "\\":
                    position += 2
                    continue
                if character == quote:
                    quote = ""
                    position += 1
                    while (
                        span_index < len(protected)
                        and protected[span_index][0] < position
                    ):
                        span_index += 1
                    continue
                position += 1
                continue
            while (
                span_index < len(protected)
                and protected[span_index][1] <= position
            ):
                span_index += 1
            if (
                span_index < len(protected)
                and protected[span_index][0] <= position
            ):
                position = protected[span_index][1]
                continue
            if closer != "#}" and character in {'"', "'"}:
                quote = character
                position += 1
                continue
            if value.startswith(closer, position):
                return position
            position += 1
        return -1

    def inline_rule(state: object, silent: bool) -> bool:
        start = state.pos
        if inside_html_literal(state):
            return False
        missing_closers = getattr(
            state,
            "_style_jinja_missing_closers",
            set(),
        )
        for opener, closer, token_type in STYLE_JINJA_PAIRS:
            if not state.src.startswith(opener, start):
                continue
            if closer in missing_closers:
                return False
            closing = find_closer(
                state.src,
                start + len(opener),
                closer,
                (),
            )
            protected_markers = (
                closing >= 0
                and any(
                    marker in state.src[start:closing]
                    for marker in ("[", "<", chr(96))
                )
            )
            if closing < 0 or protected_markers:
                protected = getattr(
                    state,
                    "_style_jinja_protected_spans",
                    None,
                )
                if protected is None:
                    protected = protected_inline_spans(state.src)
                    state._style_jinja_protected_spans = protected
                closing = find_closer(
                    state.src,
                    start + len(opener),
                    closer,
                    protected,
                )
            if closing < 0:
                missing_closers.add(closer)
                state._style_jinja_missing_closers = missing_closers
                return False
            end = closing + len(closer)
            if not silent:
                token = state.push(token_type, "", 0)
                token.content = state.src[start:end]
                token.meta[STYLE_SOURCE_START] = start
                token.meta[STYLE_SOURCE_END] = end
            state.pos = end
            return True
        return False

    def unprotected_opener(
        state: object,
        start_line: int,
    ) -> tuple[int, str, str] | None:
        line_start = state.bMarks[start_line] + state.tShift[start_line]
        line_end = state.eMarks[start_line]
        value = state.src[line_start:line_end]
        if "{" not in value:
            return None

        protected = protected_inline_spans(value)
        for opener, closer, _ in STYLE_JINJA_PAIRS:
            if not value.startswith(opener):
                continue
            closing = find_closer(
                value,
                len(opener),
                closer,
                protected,
            )
            if closing >= 0:
                return None
            return line_start, opener, closer

        span_index = 0
        for match in re.finditer(r"{[#%{]", value):
            position = match.start()
            backslashes = 0
            cursor = position - 1
            while cursor >= 0 and value[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2:
                continue
            while (
                span_index < len(protected)
                and protected[span_index][1] <= position
            ):
                span_index += 1
            if (
                span_index < len(protected)
                and protected[span_index][0] <= position
            ):
                continue
            for opener, closer, _ in STYLE_JINJA_PAIRS:
                if value.startswith(opener, position):
                    if find_closer(
                        value,
                        position + len(opener),
                        closer,
                        protected,
                    ) >= 0:
                        break
                    return line_start + position, opener, closer
        return None

    def block_rule(
        state: object,
        start_line: int,
        end_line: int,
        silent: bool,
    ) -> bool:
        if state.is_code_block(start_line):
            return False
        start = state.bMarks[start_line] + state.tShift[start_line]
        opener_data = unprotected_opener(state, start_line)
        if opener_data is None:
            return False
        opening, opener, closer = opener_data
        missing_closers = getattr(
            state,
            "_style_jinja_block_missing_closers",
            set(),
        )
        cache_key = (closer, end_line)
        if cache_key in missing_closers:
            return False
        limit = state.eMarks[end_line - 1]
        protected_source = getattr(
            state,
            "_style_jinja_document_protected_source",
            None,
        )
        if protected_source is None:
            protected_source = _jinja_lexer_source(state.src)
            state._style_jinja_document_protected_source = protected_source
        closing = find_closer(
            protected_source,
            opening + len(opener),
            closer,
            (),
        )
        if closing < 0 or closing >= limit:
            missing_closers.add(cache_key)
            state._style_jinja_block_missing_closers = missing_closers
            return False
        for next_line in range(start_line, end_line):
            if closing > state.eMarks[next_line]:
                continue
            closing_end = closing + len(closer)
            line_end = state.eMarks[next_line]
            if silent:
                return True
            state.line = next_line + 1
            hidden_block = (
                opening == start
                and not state.src[closing_end:line_end].strip()
            )
            token = state.push(
                "style_jinja_block" if hidden_block else "inline",
                "",
                0,
            )
            token.map = [start_line, next_line + 1]
            token.content = state.getLines(
                start_line,
                next_line + 1,
                state.blkIndent,
                True,
            )
            if token.type == "inline":
                token.children = []
            return True
        return False

    parser.inline.ruler.before("text", "style_jinja", inline_rule)
    parser.block.ruler.before("lheading", "style_jinja", block_rule)


def _install_style_source_spans(parser: MarkdownIt) -> None:
    """Annotate protected inline tokens with markdown-it source spans."""

    ruler = parser.inline.ruler
    active_rules = dict(
        zip(ruler.get_active_rules(), ruler.getRules(""), strict=True)
    )
    target_types = {
        "backticks": {"code_inline"},
        "html_inline": {"html_inline"},
        "image": {"image"},
        "link": {"link_open", "link_close"},
        "autolink": {"link_open", "link_close"},
    }
    missing = sorted(set(target_types) - set(active_rules))
    if missing:
        raise RuntimeError(
            "documentation parser lacks required inline rules: "
            + ", ".join(missing)
        )

    for rule_name, token_types in target_types.items():
        original = active_rules[rule_name]

        def wrapped(
            state: object,
            silent: bool,
            *,
            _original: object = original,
            _token_types: set[str] = token_types,
        ) -> bool:
            start = state.pos
            token_count = len(state.tokens)
            matched = _original(state, silent)
            if matched and not silent:
                for token in state.tokens[token_count:]:
                    if (
                        token.type in _token_types
                        and STYLE_SOURCE_START not in token.meta
                    ):
                        token.meta[STYLE_SOURCE_START] = start
                        token.meta[STYLE_SOURCE_END] = state.pos
            return matched

        ruler.at(rule_name, wrapped)


STYLE_MARKDOWN_PROBE_PARSER = MarkdownIt(
    "commonmark",
    {"html": True},
).enable("table")
_install_style_source_spans(STYLE_MARKDOWN_PROBE_PARSER)
_install_style_jinja_rules(STYLE_MARKDOWN_PARSER)
_install_style_source_spans(STYLE_MARKDOWN_PARSER)
STYLE_RAW_AMPERSAND_RE = re.compile(r"&")
STYLE_PASSIVE_RE = re.compile(
    r"\b(?:am|are|be|been|being|is|was|were)\s+"
    r"(?:[A-Za-z]+ly\s+)?[A-Za-z]+(?:ed|en)\b",
    re.IGNORECASE,
)
STYLE_SENTENCE_RE = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+")
STYLE_PROCEDURE_HEADINGS = {
    "installation",
    "quick start",
    "workflow",
}
STYLE_COMMON_COMMAND_WORDS = {
    "do",
    "for",
    "help",
    "if",
    "list",
    "return",
    "run",
    "save",
    "search",
    "use",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GENERIC_TEXT = {
    "check missing-value behavior option defaults and stored results before chaining commands",
    "start with the official help topics attached to then tailor the syntax to the current dataset",
    "prefer a small batch-mode smoke test before applying the command sequence to a large dataset",
    "package syntax and dependencies vary across versions verify the installed help file before finalizing code",
    "confirm the package and any dependencies are installed on the active adopath before writing code that uses it",
    "pair the package command with a small reproducible example before folding it into a larger do-file",
    "plan the interface and the validation path before writing plugin code or wrapper ado-files",
    "use batch-mode stata logs and compiler output together when debugging plugin failures",
    "a plugin crash terminates the stata session so treat every memory access and return code as high risk",
}
GENERIC_ROUTING_TERMS = {
    "do",
    "for",
    "help",
    "if",
    "predict",
    "replace",
    "return",
    "run",
    "save",
    "search",
    "use",
    "which",
}
UNSAFE_INSTALL_PATTERNS = (
    (re.compile(r"(?i),\s*replace\b"), "must not use replace by default"),
    (re.compile(r"(?i)\badoupdate\b"), "must not update the active environment"),
    (re.compile(r"(?i)\bhttps?://[^\s\"')]+/(?:main|master)(?:/|$)"), "must pin GitHub sources"),
    (re.compile(r"(?i)\bhttp://"), "must use HTTPS"),
    (re.compile(r"^\s*!"), "must not invoke a shell"),
    (re.compile(r"(?i)^\s*shell\b"), "must not invoke a shell"),
)
READ_ONLY_PREFLIGHT_RE = re.compile(
    r"(?i)^(?:"
    r"which(?:\s|$)|"
    r"ado\s+describe(?:\s|$)|"
    r"adopath(?:\s|$)|"
    r"sysdir(?:\s|$)|"
    r"help(?:\s|$)|"
    r"search(?:\s|$)|"
    r"findit(?:\s|$)|"
    r"ssc\s+describe(?:\s|$)|"
    r"net\s+describe(?:\s|$)"
    r")"
)
MANIFEST_EXECUTABLE_FIELDS = {
    "section",
    "title",
    "trigger",
    "routing_terms",
    "commands",
    "validation_case",
    "install_commands",
    "smoke_test",
}
TRACK_METADATA_FILES = {"stata.trk", "backup.trk"}
STABLE_WHITESPACE = frozenset(
    " \t\n\r\v\f"
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
UNICODE_PROSE_SEPARATORS = frozenset(
    "\u00ab\u00b7\u00bb"
    "\u2010\u2011\u2012\u2013\u2014\u2015"
    "\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f"
    "\u2022\u2026\u2027"
    "\u2039\u203a\u2044\u2215"
)
ROUTING_ASCII_SYMBOLS = frozenset("_+#")
COPY_ASCII_SYMBOLS = frozenset("+#")
COPY_SYMBOL_TRANSLATION = str.maketrans("", "", "+#")
MEANINGFUL_COPY_SYMBOL_TOKENS = frozenset({"c++", "c#"})
OUTLINED_LATIN_TRANSLATION = str.maketrans(
    {
        0x1CCD6 + offset: chr(ord("A") + offset)
        for offset in range(26)
    }
)
MIN_COMPACT_TRIGGER_LENGTH = 48
MAX_COPY_TEXT_LENGTH = 4096
MAX_COPY_TOKEN_LENGTH = 512
MAX_FUSED_SEGMENT_TOKEN_LENGTH = 96
COPY_SIMILARITY_GRAM_SIZE = 3
COPY_SIMILARITY_PERCENT = 85
COPY_SIMILARITY_THRESHOLD = COPY_SIMILARITY_PERCENT / 100
COPY_MAX_EDIT_PERCENT = 100 - COPY_SIMILARITY_PERCENT


@dataclass(frozen=True)
class CopyForms:
    """Obfuscation-resistant text with and without real whitespace."""

    tokens: tuple[str, ...]
    spaced: str
    compact: str
    plain_tokens: tuple[str, ...]
    plain_spaced: str
    plain_compact: str


def nfkc_casefold(value: str) -> str:
    """Return stable compatibility-normalized, case-insensitive text."""

    value = value.translate(OUTLINED_LATIN_TRANSLATION)
    compatible = unicodedata.normalize("NFKC", value)
    return unicodedata.normalize("NFKC", compatible.casefold())


def normalized_text(value: str) -> str:
    """Normalize prose without changing word-count semantics."""

    folded = nfkc_casefold(value)
    prose = "".join(
        character if character.isalnum() else " "
        for character in folded
    )
    return " ".join(prose.split())


def copy_forms(value: str) -> CopyForms:
    """Return stable hard-token and compact forms for integrity checks."""

    folded = unicodedata.normalize("NFKD", nfkc_casefold(value))
    tokens: list[str] = []
    token: list[str] = []
    for character in folded:
        if character in STABLE_WHITESPACE:
            if token:
                tokens.append("".join(token))
                token = []
            continue
        if character.isascii():
            if character.isalnum() or character in COPY_ASCII_SYMBOLS:
                token.append(character)
            continue
        if unicodedata.category(character)[:1] in {"L", "N"}:
            token.append(character)
    if token:
        tokens.append("".join(token))
    frozen_tokens = tuple(tokens)
    plain_tokens = tuple(
        plain_token
        for raw_token in frozen_tokens
        if (plain_token := raw_token.translate(COPY_SYMBOL_TRANSLATION))
    )
    return CopyForms(
        tokens=frozen_tokens,
        spaced=" ".join(frozen_tokens),
        compact="".join(frozen_tokens),
        plain_tokens=plain_tokens,
        plain_spaced=" ".join(plain_tokens),
        plain_compact="".join(plain_tokens),
    )


def copy_forms_within_limits(forms: CopyForms) -> bool:
    """Return whether normalized integrity forms stay within fixed bounds."""

    return (
        len(forms.spaced) <= MAX_COPY_TEXT_LENGTH
        and len(forms.compact) <= MAX_COPY_TEXT_LENGTH
        and all(
            len(token) <= MAX_COPY_TOKEN_LENGTH
            for token in forms.tokens
        )
    )


def copy_text_limit_error(value: str) -> str | None:
    """Describe the first raw or normalized integrity limit violation."""

    if len(value) > MAX_COPY_TEXT_LENGTH:
        return f"exceeds {MAX_COPY_TEXT_LENGTH} characters"
    forms = copy_forms(value)
    if (
        len(forms.spaced) > MAX_COPY_TEXT_LENGTH
        or len(forms.compact) > MAX_COPY_TEXT_LENGTH
    ):
        return (
            f"normalizes beyond {MAX_COPY_TEXT_LENGTH} characters"
        )
    if any(
        len(token) > MAX_COPY_TOKEN_LENGTH
        for token in forms.tokens
    ):
        return (
            f"contains a token longer than "
            f"{MAX_COPY_TOKEN_LENGTH} characters"
        )
    return None


def copy_text_within_limits(value: str) -> bool:
    """Return whether integrity matching can process text within fixed bounds."""

    return copy_text_limit_error(value) is None


def bounded_copy_similarity(left: str, right: str) -> float:
    """Return linear-time ordered and trigram similarity for bounded copy text."""

    if left == right:
        return 1.0
    positional_similarity = 0.0
    if left and len(left) == len(right):
        positional_similarity = sum(
            left_character == right_character
            for left_character, right_character in zip(left, right)
        ) / len(left)
    gram_size = COPY_SIMILARITY_GRAM_SIZE
    if min(len(left), len(right)) < gram_size:
        return positional_similarity
    left_grams = Counter(
        left[index : index + gram_size]
        for index in range(len(left) - gram_size + 1)
    )
    right_grams = Counter(
        right[index : index + gram_size]
        for index in range(len(right) - gram_size + 1)
    )
    overlap = sum((left_grams & right_grams).values())
    trigram_similarity = (2 * overlap) / (
        len(left) + len(right) - (2 * gram_size) + 2
    )
    return max(positional_similarity, trigram_similarity)


def bounded_edit_similar(left: str, right: str) -> bool:
    """Return whether bit-parallel edit distance meets the copy threshold."""

    maximum_length = max(len(left), len(right))
    if maximum_length == 0:
        return True
    maximum_edits = (
        maximum_length * COPY_MAX_EDIT_PERCENT
    ) // 100
    if abs(len(left) - len(right)) > maximum_edits:
        return False
    if not left or not right:
        return maximum_length <= maximum_edits
    if len(left) > len(right):
        left, right = right, left

    pattern_length = len(left)
    bitmask = (1 << pattern_length) - 1
    high_bit = 1 << (pattern_length - 1)
    character_masks: dict[str, int] = {}
    for index, character in enumerate(left):
        character_masks[character] = (
            character_masks.get(character, 0) | (1 << index)
        )

    positive = bitmask
    negative = 0
    score = pattern_length
    text_length = len(right)
    for index, character in enumerate(right, start=1):
        equal = character_masks.get(character, 0)
        vertical = equal | negative
        horizontal = (
            ((((equal & positive) + positive) ^ positive) | equal)
            & bitmask
        )
        positive_horizontal = (
            negative | ~(horizontal | positive)
        ) & bitmask
        negative_horizontal = positive & horizontal
        if positive_horizontal & high_bit:
            score += 1
        elif negative_horizontal & high_bit:
            score -= 1
        positive_horizontal = (
            (positive_horizontal << 1) | 1
        ) & bitmask
        negative_horizontal = (
            negative_horizontal << 1
        ) & bitmask
        positive = (
            negative_horizontal
            | ~(vertical | positive_horizontal)
        ) & bitmask
        negative = (positive_horizontal & vertical) & bitmask
        if score - (text_length - index) > maximum_edits:
            return False

    return score <= maximum_edits


def bounded_copy_near_verbatim(left: str, right: str) -> bool:
    """Return whether bounded text meets the near-verbatim copy policy."""

    return (
        bounded_copy_similarity(left, right)
        >= COPY_SIMILARITY_THRESHOLD
        or bounded_edit_similar(left, right)
    )


def normalized_copy_text(value: str) -> str:
    """Normalize integrity text without collapsing real whitespace."""

    return copy_forms(value).plain_spaced


def signature_aware_copy_text(
    forms: CopyForms,
    signature_forms: CopyForms,
    *,
    allow_ambiguous_signature_match: bool = False,
) -> tuple[tuple[str, ...], str, str]:
    """Strip unexpected +/# while preserving meaningful signature tokens."""

    def symbol_subsequence(expected: str, observed: str) -> bool:
        remaining = iter(observed)
        return all(symbol in remaining for symbol in expected)

    def matches_padded_token(observed: str, expected: str) -> bool:
        return (
            observed.translate(COPY_SYMBOL_TRANSLATION)
            == expected.translate(COPY_SYMBOL_TRANSLATION)
            and symbol_subsequence(
                "".join(
                    symbol
                    for symbol in expected
                    if symbol in COPY_ASCII_SYMBOLS
                ),
                "".join(
                    symbol
                    for symbol in observed
                    if symbol in COPY_ASCII_SYMBOLS
                ),
            )
        )

    def join_symbol_only_tokens(
        source_tokens: tuple[str, ...],
    ) -> tuple[str, ...]:
        coalesced: list[str] = []
        leading_symbols = ""
        for token in source_tokens:
            if token and all(
                character in COPY_ASCII_SYMBOLS
                for character in token
            ):
                if coalesced:
                    coalesced[-1] += token
                else:
                    leading_symbols += token
                continue
            coalesced.append(f"{leading_symbols}{token}")
            leading_symbols = ""
        if leading_symbols:
            if coalesced:
                coalesced[-1] += leading_symbols
            else:
                coalesced.append(leading_symbols)
        return tuple(coalesced)

    signature_tokens = join_symbol_only_tokens(signature_forms.tokens)
    signature_symbol_tokens = frozenset(
        token
        for token in signature_tokens
        if any(symbol in token for symbol in COPY_ASCII_SYMBOLS)
    )
    expected_symbol_tokens = (
        signature_symbol_tokens | MEANINGFUL_COPY_SYMBOL_TOKENS
    )
    expected_symbols_by_plain: dict[str, frozenset[str]] = {
        plain: frozenset(
            expected
            for expected in expected_symbol_tokens
            if expected.translate(COPY_SYMBOL_TRANSLATION) == plain
        )
        for plain in {
            expected.translate(COPY_SYMBOL_TRANSLATION)
            for expected in expected_symbol_tokens
        }
        if plain
    }
    signature_plain_tokens = frozenset(
        plain
        for token in signature_tokens
        if (plain := token.translate(COPY_SYMBOL_TRANSLATION))
    )
    signature_plain_compact = "".join(
        token.translate(COPY_SYMBOL_TRANSLATION)
        for token in signature_tokens
    )
    signature_symbol_at: dict[int, str] = {}
    signature_plain_c_at: set[int] = set()
    signature_cursor = 0
    for token in signature_tokens:
        plain = token.translate(COPY_SYMBOL_TRANSLATION)
        if not plain:
            continue
        signature_cursor += len(plain)
        if any(symbol in token for symbol in COPY_ASCII_SYMBOLS):
            signature_symbol_at[signature_cursor - 1] = token
        elif plain == "c":
            signature_plain_c_at.add(signature_cursor - 1)

    def fused_symbol_segments(token: str) -> tuple[str, ...]:
        if len(token) > MAX_FUSED_SEGMENT_TOKEN_LENGTH:
            return (token,)
        plain_characters: list[str] = []
        plain_to_raw: list[int] = []
        for index, character in enumerate(token):
            if character not in COPY_ASCII_SYMBOLS:
                plain_characters.append(character)
                plain_to_raw.append(index)
        plain_token = "".join(plain_characters)
        candidates: list[tuple[tuple[int, int], tuple[str, ...]]] = []
        for expected_plain, expected_tokens in expected_symbols_by_plain.items():
            plain_start = plain_token.find(expected_plain)
            while plain_start >= 0:
                raw_start = plain_to_raw[plain_start]
                raw_end = (
                    plain_to_raw[plain_start + len(expected_plain) - 1] + 1
                )
                while (
                    raw_end < len(token)
                    and token[raw_end] in COPY_ASCII_SYMBOLS
                ):
                    raw_end += 1
                candidate = token[raw_start:raw_end]
                left = token[:raw_start]
                right = token[raw_end:]
                if (
                    candidate[-1] in COPY_ASCII_SYMBOLS
                    and any(
                        matches_padded_token(candidate, expected)
                        for expected in expected_tokens
                    )
                    and (left or right)
                    and (
                        not left
                        or left.translate(COPY_SYMBOL_TRANSLATION)
                        in signature_plain_tokens
                    )
                    and (
                        not right
                        or (
                            right[0].isalnum()
                            and right.translate(COPY_SYMBOL_TRANSLATION)
                            in signature_plain_tokens
                        )
                    )
                ):
                    pieces = tuple(
                        piece for piece in (left, candidate, right) if piece
                    )
                    score = (
                        int(bool(left)) + int(bool(right)),
                        -(raw_end - raw_start),
                    )
                    candidates.append((score, pieces))
                plain_start = plain_token.find(
                    expected_plain,
                    plain_start + 1,
                )
        if not candidates:
            return (token,)
        best_score = min(score for score, _ in candidates)
        best = {
            pieces
            for score, pieces in candidates
            if score == best_score
        }
        return next(iter(best)) if len(best) == 1 else (token,)

    def coalesce_symbol_continuations(
        source_tokens: tuple[str, ...],
    ) -> tuple[str, ...]:
        coalesced: list[str] = []
        leading_symbols = ""
        for token in source_tokens:
            if token and all(
                character in COPY_ASCII_SYMBOLS
                for character in token
            ):
                if coalesced:
                    coalesced[-1] += token
                else:
                    leading_symbols += token
                continue
            token = f"{leading_symbols}{token}"
            leading_symbols = ""
            if coalesced:
                prefix = token[: len(token) - len(token.lstrip("+#"))]
                candidate = f"{coalesced[-1]}{prefix}"
                if prefix and any(
                    matches_padded_token(candidate, expected)
                    for expected in expected_symbol_tokens
                ):
                    coalesced[-1] = candidate
                    token = token[len(prefix) :]
            if token:
                coalesced.extend(fused_symbol_segments(token))
        if leading_symbols:
            if coalesced:
                coalesced[-1] += leading_symbols
            else:
                coalesced.append(leading_symbols)
        return tuple(coalesced)

    def normalized_token_for(
        token: str,
        allowed_signature_tokens: frozenset[str],
    ) -> str:
        if (
            token in allowed_signature_tokens
            or token in MEANINGFUL_COPY_SYMBOL_TOKENS
        ):
            return token
        signature_matches = sorted(
            expected
            for expected in allowed_signature_tokens
            if matches_padded_token(token, expected)
        )
        meaningful_matches = sorted(
            expected
            for expected in MEANINGFUL_COPY_SYMBOL_TOKENS
            if matches_padded_token(token, expected)
        )
        if len(meaningful_matches) == 1:
            return meaningful_matches[0]
        if len(meaningful_matches) > 1:
            if (
                allow_ambiguous_signature_match
                and len(signature_matches) == 1
            ):
                return signature_matches[0]
            return token
        if len(signature_matches) == 1:
            return signature_matches[0]
        if len(signature_matches) > 1:
            return token
        return token.translate(COPY_SYMBOL_TRANSLATION)

    def aligned_fused_token(token: str) -> str | None:
        if len(token) > MAX_COPY_TOKEN_LENGTH:
            return None
        plain = token.translate(COPY_SYMBOL_TRANSLATION)
        if plain in signature_plain_tokens:
            return None
        signature_offset = signature_plain_compact.find(plain)
        if signature_offset >= 0 and (
            signature_plain_compact.find(plain, signature_offset + 1) < 0
        ):
            observed_offset = 0
            matched_length = len(plain)
        else:
            observed_offset = plain.find(signature_plain_compact)
            if (
                observed_offset < 0
                or plain.find(
                    signature_plain_compact,
                    observed_offset + 1,
                )
                >= 0
            ):
                return None
            signature_offset = 0
            matched_length = len(signature_plain_compact)

        normalized: list[str] = []
        plain_index = -1
        index = 0
        while index < len(token):
            character = token[index]
            if character not in COPY_ASCII_SYMBOLS:
                plain_index += 1
                normalized.append(character)
                index += 1
                continue
            end = index + 1
            while (
                end < len(token)
                and token[end] in COPY_ASCII_SYMBOLS
            ):
                end += 1
            symbols = token[index:end]
            relative_position = plain_index - observed_offset
            signature_position = (
                signature_offset + relative_position
                if 0 <= relative_position < matched_length
                else -1
            )
            expected = signature_symbol_at.get(signature_position)
            if expected is not None:
                base = expected.translate(COPY_SYMBOL_TRANSLATION)
                candidate = normalized_token_for(
                    f"{base}{symbols}",
                    frozenset({expected}),
                )
                normalized.append(
                    candidate[len(base) :]
                    if candidate.startswith(base)
                    else symbols
                )
            elif signature_position in signature_plain_c_at:
                candidate = normalized_token_for(
                    f"c{symbols}",
                    frozenset(),
                )
                normalized.append(
                    candidate[1:]
                    if candidate.startswith("c")
                    else symbols
                )
            index = end
        return "".join(normalized)

    tokens_list: list[str] = []
    for token in coalesce_symbol_continuations(forms.tokens):
        normalized = aligned_fused_token(token)
        if normalized is None:
            normalized = normalized_token_for(
                token,
                signature_symbol_tokens,
            )
        if normalized:
            tokens_list.append(normalized)
    tokens = tuple(tokens_list)
    return tokens, " ".join(tokens), "".join(tokens)


def has_symbolic_copy_substitution(
    observed_tokens: tuple[str, ...],
    signature_tokens: tuple[str, ...],
) -> bool:
    """Return whether flexible matching changes a symbolic token's identity."""

    def grouped(tokens: tuple[str, ...]) -> dict[str, Counter[str]]:
        result: dict[str, Counter[str]] = {}
        for token in tokens:
            plain = token.translate(COPY_SYMBOL_TRANSLATION)
            if plain:
                result.setdefault(plain, Counter())[token] += 1
        return result

    observed = grouped(observed_tokens)
    signature = grouped(signature_tokens)
    for plain, expected_counts in signature.items():
        actual_counts = observed.get(plain, Counter())
        has_deficit = any(
            count > actual_counts[identity]
            for identity, count in expected_counts.items()
        )
        has_symbolic_surplus = any(
            any(symbol in identity for symbol in COPY_ASCII_SYMBOLS)
            and count > expected_counts[identity]
            for identity, count in actual_counts.items()
        )
        if has_deficit and has_symbolic_surplus:
            return True
    return False


def normalized_routing_term(value: str) -> str:
    """Normalize phrases with a conservative, version-stable token policy."""

    folded = nfkc_casefold(value)
    identifier_aware: list[str] = []
    for character in folded:
        if (
            character in STABLE_WHITESPACE
            or character in UNICODE_PROSE_SEPARATORS
        ):
            identifier_aware.append(" ")
        elif character.isascii():
            identifier_aware.append(
                character
                if character.isalnum() or character in ROUTING_ASCII_SYMBOLS
                else " "
            )
        else:
            # Preserve every non-ASCII attachment. This deliberately favors a
            # missed match over inventing a token boundary from Unicode data
            # that differs among supported Python versions.
            identifier_aware.append(character)
    return " ".join("".join(identifier_aware).split())


def contains_routing_term(prompt: str, routing_term: str) -> bool:
    """Match a normalized routing phrase at token boundaries."""

    normalized_prompt = normalized_routing_term(prompt)
    normalized_term = normalized_routing_term(routing_term)
    return bool(normalized_term) and (
        f" {normalized_term} " in f" {normalized_prompt} "
    )


def trigger_copy_kind(prompt: str, trigger: str) -> str | None:
    """Classify fixtures copied from curated trigger text."""

    if (
        len(prompt) > MAX_COPY_TEXT_LENGTH
        or len(trigger) > MAX_COPY_TEXT_LENGTH
    ):
        return None
    prompt_forms = copy_forms(prompt)
    trigger_forms = copy_forms(trigger)
    if (
        not copy_forms_within_limits(prompt_forms)
        or not copy_forms_within_limits(trigger_forms)
    ):
        return None
    prompt_flexible_tokens, prompt_flexible_spaced, prompt_flexible_compact = (
        signature_aware_copy_text(
            prompt_forms,
            trigger_forms,
            allow_ambiguous_signature_match=True,
        )
    )
    trigger_flexible_tokens, trigger_flexible_spaced, trigger_flexible_compact = (
        signature_aware_copy_text(
            trigger_forms,
            trigger_forms,
            allow_ambiguous_signature_match=True,
        )
    )
    if (
        prompt_forms.spaced == trigger_forms.spaced
        or prompt_forms.compact == trigger_forms.compact
        or prompt_flexible_spaced == trigger_flexible_spaced
        or prompt_flexible_compact == trigger_flexible_compact
    ):
        return "normalized exact"
    compact_embedded = (
        len(trigger_forms.compact) >= MIN_COMPACT_TRIGGER_LENGTH
        and trigger_forms.compact in prompt_forms.compact
    )
    flexible_embedded = (
        len(trigger_flexible_compact) >= MIN_COMPACT_TRIGGER_LENGTH
        and trigger_flexible_compact in prompt_flexible_compact
    )
    if (
        trigger_forms.spaced
        and trigger_forms.spaced in prompt_forms.spaced
    ) or compact_embedded or flexible_embedded:
        return "embedded normalized"
    symbol_conflict = has_symbolic_copy_substitution(
        prompt_flexible_tokens,
        trigger_flexible_tokens,
    )
    raw_near_verbatim = any(
        bounded_copy_near_verbatim(
            left,
            right,
        )
        for left, right in (
            (
                prompt_forms.spaced,
                trigger_forms.spaced,
            ),
            (
                prompt_forms.compact,
                trigger_forms.compact,
            ),
        )
    )
    flexible_near_verbatim = any(
        bounded_copy_near_verbatim(
            left,
            right,
        )
        for left, right in (
            (
                prompt_flexible_spaced,
                trigger_flexible_spaced,
            ),
            (
                prompt_flexible_compact,
                trigger_flexible_compact,
            ),
        )
    )
    if raw_near_verbatim or (
        not symbol_conflict
        and flexible_near_verbatim
    ):
        return "near-verbatim"
    if min(
        len(prompt_flexible_tokens),
        len(trigger_flexible_tokens),
    ) < 8:
        return None
    raw_prompt_tokens = Counter(prompt_forms.tokens)
    raw_trigger_tokens = Counter(trigger_forms.tokens)
    flexible_prompt_tokens = Counter(prompt_flexible_tokens)
    flexible_trigger_tokens = Counter(trigger_flexible_tokens)
    raw_coverage = sum(
        (raw_prompt_tokens & raw_trigger_tokens).values()
    ) / sum(raw_trigger_tokens.values())
    flexible_coverage = sum(
        (flexible_prompt_tokens & flexible_trigger_tokens).values()
    ) / sum(flexible_trigger_tokens.values())
    if (
        raw_coverage >= COPY_SIMILARITY_THRESHOLD
    ) or (
        not symbol_conflict
        and flexible_coverage >= COPY_SIMILARITY_THRESHOLD
    ):
        return "high trigger-token coverage"
    return None


def copy_equivalent(value: str, signature: str) -> bool:
    """Return whether text differs from a signature only by obfuscation."""

    value_forms = copy_forms(value)
    signature_forms = copy_forms(signature)
    _, value_flexible_spaced, value_flexible_compact = (
        signature_aware_copy_text(value_forms, signature_forms)
    )
    _, signature_flexible_spaced, signature_flexible_compact = (
        signature_aware_copy_text(signature_forms, signature_forms)
    )
    return (
        value_forms.spaced == signature_forms.spaced
        or value_forms.compact == signature_forms.compact
        or value_flexible_spaced == signature_flexible_spaced
        or value_flexible_compact == signature_flexible_compact
    )


def copy_startswith(value: str, signature: str) -> bool:
    """Return whether text starts with an obfuscated signature."""

    value_forms = copy_forms(value)
    signature_forms = copy_forms(signature)
    _, value_flexible_spaced, value_flexible_compact = (
        signature_aware_copy_text(value_forms, signature_forms)
    )
    _, signature_flexible_spaced, signature_flexible_compact = (
        signature_aware_copy_text(signature_forms, signature_forms)
    )
    return (
        value_forms.spaced.startswith(signature_forms.spaced)
        or value_forms.compact.startswith(signature_forms.compact)
        or value_flexible_spaced.startswith(signature_flexible_spaced)
        or value_flexible_compact.startswith(signature_flexible_compact)
    )


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class StyleMarkdownLine:
    """One parsed Markdown block with deterministic prose exposed."""

    number: int
    raw: str
    visible: str
    accessible: str = ""
    heading_text: str = ""
    prose_fragments: tuple[tuple[int, str], ...] = ()
    ampersand_fragments: tuple[tuple[int, str], ...] = ()
    links: tuple[StyleMarkdownLink, ...] = ()
    missing_html_alt_offsets: tuple[int, ...] = ()
    unsupported_html_heading_offsets: tuple[int, ...] = ()
    heading_level: int | None = None
    heading_has_content: bool = False
    protected_heading_prefix: bool = False
    block_kind: str | None = None


@dataclass(frozen=True)
class StyleMarkdownLink:
    """One rendered link label and its source-line offset."""

    text: str
    line_offset: int


@dataclass(frozen=True)
class StyleAdvisoryReport:
    """A bounded advisory display with its untruncated candidate count."""

    items: tuple[str, ...]
    total_count: int


@dataclass
class _StyleHTMLAnchor:
    parts: list[str]
    opening_line_offset: int
    text_line_offset: int | None = None


@dataclass
class _StyleMarkdownAnchor:
    parts: list[str]
    suppressed: bool
    opening_line_offset: int
    text_line_offset: int | None = None


class _StyleLiteralStack:
    """Track nested literal HTML tags with amortized constant-time closes."""

    def __init__(self) -> None:
        self._items: list[tuple[str, int, int]] = []
        self._last: dict[str, int] = {}

    def __bool__(self) -> bool:
        return bool(self._items)

    def contains(self, tag: str) -> bool:
        return tag in self._last

    def push(self, tag: str, source_offset: int = 0) -> None:
        previous = self._last.get(tag, -1)
        self._items.append((tag, source_offset, previous))
        self._last[tag] = len(self._items) - 1

    def close(self, tag: str) -> int | None:
        matching = self._last.get(tag)
        if matching is None:
            return None
        source_offset = self._items[matching][1]
        for index in range(len(self._items) - 1, matching - 1, -1):
            popped_tag, _, previous = self._items[index]
            if self._last.get(popped_tag) != index:
                continue
            if previous >= 0:
                self._last[popped_tag] = previous
            else:
                self._last.pop(popped_tag, None)
        del self._items[matching:]
        return source_offset

    def clear(self) -> None:
        self._items.clear()
        self._last.clear()

    def openings(self) -> tuple[tuple[str, int], ...]:
        return tuple((tag, source_offset) for tag, source_offset, _ in self._items)


def load_documentation_style_profile() -> dict:
    """Load the reviewed documentation-style profile."""

    return read_yaml(DOCUMENTATION_STYLE_PATH)


def lint_documentation_style_profile(profile: object) -> list[str]:
    """Validate the dated, project-scoped documentation-style profile."""

    label = "config/documentation-style.yaml"
    if not isinstance(profile, dict):
        return [f"{label}: profile must be a mapping"]
    errors: list[str] = []
    if profile.get("schema_version") != 1:
        errors.append(f"{label}: schema_version must be 1")

    authority = profile.get("authority")
    if not isinstance(authority, dict):
        errors.append(f"{label}: authority must be a mapping")
    else:
        if authority.get("url") != STYLE_PROFILE_URL:
            errors.append(
                f"{label}: authority.url must be {STYLE_PROFILE_URL}"
            )
        reviewed_on = authority.get("reviewed_on")
        valid_review_date = False
        if isinstance(reviewed_on, str) and STYLE_PROFILE_DATE_RE.fullmatch(
            reviewed_on
        ):
            try:
                date.fromisoformat(reviewed_on)
                valid_review_date = True
            except ValueError:
                pass
        if not valid_review_date:
            errors.append(f"{label}: authority.reviewed_on must be YYYY-MM-DD")
        if not is_nonempty_string(authority.get("name")):
            errors.append(f"{label}: authority.name must be nonempty")

    precedence = profile.get("precedence")
    if (
        not isinstance(precedence, dict)
        or precedence.get("project_first") is not True
        or not is_nonempty_string(precedence.get("statement"))
    ):
        errors.append(
            f"{label}: precedence must state that project requirements come first"
        )

    rules = profile.get("rules")
    if not isinstance(rules, dict):
        errors.append(f"{label}: rules must be a mapping")
    else:
        for group in ("commonmark_project", "google_hard", "google_advisory"):
            values = rules.get(group)
            if (
                not isinstance(values, list)
                or not values
                or any(not is_nonempty_string(value) for value in values)
            ):
                errors.append(f"{label}: rules.{group} must be a nonempty string list")

    protected = profile.get("protected_contexts")
    if not isinstance(protected, list) or any(
        not is_nonempty_string(value) for value in protected
    ):
        errors.append(f"{label}: protected_contexts must be a string list")
    else:
        missing = sorted(STYLE_REQUIRED_PROTECTED_CONTEXTS - set(protected))
        if missing:
            errors.append(
                f"{label}: missing protected contexts: {', '.join(missing)}"
            )
        if len(protected) != len(set(protected)):
            errors.append(f"{label}: protected_contexts contains duplicates")

    exceptions = profile.get("exceptions")
    if not isinstance(exceptions, dict):
        errors.append(f"{label}: exceptions must be a mapping")
    else:
        for field in ("proper_names", "lowercase_title_prefixes"):
            values = exceptions.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not is_nonempty_string(value) for value in values)
                or len(values) != len(set(values))
            ):
                errors.append(
                    f"{label}: exceptions.{field} must contain unique strings"
                )
        prefixes = exceptions.get("lowercase_title_prefixes", [])
        if isinstance(prefixes, list) and any(
            isinstance(value, str) and value != value.casefold()
            for value in prefixes
        ):
            errors.append(
                f"{label}: lowercase title prefixes must be lowercase"
            )
        technical = exceptions.get("technical")
        if not isinstance(technical, list) or not technical or any(
            not isinstance(item, dict)
            or not is_nonempty_string(item.get("id"))
            or not is_nonempty_string(item.get("rationale"))
            for item in technical if isinstance(technical, list)
        ):
            errors.append(
                f"{label}: exceptions.technical must contain id/rationale mappings"
            )

    hard_checks = profile.get("hard_checks")
    if not isinstance(hard_checks, dict):
        errors.append(f"{label}: hard_checks must be a mapping")
    else:
        for field in ("vague_link_text", "discouraged_phrases"):
            values = hard_checks.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not is_nonempty_string(value) for value in values)
                or len(values) != len(set(values))
            ):
                errors.append(
                    f"{label}: hard_checks.{field} must contain unique strings"
                )

    documents = profile.get("documents")
    if not isinstance(documents, dict):
        errors.append(f"{label}: documents must be a mapping")
    else:
        sources = documents.get("source")
        if not isinstance(sources, list) or not sources or any(
            not is_nonempty_string(value)
            or Path(value).is_absolute()
            or ".." in Path(value).parts
            for value in sources if isinstance(sources, list)
        ):
            errors.append(f"{label}: documents.source must contain safe relative paths")
        elif len(sources) != len(set(sources)):
            errors.append(f"{label}: documents.source contains duplicates")
        generated_glob = documents.get("generated_glob")
        if (
            not is_nonempty_string(generated_glob)
            or Path(generated_glob).is_absolute()
            or ".." in Path(generated_glob).parts
            or not str(generated_glob).startswith("build/generated/")
        ):
            errors.append(
                f"{label}: documents.generated_glob must be a safe build/generated glob"
            )

    advisory = profile.get("advisory")
    if not isinstance(advisory, dict):
        errors.append(f"{label}: advisory must be a mapping")
    else:
        for field in ("max_sentence_words", "max_report_items"):
            value = advisory.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(f"{label}: advisory.{field} must be a positive integer")
    return errors


def style_profile_names(profile: dict) -> set[str]:
    """Return reviewed proper names from a valid or partial profile."""

    values = profile.get("exceptions", {}).get("proper_names", [])
    if not isinstance(values, list):
        return set(DEFAULT_STYLE_ALLOWED_CAPITALIZED_WORDS)
    return {
        value for value in values if isinstance(value, str) and value.strip()
    }


def style_profile_lowercase_prefixes(profile: dict) -> set[str]:
    """Return reviewed lowercase technical prefixes from a profile."""

    values = profile.get("exceptions", {}).get(
        "lowercase_title_prefixes",
        [],
    )
    if not isinstance(values, list):
        return set()
    return {
        value.casefold()
        for value in values
        if isinstance(value, str) and value.strip()
    }


def sentence_case_error(
    value: str,
    *,
    proper_names: set[str] | None = None,
    lowercase_initials: set[str] | None = None,
) -> str | None:
    """Return the first word with unexpected heading-style casing."""

    violation = sentence_case_violation(
        value,
        proper_names=proper_names,
        lowercase_initials=lowercase_initials,
    )
    return violation[0] if violation is not None else None


def sentence_case_violation(
    value: str,
    *,
    proper_names: set[str] | None = None,
    lowercase_initials: set[str] | None = None,
) -> tuple[str, int] | None:
    """Return the first unexpected word and its character offset."""

    allowed_names = (
        DEFAULT_STYLE_ALLOWED_CAPITALIZED_WORDS
        if proper_names is None
        else proper_names
    )
    allowed_initials = lowercase_initials or set()
    normalized_value = re.sub(r"[\u2010-\u2015\u2212]", "-", value)
    proper_spans: list[tuple[int, int]] = []
    for name in allowed_names:
        normalized_name = re.sub(r"[\u2010-\u2015\u2212]", "-", name)
        if not normalized_name:
            continue
        for proper_match in re.finditer(
            rf"(?<![\w+.-]){re.escape(normalized_name)}"
            rf"(?![\w+.-])",
            normalized_value,
        ):
            proper_spans.append(proper_match.span())

    merged_proper_spans: list[list[int]] = []
    for start, end in sorted(proper_spans):
        if merged_proper_spans and start <= merged_proper_spans[-1][1]:
            merged_proper_spans[-1][1] = max(
                merged_proper_spans[-1][1],
                end,
            )
        else:
            merged_proper_spans.append([start, end])

    previous_end = 0
    proper_span_index = 0
    for index, match in enumerate(STYLE_WORD_RE.finditer(normalized_value)):
        word = match.group(0)
        while (
            proper_span_index < len(merged_proper_spans)
            and merged_proper_spans[proper_span_index][1] <= match.start()
        ):
            proper_span_index += 1
        reviewed_name = (
            proper_span_index < len(merged_proper_spans)
            and merged_proper_spans[proper_span_index][0] <= match.start()
            and match.end() <= merged_proper_spans[proper_span_index][1]
        )
        if not reviewed_name and any(
            segment and segment[0].isupper()
            for segment in word.split("-")[1:]
        ):
            return word, match.start()
        initial_position = index == 0 or ":" in normalized_value[
            previous_end : match.start()
        ]
        if not reviewed_name and initial_position:
            if word[0].islower() and word.casefold() not in allowed_initials:
                return word, match.start()
            letters = [character for character in word if character.isalpha()]
            if len(letters) > 1 and all(character.isupper() for character in letters):
                return word, match.start()
        elif not reviewed_name and word[0].isupper() and not any(
            character.isdigit() for character in word
        ):
            return word, match.start()
        previous_end = match.end()
    return None


def _jinja_source_variants(
    value: str,
) -> tuple[str, str, str]:
    """Return visible/accessibility variants from literal Jinja source."""

    visible: list[str] = []
    accessible: list[str] = []
    prose: list[str] = []
    position = 0
    while position < len(value):
        opener_match = STYLE_JINJA_OPENER_RE.search(value, position)
        if opener_match is None:
            visible.append(value[position:])
            accessible.append(value[position:])
            prose.append(value[position:])
            break
        opening = opener_match.start()
        visible.append(value[position:opening])
        accessible.append(value[position:opening])
        prose.append(value[position:opening])
        opener_data = next(
            (
                (opener, closer, token_type)
                for opener, closer, token_type in STYLE_JINJA_PAIRS
                if value.startswith(opener, opening)
            ),
            None,
        )
        if opener_data is None:
            visible.append(value[opening])
            accessible.append(value[opening])
            prose.append(value[opening])
            position = opening + 1
            continue
        opener, closer, token_type = opener_data
        cursor = opening + len(opener)
        quote = ""
        while cursor < len(value):
            character = value[cursor]
            if closer != "#}" and quote:
                if character == "\\":
                    cursor += 2
                    continue
                if character == quote:
                    quote = ""
                cursor += 1
                continue
            if closer != "#}" and character in {'"', "'"}:
                quote = character
                cursor += 1
                continue
            if value.startswith(closer, cursor):
                break
            cursor += 1
        if cursor >= len(value):
            visible.append(value[opening:])
            accessible.append(value[opening:])
            prose.append(value[opening:])
            break
        end = cursor + len(closer)
        source = value[opening:end]
        line_mask = "".join(
            character if character in "\r\n" else " "
            for character in source
        )
        visible.append(line_mask)
        if token_type == "style_jinja_value":
            accessible.append(
                " dynamic-value "
                + "".join(
                    character
                    for character in source
                    if character in "\r\n"
                )
            )
            prose.append(
                STYLE_PROTECTED_BOUNDARY
                + "".join(
                    character
                    for character in source
                    if character in "\r\n"
                )
            )
        else:
            accessible.append(line_mask)
            prose.append(line_mask)
        position = end
    return "".join(visible), "".join(accessible), "".join(prose)


def _mask_jinja(value: str) -> str:
    """Mask literal Jinja expressions and controls in visible source."""

    return _jinja_source_variants(value)[0]


def _jinja_accessible_text(value: str) -> str:
    """Retain value placeholders but discard Jinja control and comments."""

    return _jinja_source_variants(value)[1]


def _jinja_prose_text(value: str) -> str:
    """Replace Jinja values with a boundary and hide controls."""

    return _jinja_source_variants(value)[2]


def _single_source_line(value: str) -> str:
    """Keep entity-decoded line controls on their original source line."""

    return re.sub(r"[\r\n]+", " ", value)


def _has_visible_text(value: str) -> bool:
    """Return whether rendered text contains a visible non-space character."""

    return any(
        not character.isspace()
        and unicodedata.category(character)[0] not in {"C", "M"}
        for character in value
    )


def _mask_frontmatter(text: str, *, mask_entities: bool) -> str:
    """Mask non-prose source spans while preserving Markdown line numbers."""

    lines = text.splitlines(keepends=True)
    if lines and lines[0].lstrip("\ufeff").rstrip("\r\n") == "---":
        closing_index = len(lines) - 1
        for index, line in enumerate(lines[1:], start=1):
            if line.rstrip("\r\n") in {"---", "..."}:
                closing_index = index
                break
        for index in range(closing_index + 1):
            lines[index] = "".join(
                character if character in "\r\n" else " "
                for character in lines[index]
            )
    masked = "".join(lines)
    if not mask_entities:
        return masked

    def replace_valid_entity(match: re.Match[str]) -> str:
        name = match.group("named")
        if name is not None and f"{name};" not in HTML5_ENTITIES:
            return match.group(0)
        return "x" * len(match.group(0))

    return STYLE_ENTITY_RE.sub(replace_valid_entity, masked)


def _html_attribute_source(
    start_tag: str,
    target: str,
) -> tuple[str | None, int]:
    """Return a raw HTML attribute value and its physical line offset."""

    cursor = 1
    limit = len(start_tag)
    while cursor < limit and start_tag[cursor].isspace():
        cursor += 1
    while (
        cursor < limit
        and not start_tag[cursor].isspace()
        and start_tag[cursor] not in "/>"
    ):
        cursor += 1
    while cursor < limit:
        while cursor < limit and start_tag[cursor].isspace():
            cursor += 1
        if cursor >= limit or start_tag[cursor] in "/>":
            break
        name_start = cursor
        while (
            cursor < limit
            and not start_tag[cursor].isspace()
            and start_tag[cursor] not in "=/>"
        ):
            cursor += 1
        name = start_tag[name_start:cursor].casefold()
        while cursor < limit and start_tag[cursor].isspace():
            cursor += 1
        value_start = name_start
        value_end = cursor
        has_value = False
        if cursor < limit and start_tag[cursor] == "=":
            has_value = True
            cursor += 1
            while cursor < limit and start_tag[cursor].isspace():
                cursor += 1
            value_start = cursor
            if cursor < limit and start_tag[cursor] in {'"', "'"}:
                quote = start_tag[cursor]
                cursor += 1
                value_start = cursor
                while cursor < limit and start_tag[cursor] != quote:
                    cursor += 1
                value_end = cursor
                cursor += cursor < limit
            else:
                while (
                    cursor < limit
                    and not start_tag[cursor].isspace()
                    and start_tag[cursor] != ">"
                ):
                    cursor += 1
                value_end = cursor
        if name == target.casefold():
            return (
                start_tag[value_start:value_end] if has_value else "",
                start_tag[:value_start].count("\n"),
            )
    return None, 0


def _decode_valid_html_references(value: str) -> str:
    """Decode exact CommonMark HTML references without legacy-prefix recovery."""

    def replace(match: re.Match[str]) -> str:
        name = match.group("named")
        if name is not None and f"{name};" not in HTML5_ENTITIES:
            return match.group(0)
        return unescape(match.group(0))

    return STYLE_ENTITY_RE.sub(replace, value)


class _StyleHTMLScanner(HTMLParser):
    """Expose HTML text while protecting tags, attributes, and literal code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.visible: list[str] = []
        self.accessible: list[str] = []
        self.accessible_fragments: list[tuple[int, str]] = []
        self.prose_fragments: list[tuple[int, str]] = []
        self.links: list[StyleMarkdownLink] = []
        self.missing_alt_offsets: list[int] = []
        self.html_heading_offsets: list[int] = []
        self._literal_tags = _StyleLiteralStack()
        self._anchors: list[_StyleHTMLAnchor] = []
        self._suppress_data = False
        self._jinja_closer = ""
        self._jinja_type = ""
        self._jinja_quote = ""
        self._jinja_buffer_parts: list[str] = []
        self._jinja_opening_line = 0
        self._jinja_literal_tags = _StyleLiteralStack()

    def parse_html_declaration(self, index: int) -> int:
        """Treat malformed SGML declarations as text instead of crashing."""

        try:
            return super().parse_html_declaration(index)
        except AssertionError:
            self.handle_data(self.rawdata[index : index + 1])
            return index + 1

    @property
    def in_literal(self) -> bool:
        return bool(self._literal_tags)

    @property
    def in_hidden_literal(self) -> bool:
        return self._literal_tags.contains("script") or self._literal_tags.contains(
            "style"
        )

    @property
    def suppresses_nested_markdown(self) -> bool:
        return self.in_hidden_literal or self._literal_tags.contains("pre")

    def add_accessible(self, value: str, *, line_offset: int = 0) -> None:
        if self.in_hidden_literal:
            return
        self.accessible.append(value)
        first_visible = re.search(r"\S", value)
        leading_lines = (
            value[: first_visible.start()].count("\n")
            if first_visible is not None
            else 0
        )
        accessible_line = (
            self.getpos()[0] - 1 + line_offset + leading_lines
        )
        if value:
            self.accessible_fragments.append((accessible_line, value))
        for anchor in self._anchors:
            anchor.parts.append(value)
            if anchor.text_line_offset is None and value.strip():
                anchor.text_line_offset = accessible_line

    def add_text(
        self,
        value: str,
        *,
        accessible_value: str | None = None,
        line_offset: int = 0,
    ) -> None:
        self.add_accessible(
            value if accessible_value is None else accessible_value,
            line_offset=line_offset,
        )
        if not self.in_literal:
            self.visible.append(value)
            base_offset = self.getpos()[0] - 1 + line_offset
            self.prose_fragments.extend(
                (base_offset + offset, part)
                for offset, part in enumerate(value.split("\n"))
                if part
            )
        elif not self.in_hidden_literal and value:
            self.prose_fragments.append(
                (
                    self.getpos()[0] - 1 + line_offset,
                    STYLE_PROTECTED_BOUNDARY,
                )
            )

    def add_protected_boundary(self, *, line_offset: int = 0) -> None:
        """Keep reader-visible protected text from collapsing prose phrases."""

        if not self.in_hidden_literal:
            self.prose_fragments.append(
                (
                    self.getpos()[0] - 1 + line_offset,
                    STYLE_PROTECTED_BOUNDARY,
                )
            )

    def _finish_anchor(self) -> None:
        """Record the active HTML anchor using its first visible label line."""

        if not self._anchors:
            return
        anchor = self._anchors.pop()
        self.links.append(
            StyleMarkdownLink(
                "".join(anchor.parts),
                anchor.text_line_offset
                if anchor.text_line_offset is not None
                else anchor.opening_line_offset,
            )
        )

    def _append_jinja_protected_source(self, value: str) -> None:
        """Retain protected HTML source without interpreting Jinja closers in it."""

        self._jinja_buffer_parts.append(value)

    def _consume_jinja_source(
        self,
        value: str,
        *,
        emit_plain: bool,
    ) -> None:
        """Consume source text while hiding only literal Jinja controls."""

        continued = bool(self._jinja_closer)
        if continued:
            self._jinja_buffer_parts.append(value)
        line_offsets = _source_line_offsets(value)
        pending_opening: int | None = None
        position = 0
        while position < len(value):
            if self._jinja_closer:
                while position < len(value):
                    character = value[position]
                    if self._jinja_closer != "#}" and self._jinja_quote:
                        if character == "\\":
                            position += 2
                            continue
                        if character == self._jinja_quote:
                            self._jinja_quote = ""
                        position += 1
                        continue
                    if (
                        self._jinja_closer != "#}"
                        and character in {'"', "'"}
                    ):
                        self._jinja_quote = character
                        position += 1
                        continue
                    if value.startswith(self._jinja_closer, position):
                        position += len(self._jinja_closer)
                        self._jinja_closer = ""
                        self._jinja_type = ""
                        self._jinja_quote = ""
                        self._jinja_buffer_parts.clear()
                        self._jinja_literal_tags.clear()
                        pending_opening = None
                        break
                    position += 1
                if self._jinja_closer:
                    if pending_opening is not None:
                        self._jinja_buffer_parts.append(value[pending_opening:])
                    return
                if not emit_plain:
                    return
                continue

            opener_match = STYLE_JINJA_OPENER_RE.search(value, position)
            if opener_match is None:
                if emit_plain:
                    self.add_text(
                        value[position:],
                        line_offset=line_offsets[position],
                    )
                return
            opening = opener_match.start()
            if emit_plain and opening > position:
                self.add_text(
                    value[position:opening],
                    line_offset=line_offsets[position],
                )
            opener_data = next(
                (
                    (opener, closer, token_type)
                    for opener, closer, token_type in STYLE_JINJA_PAIRS
                    if value.startswith(opener, opening)
                ),
                None,
            )
            if opener_data is None:
                position = opening + 1
                continue
            opener, self._jinja_closer, self._jinja_type = opener_data
            self._jinja_buffer_parts.clear()
            pending_opening = opening
            self._jinja_opening_line = (
                self.getpos()[0]
                - 1
                + line_offsets[opening]
            )
            if self._jinja_type == "style_jinja_value":
                self.add_accessible(
                    "dynamic-value",
                    line_offset=line_offsets[opening],
                )
                self.add_protected_boundary(
                    line_offset=line_offsets[opening],
                )
            position = opening + len(opener)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self._jinja_closer:
            normalized = tag.casefold()
            if normalized in STYLE_HTML_LITERAL_TAGS:
                self._jinja_literal_tags.push(normalized)
            self._append_jinja_protected_source(
                self.get_starttag_text() or ""
            )
            return
        normalized = tag.casefold()
        if normalized in STYLE_HTML_LITERAL_TAGS:
            self._literal_tags.push(normalized)
            return
        if self.in_hidden_literal:
            return
        if normalized in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.html_heading_offsets.append(self.getpos()[0] - 1)
        if not self.in_literal and normalized in STYLE_HTML_BREAK_TAGS:
            self.add_text(" ")
        if normalized == "a" and any(
            name.casefold() == "href" for name, _ in attrs
        ):
            if self._anchors:
                self._finish_anchor()
            self._anchors.append(
                _StyleHTMLAnchor([], self.getpos()[0] - 1)
            )
        elif normalized == "img":
            alt_values = [
                value
                for name, value in attrs
                if name.casefold() == "alt"
            ]
            if not alt_values:
                self.missing_alt_offsets.append(self.getpos()[0] - 1)
            else:
                source_alt, relative_offset = _html_attribute_source(
                    self.get_starttag_text() or "",
                    "alt",
                )
                source_offset = (
                    self.getpos()[0]
                    - 1
                    + relative_offset
                )
                raw_alt = (
                    source_alt
                    if source_alt is not None
                    else (alt_values[0] or "")
                )
                accessible_source = _jinja_accessible_text(raw_alt)
                accessible_lines = [
                    _single_source_line(
                        _decode_valid_html_references(source_line)
                    )
                    for source_line in accessible_source.splitlines()
                ] or [""]
                accessible = " ".join(accessible_lines)
                first_accessible_line = next(
                    (
                        offset
                        for offset, part in enumerate(accessible_lines)
                        if _has_visible_text(part)
                    ),
                    0,
                )
                source_lines = _mask_jinja(raw_alt).splitlines() or [""]
                visible_lines = [
                    _single_source_line(
                        _decode_valid_html_references(source_line)
                    )
                    for source_line in source_lines
                ]
                prose_lines = [
                    _single_source_line(
                        _decode_valid_html_references(source_line)
                    )
                    for source_line in (
                        _jinja_prose_text(raw_alt).splitlines() or [""]
                    )
                ]
                visible = " ".join(visible_lines)
                self.add_accessible(
                    accessible,
                    line_offset=relative_offset + first_accessible_line,
                )
                if not self.in_literal:
                    self.visible.append(visible)
                self.prose_fragments.extend(
                    (source_offset + offset, part)
                    for offset, part in enumerate(prose_lines)
                    if part
                )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self._jinja_closer:
            self._append_jinja_protected_source(
                self.get_starttag_text() or ""
            )
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._jinja_closer:
            self._append_jinja_protected_source(f"</{tag}>")
            normalized = tag.casefold()
            self._jinja_literal_tags.close(normalized)
            return
        normalized = tag.casefold()
        if normalized in STYLE_HTML_LITERAL_TAGS:
            self._literal_tags.close(normalized)
            return
        if normalized == "a" and self._anchors and not self.in_hidden_literal:
            self._finish_anchor()
        if self.in_literal:
            return
        if normalized in STYLE_HTML_BREAK_TAGS and normalized != "br":
            self.add_text(" ")

    def handle_data(self, data: str) -> None:
        if self._suppress_data:
            return
        if self._jinja_closer and self._jinja_literal_tags:
            self._append_jinja_protected_source(data)
            return
        if self.in_literal:
            self.add_text(data)
            return
        self._consume_jinja_source(data, emit_plain=True)

    def handle_comment(self, data: str) -> None:
        if self._jinja_closer:
            self._append_jinja_protected_source(f"<!--{data}-->")

    def handle_entityref(self, name: str) -> None:
        raw = f"&{name};"
        if self._jinja_closer:
            self._append_jinja_protected_source(raw)
            return
        self.add_text(
            _single_source_line(unescape(raw))
            if f"{name};" in HTML5_ENTITIES
            else raw
        )

    def handle_charref(self, name: str) -> None:
        raw = f"&#{name};"
        if self._jinja_closer:
            self._append_jinja_protected_source(raw)
            return
        self.add_text(
            _single_source_line(unescape(raw))
            if STYLE_ENTITY_RE.fullmatch(raw)
            else raw
        )

    def advance_lines(self, count: int) -> None:
        if count < 1:
            return
        self._suppress_data = True
        try:
            self.feed("\n" * count)
        finally:
            self._suppress_data = False

    def finish(self) -> None:
        if self._jinja_closer:
            buffered = "".join(self._jinja_buffer_parts)
            opening_line = self._jinja_opening_line
            self._jinja_closer = ""
            self._jinja_type = ""
            self._jinja_quote = ""
            self._jinja_buffer_parts.clear()
            self._jinja_literal_tags.clear()
            self.add_text(
                buffered,
                line_offset=opening_line - (self.getpos()[0] - 1),
            )
        while self._anchors:
            self._finish_anchor()


def _source_line_offsets(value: str) -> list[int]:
    """Return the zero-based source line for every character boundary."""

    offsets = [0]
    for character in value:
        offsets.append(offsets[-1] + (1 if character == "\n" else 0))
    return offsets


def _render_image_alt_text(
    children: list[Token] | None,
    *,
    include_dynamic: bool,
) -> str:
    """Render CommonMark image alt text with explicit Jinja semantics."""

    parts: list[str] = []
    for child in children or []:
        if child.type == "text":
            parts.append(child.content)
        elif child.type == "image":
            parts.append(
                _render_image_alt_text(
                    child.children,
                    include_dynamic=include_dynamic,
                )
            )
        elif child.type == "softbreak":
            parts.append("\n")
        elif child.type == "style_jinja_value" and include_dynamic:
            parts.append("dynamic-value")
    return "".join(parts)


def _image_alt_prose_fragments(image: Token) -> tuple[tuple[int, str], ...]:
    """Return CommonMark-visible image alt prose with relative source lines."""

    line_offsets = _source_line_offsets(image.content)
    line_offset = 0
    fragments: list[tuple[int, str]] = []
    for child in image.children or []:
        start = child.meta.get(STYLE_SOURCE_START)
        if isinstance(start, int) and start >= 0:
            line_offset = max(
                line_offset,
                line_offsets[min(start, len(image.content))],
            )
        if child.type == "text":
            value = _single_source_line(child.content)
            if value:
                fragments.append((line_offset, value))
        elif child.type == "softbreak":
            line_offset += 1
        elif child.type == "hardbreak":
            fragments.append((line_offset, STYLE_PROTECTED_BOUNDARY))
            line_offset += 1
        elif child.type == "style_jinja_value":
            fragments.append((line_offset, STYLE_PROTECTED_BOUNDARY))
        elif child.type == "image":
            fragments.extend(
                (line_offset + nested_offset, value)
                for nested_offset, value in _image_alt_prose_fragments(child)
            )
        end = child.meta.get(STYLE_SOURCE_END)
        if isinstance(end, int) and end >= 0:
            line_offset = max(
                line_offset,
                line_offsets[min(end, len(image.content))],
            )
    return tuple(fragments)


def _inline_style_data(
    token: Token,
) -> tuple[
    str,
    tuple[tuple[int, str], ...],
    tuple[StyleMarkdownLink, ...],
    tuple[int, ...],
    tuple[int, ...],
    str,
    str,
]:
    """Extract visible prose and link metadata from one inline token."""

    scanner = _StyleHTMLScanner()
    heading_parts: list[str] = []
    markdown_links: list[_StyleMarkdownAnchor] = []
    resolved_links: list[StyleMarkdownLink] = []
    source_line_offsets = _source_line_offsets(token.content)
    scanner_line_offset = 0

    def align_to_source(child: Token) -> int:
        nonlocal scanner_line_offset
        start = child.meta.get(STYLE_SOURCE_START)
        if not isinstance(start, int) or start < 0:
            return scanner_line_offset
        target = source_line_offsets[min(start, len(token.content))]
        if target > scanner_line_offset:
            scanner.advance_lines(target - scanner_line_offset)
            scanner_line_offset = target
        return target

    def advance_to_source_end(child: Token) -> None:
        nonlocal scanner_line_offset
        end = child.meta.get(STYLE_SOURCE_END)
        if not isinstance(end, int) or end < 0:
            return
        target = source_line_offsets[min(end, len(token.content))]
        if target > scanner_line_offset:
            scanner.advance_lines(target - scanner_line_offset)
            scanner_line_offset = target

    def add_text(value: str) -> None:
        source_line_value = _single_source_line(value)
        scanner.add_text(
            source_line_value,
            accessible_value=source_line_value,
        )
        if not scanner.in_literal:
            heading_parts.append(source_line_value)
        add_link_text(source_line_value, scanner_line_offset)

    def add_link_text(value: str, line_offset: int) -> None:
        if scanner.in_hidden_literal:
            return
        for anchor in markdown_links:
            if anchor.suppressed:
                continue
            anchor.parts.append(value)
            if anchor.text_line_offset is None and _has_visible_text(value):
                first_visible = next(
                    (
                        index
                        for index, character in enumerate(value)
                        if not character.isspace()
                    ),
                    0,
                )
                anchor.text_line_offset = (
                    line_offset + value[:first_visible].count("\n")
                )

    for child in token.children or []:
        child_line_offset = align_to_source(child)
        if child.type == "style_jinja_control":
            pass
        elif child.type == "style_jinja_value":
            scanner.add_accessible("dynamic-value")
            scanner.add_protected_boundary()
            add_link_text("dynamic-value", child_line_offset)
        elif child.type == "text":
            add_text(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            add_text("\n")
            scanner.advance_lines(1)
            scanner_line_offset += 1
        elif child.type == "html_inline":
            fragment_start = len(scanner.accessible_fragments)
            visible_start = len(scanner.visible)
            scanner.feed(child.content)
            added_visible = "".join(scanner.visible[visible_start:])
            if added_visible:
                heading_parts.append(added_visible)
            for line_offset, value in scanner.accessible_fragments[fragment_start:]:
                add_link_text(value, line_offset)
            scanner_line_offset += child.content.count("\n")
        elif child.type == "link_open":
            markdown_links.append(
                _StyleMarkdownAnchor(
                    [],
                    scanner.suppresses_nested_markdown,
                    child_line_offset,
                )
            )
        elif child.type == "link_close" and markdown_links:
            anchor = markdown_links.pop()
            if not anchor.suppressed:
                resolved_links.append(
                    StyleMarkdownLink(
                        "".join(anchor.parts),
                        anchor.text_line_offset
                        if anchor.text_line_offset is not None
                        else anchor.opening_line_offset,
                    )
                )
        elif child.type == "code_inline":
            scanner.add_accessible(child.content)
            scanner.add_protected_boundary()
            start = child.meta.get(STYLE_SOURCE_START)
            end = child.meta.get(STYLE_SOURCE_END)
            code_line_offset = child_line_offset
            if isinstance(start, int) and isinstance(end, int):
                raw_code = token.content[start:end]
                markup_length = len(child.markup)
                inner = raw_code[
                    markup_length : len(raw_code) - markup_length
                    if markup_length
                    else len(raw_code)
                ]
                first_visible = re.search(r"\S", inner)
                if first_visible is not None:
                    code_line_offset += inner[: first_visible.start()].count("\n")
            add_link_text(child.content, code_line_offset)
        elif child.type == "image":
            image_text = _single_source_line(
                _render_image_alt_text(
                    child.children,
                    include_dynamic=True,
                )
            )
            visible_alt = _single_source_line(
                _render_image_alt_text(
                    child.children,
                    include_dynamic=False,
                )
            )
            scanner.add_accessible(image_text)
            image_fragments = _image_alt_prose_fragments(child)
            if not scanner.suppresses_nested_markdown:
                base_offset = scanner.getpos()[0] - 1
                scanner.prose_fragments.extend(
                    (base_offset + offset, part)
                    for offset, part in image_fragments
                )
            if not scanner.in_literal:
                scanner.visible.append(visible_alt)
                heading_parts.append(visible_alt)
            image_line_offset = (
                child_line_offset + image_fragments[0][0]
                if image_fragments
                else child_line_offset
            )
            add_link_text(image_text, image_line_offset)
        if child.type in {
            "code_inline",
            "html_inline",
            "image",
            "link_close",
            "style_jinja_control",
            "style_jinja_value",
        }:
            advance_to_source_end(child)
    scanner.close()
    scanner.finish()
    return (
        "".join(scanner.visible),
        tuple(scanner.prose_fragments),
        tuple(resolved_links + scanner.links),
        tuple(scanner.missing_alt_offsets),
        tuple(scanner.html_heading_offsets),
        "".join(scanner.accessible),
        "".join(heading_parts),
    )


def _html_block_style_data(
    value: str,
) -> tuple[
    str,
    tuple[tuple[int, str], ...],
    tuple[StyleMarkdownLink, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    scanner = _StyleHTMLScanner()
    scanner.feed(value)
    scanner.close()
    scanner.finish()
    return (
        "".join(scanner.visible),
        tuple(scanner.prose_fragments),
        tuple(scanner.links),
        tuple(scanner.missing_alt_offsets),
        tuple(scanner.html_heading_offsets),
    )


def _parsed_markdown_lines(
    text: str,
    *,
    mask_entities: bool,
) -> list[StyleMarkdownLine]:
    """Parse CommonMark into reader-visible prose blocks."""

    parsed_text = _mask_frontmatter(text, mask_entities=mask_entities)
    tokens = STYLE_MARKDOWN_PARSER.parse(parsed_text)
    raw_lines = text.splitlines()
    result: list[StyleMarkdownLine] = []
    list_kinds: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == "bullet_list_open":
            list_kinds.append("bullet")
            continue
        if token.type == "ordered_list_open":
            list_kinds.append("ordered")
            continue
        if token.type in {"bullet_list_close", "ordered_list_close"}:
            if list_kinds:
                list_kinds.pop()
            continue
        if token.type not in {"inline", "html_block"} or token.map is None:
            continue

        start, end = token.map
        raw = "\n".join(raw_lines[start:end])
        previous = tokens[index - 1] if index else None
        heading_level = (
            int(previous.tag[1:])
            if token.type == "inline"
            and previous is not None
            and previous.type == "heading_open"
            else None
        )
        block_kind = (
            "table"
            if previous is not None and previous.type in {"th_open", "td_open"}
            else (list_kinds[-1] if list_kinds else None)
        )
        if token.type == "inline":
            (
                visible,
                prose_fragments,
                links,
                missing_alt_offsets,
                html_heading_offsets,
                accessible,
                heading_text,
            ) = _inline_style_data(token)
        else:
            (
                visible,
                prose_fragments,
                links,
                missing_alt_offsets,
                html_heading_offsets,
            ) = _html_block_style_data(token.content)
            accessible = visible
            heading_text = visible
        raw_heading = token.content.lstrip() if heading_level is not None else ""
        result.append(
            StyleMarkdownLine(
                number=start + 1,
                raw=raw,
                visible=visible,
                accessible=accessible,
                heading_text=heading_text,
                prose_fragments=prose_fragments,
                ampersand_fragments=prose_fragments,
                links=links,
                missing_html_alt_offsets=missing_alt_offsets,
                unsupported_html_heading_offsets=html_heading_offsets,
                heading_level=heading_level,
                heading_has_content=_has_visible_text(accessible),
                protected_heading_prefix=raw_heading.casefold().startswith(
                    ("`", "{{", "{%", "{#", "<code", "<kbd", "<pre")
                ),
                block_kind=block_kind,
            )
        )
    return result


def protected_markdown_lines(text: str) -> list[StyleMarkdownLine]:
    """Parse prose semantically while retaining raw-ampersand evidence."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    semantic = _parsed_markdown_lines(text, mask_entities=False)
    if "&" not in text:
        return semantic
    entity_masked = _parsed_markdown_lines(text, mask_entities=True)
    if len(semantic) != len(entity_masked) or any(
        (line.number, line.heading_level, line.block_kind)
        != (masked.number, masked.heading_level, masked.block_kind)
        for line, masked in zip(semantic, entity_masked, strict=False)
    ):
        return semantic
    return [
        replace(line, ampersand_fragments=masked.prose_fragments)
        for line, masked in zip(semantic, entity_masked, strict=True)
    ]


def _plain_link_text(value: str) -> str:
    """Normalize link text for deterministic vague-text checks."""

    def strip_edge_decorations(candidate: str) -> str:
        left = 0
        right = len(candidate)
        while left < right and (
            candidate[left].isspace()
            or unicodedata.category(candidate[left])[0] in {"P", "S"}
        ):
            left += 1
        while right > left and (
            candidate[right - 1].isspace()
            or unicodedata.category(candidate[right - 1])[0] in {"P", "S"}
        ):
            right -= 1
        return candidate[left:right].strip()

    value = strip_edge_decorations(value)
    value = unicodedata.normalize("NFKC", value)
    value = "".join(
        character
        for character in value
        if unicodedata.category(character)[0] not in {"C", "M"}
    )
    value = re.sub(r"[*_~]", "", value)
    value = " ".join(value.split())
    return strip_edge_decorations(value).casefold()


def _prose_by_line(
    fragments: tuple[tuple[int, str], ...],
) -> tuple[tuple[int, str], ...]:
    """Combine visible fragments that belong to the same source line."""

    grouped: dict[int, list[str]] = {}
    for offset, value in fragments:
        grouped.setdefault(offset, []).append(value)
    return tuple(
        (offset, "".join(parts))
        for offset, parts in sorted(grouped.items())
    )


def _rendered_prose_with_offsets(
    fragments: tuple[tuple[int, str], ...],
) -> tuple[str, list[int]]:
    """Join rendered fragments and retain a source line for each character."""

    rendered: list[str] = []
    source_offsets: list[int] = []
    previous_offset: int | None = None
    for offset, value in fragments:
        if (
            rendered
            and previous_offset is not None
            and offset > previous_offset
            and not rendered[-1].isspace()
            and value
            and not value[0].isspace()
        ):
            rendered.append(" ")
            source_offsets.append(previous_offset)
        rendered.extend(value)
        source_offsets.extend([offset] * len(value))
        previous_offset = offset
    return "".join(rendered), source_offsets


def _phrase_line_offsets(
    fragments: tuple[tuple[int, str], ...],
    phrase: str,
) -> tuple[int, ...]:
    """Find phrase starts in rendered prose while retaining source lines."""

    phrase_pattern = r"\s+".join(
        re.escape(part)
        for part in phrase.split()
    )
    pattern = re.compile(rf"\b{phrase_pattern}\b", re.IGNORECASE)
    combined, source_offsets = _rendered_prose_with_offsets(fragments)
    return tuple(
        sorted(
            {
                source_offsets[match.start()]
                for match in pattern.finditer(combined)
                if match.start() < len(source_offsets)
            }
        )
    )


def _path_label(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _mask_source_spans(value: str, spans: list[tuple[int, int]]) -> str:
    """Mask source spans without changing line or character positions."""

    masked = list(value)
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        bounded_start = max(0, start)
        bounded_end = min(len(masked), end)
        if bounded_end <= bounded_start:
            continue
        if merged and bounded_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bounded_end)
        else:
            merged.append([bounded_start, bounded_end])
    for start, end in merged:
        for position in range(max(0, start), min(len(masked), end)):
            if masked[position] not in "\r\n":
                masked[position] = " "
    return "".join(masked)


def _html_protected_spans(
    value: str,
    *,
    inline_tokens: list[Token] | None = None,
    include_tags: bool = True,
    allow_unclosed: bool = False,
) -> tuple[tuple[int, int], ...]:
    """Return CommonMark HTML syntax and literal-element source spans."""

    if allow_unclosed:
        leading = len(value) - len(value.lstrip(" \t\r\n"))
        for opener, closer in (
            ("<!--", "-->"),
            ("<?", "?>"),
            ("<![CDATA[", "]]>")
        ):
            if value.startswith(opener, leading) and value.find(
                closer,
                leading + len(opener),
            ) < 0:
                return ((leading, len(value)),)
        if re.match(r"<![A-Z]", value[leading:]):
            cursor = leading + 2
            quote = ""
            while cursor < len(value):
                character = value[cursor]
                if quote:
                    if character == quote:
                        quote = ""
                elif character in {'"', "'"}:
                    quote = character
                elif character == ">":
                    break
                cursor += 1
            if cursor >= len(value):
                return ((leading, len(value)),)
    if inline_tokens is None:
        inline_tokens = []
        STYLE_MARKDOWN_PROBE_PARSER.inline.parse(
            value,
            STYLE_MARKDOWN_PROBE_PARSER,
            {},
            inline_tokens,
        )
    tags: list[tuple[int, int, str, bool, bool]] = []
    for token in inline_tokens:
        if token.type != "html_inline":
            continue
        start = token.meta.get(STYLE_SOURCE_START)
        end = token.meta.get(STYLE_SOURCE_END)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        raw = value[start:end]
        name_match = re.match(
            r"<\s*(/)?\s*([A-Za-z][A-Za-z0-9:-]*)",
            raw,
        )
        tags.append(
            (
                start,
                end,
                name_match.group(2).casefold()
                if name_match is not None
                else "",
                bool(name_match and name_match.group(1)),
                name_match is None or raw.rstrip().endswith("/>"),
            )
        )

    spans = (
        [(start, end) for start, end, _, _, _ in tags]
        if include_tags
        else []
    )
    literal_stack = _StyleLiteralStack()
    for start, end, tag, closing, self_closing in tags:
        if tag not in STYLE_HTML_LITERAL_TAGS:
            continue
        if closing:
            literal_start = literal_stack.close(tag)
            if literal_start is not None:
                spans.append((literal_start, end))
        elif not self_closing:
            literal_stack.push(tag, start)
    spans.extend(
        (start, len(value))
        for _, start in literal_stack.openings()
    )
    if allow_unclosed:
        protected_tags = sorted((start, end) for start, end, *_ in tags)
        protected_index = 0
        closing_positions = {
            "<!--": value.rfind("-->"),
            "<?": value.rfind("?>"),
            "<![CDATA[": value.rfind("]]>")
        }
        for opening_match in re.finditer(r"<!--|<\?|<!\[CDATA\[", value):
            opening = opening_match.start()
            while (
                protected_index < len(protected_tags)
                and protected_tags[protected_index][1] <= opening
            ):
                protected_index += 1
            if (
                protected_index < len(protected_tags)
                and protected_tags[protected_index][0] <= opening
            ):
                continue
            opener = opening_match.group(0)
            if closing_positions[opener] < opening_match.end():
                spans.append((opening, len(value)))
                break
    return tuple(spans)


def _relative_link_destination_spans(
    value: str,
    start: int,
    end: int,
) -> tuple[tuple[int, int], ...]:
    """Return only destination/reference syntax from one Markdown link span."""

    raw = value[start:end]
    if raw.startswith("<"):
        return ((start, end),)
    bracket_start = 1 if raw.startswith("![") else (0 if raw.startswith("[") else -1)
    if bracket_start < 0:
        return ()
    state = StateInline(raw, STYLE_MARKDOWN_PROBE_PARSER, {}, [])
    label_end = parseLinkLabel(state, bracket_start)
    position = label_end + 1
    if label_end < 0 or position >= len(raw) or raw[position] not in "([":
        return ()
    return ((start + position, end),)


def _relative_inline_protected_spans(
    value: str,
    tokens: list[Token],
    *,
    base_offset: int = 0,
    protect_html_tags: bool = True,
) -> list[tuple[int, int]]:
    """Collect protected inline syntax, including nested image destinations."""

    spans: list[tuple[int, int]] = []
    for token in tokens:
        start = token.meta.get(STYLE_SOURCE_START)
        end = token.meta.get(STYLE_SOURCE_END)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if token.type == "code_inline" or (
            protect_html_tags and token.type == "html_inline"
        ):
            spans.append((base_offset + start, base_offset + end))
        elif token.type in {"image", "link_open", "link_close"}:
            spans.extend(
                (base_offset + span_start, base_offset + span_end)
                for span_start, span_end in _relative_link_destination_spans(
                    value,
                    start,
                    end,
                )
            )

        if token.type != "image" or not token.children:
            continue
        raw = value[start:end]
        if not raw.startswith("!["):
            continue
        state = StateInline(raw, STYLE_MARKDOWN_PROBE_PARSER, {}, [])
        label_end = parseLinkLabel(state, 1)
        if label_end < 0:
            continue
        label_start = start + 2
        spans.extend(
            _relative_inline_protected_spans(
                token.content,
                token.children,
                base_offset=base_offset + label_start,
                protect_html_tags=protect_html_tags,
            )
        )
    return spans


def _inline_content_source_map(
    value: str,
    token: Token,
    line_starts: list[int],
    search_offsets: dict[tuple[int, int], int],
) -> list[int]:
    """Map normalized inline content characters to physical source offsets."""

    if token.map is None:
        return []
    start_line, end_line = token.map
    block_start = line_starts[min(start_line, len(line_starts) - 1)]
    block_end = line_starts[min(end_line, len(line_starts) - 1)]
    block_key = (block_start, block_end)
    physical_line = start_line
    cursor = search_offsets.get(block_key, block_start)
    mapped: list[int] = []
    for part in token.content.splitlines(keepends=True):
        line_text = part.rstrip("\r\n")
        newline_count = len(part) - len(line_text)
        found = -1
        line_mapping: list[int] = []
        content_end = block_end
        while physical_line < end_line:
            physical_start = line_starts[physical_line]
            physical_end = line_starts[min(physical_line + 1, len(line_starts) - 1)]
            content_end = physical_end
            while content_end > physical_start and value[content_end - 1] in "\r\n":
                content_end -= 1
            search_start = max(cursor, physical_start)
            found = (
                value.find(line_text, search_start, content_end)
                if line_text
                else min(search_start, content_end)
            )
            if found >= 0:
                line_mapping = list(range(found, found + len(line_text)))
                break
            normalized: list[str] = []
            normalized_positions: list[int] = []
            source_position = search_start
            while source_position < content_end:
                if (
                    value[source_position] == "\\"
                    and source_position + 1 < content_end
                    and value[source_position + 1] == "|"
                ):
                    normalized.append("|")
                    normalized_positions.append(source_position + 1)
                    source_position += 2
                    continue
                normalized.append(value[source_position])
                normalized_positions.append(source_position)
                source_position += 1
            normalized_found = "".join(normalized).find(line_text)
            if normalized_found >= 0:
                line_mapping = normalized_positions[
                    normalized_found : normalized_found + len(line_text)
                ]
                found = line_mapping[0] if line_mapping else search_start
                break
            physical_line += 1
            if physical_line < end_line:
                cursor = line_starts[physical_line]
        if found < 0:
            return []
        mapped.extend(line_mapping)
        if newline_count:
            mapped.extend([content_end] * newline_count)
            physical_line += 1
            cursor = (
                line_starts[physical_line]
                if physical_line < len(line_starts)
                else block_end
            )
        else:
            cursor = line_mapping[-1] + 1 if line_mapping else found
    search_offsets[block_key] = cursor
    return mapped


def _mapped_source_spans(
    mapping: list[int],
    relative_spans: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Project relative inline spans through a physical character map."""

    merged_relative: list[list[int]] = []
    for start, end in sorted(relative_spans):
        bounded_start = max(0, start)
        bounded_end = min(len(mapping), end)
        if bounded_end <= bounded_start:
            continue
        if merged_relative and bounded_start <= merged_relative[-1][1]:
            merged_relative[-1][1] = max(
                merged_relative[-1][1],
                bounded_end,
            )
        else:
            merged_relative.append([bounded_start, bounded_end])
    selected = sorted(
        {
            mapping[position]
            for start, end in merged_relative
            for position in range(start, end)
        }
    )
    if not selected:
        return []
    spans: list[tuple[int, int]] = []
    span_start = selected[0]
    previous = selected[0]
    for position in selected[1:]:
        if position > previous + 1:
            spans.append((span_start, previous + 1))
            span_start = position
        previous = position
    spans.append((span_start, previous + 1))
    return spans


def _jinja_lexer_source(
    text: str,
    *,
    protect_html_tags: bool = True,
) -> str:
    """Mask declared Markdown/HTML protected contexts before Jinja lexing."""

    value = _mask_frontmatter(text, mask_entities=False)
    line_starts = [0]
    line_starts.extend(
        match.end() for match in re.finditer(r"\n", value)
    )
    line_starts.append(len(value))
    spans: list[tuple[int, int]] = []
    inline_search_offsets: dict[tuple[int, int], int] = {}
    environment: dict = {}
    tokens = STYLE_MARKDOWN_PROBE_PARSER.parse(value, environment)
    references = list(environment.get("references", {}).values())
    references.extend(environment.get("duplicate_refs", []))
    for reference in references:
        reference_map = reference.get("map") if isinstance(reference, dict) else None
        if (
            not isinstance(reference_map, list)
            or len(reference_map) != 2
            or not all(isinstance(item, int) for item in reference_map)
        ):
            continue
        start_line, end_line = reference_map
        definition_start = line_starts[min(start_line, len(line_starts) - 1)]
        definition_end = line_starts[min(end_line, len(line_starts) - 1)]
        separator = value.find("]:", definition_start, definition_end)
        if separator >= 0:
            spans.append((separator + 2, definition_end))
    for token in tokens:
        if token.map is None:
            continue
        start_line, end_line = token.map
        block_start = line_starts[min(start_line, len(line_starts) - 1)]
        block_end = line_starts[min(end_line, len(line_starts) - 1)]
        if token.type in {"fence", "code_block"}:
            spans.append((block_start, block_end))
            continue
        if token.type == "html_block":
            spans.extend(
                (block_start + start, block_start + end)
                for start, end in _html_protected_spans(
                    token.content,
                    inline_tokens=None,
                    include_tags=protect_html_tags,
                    allow_unclosed=True,
                )
            )
            continue
        if token.type != "inline":
            continue
        mapping = _inline_content_source_map(
            value,
            token,
            line_starts,
            inline_search_offsets,
        )
        if not mapping:
            continue
        relative_spans = _relative_inline_protected_spans(
            token.content,
            token.children or [],
            protect_html_tags=protect_html_tags,
        )
        relative_spans.extend(
            _html_protected_spans(
                token.content,
                inline_tokens=token.children or [],
                include_tags=protect_html_tags,
            )
        )
        spans.extend(_mapped_source_spans(mapping, relative_spans))
    for match in STYLE_JINJA_OPENER_RE.finditer(value):
        cursor = match.start() - 1
        backslashes = 0
        while cursor >= 0 and value[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            spans.append((match.start(), match.start() + 1))
    return _mask_source_spans(value, spans)


def _jinja_source_issues(
    text: str,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Locate real raw directives and unbalanced Jinja source syntax."""

    raw_lines: set[int] = set()
    unbalanced_lines: set[int] = set()
    raw_source = _jinja_lexer_source(text, protect_html_tags=False)
    try:
        for line_number, token_type, _ in STYLE_JINJA_ENVIRONMENT.lex(raw_source):
            if token_type == "raw_begin":
                raw_lines.add(line_number)
    except TemplateSyntaxError:
        pass

    masked = _jinja_lexer_source(text)
    stack: list[tuple[str, int]] = []
    pairs = {
        "block_begin": "block_end",
        "comment_begin": "comment_end",
        "variable_begin": "variable_end",
    }
    try:
        for line_number, token_type, _ in STYLE_JINJA_ENVIRONMENT.lex(masked):
            if token_type in pairs:
                stack.append((pairs[token_type], line_number))
            elif stack and token_type == stack[-1][0]:
                stack.pop()
    except TemplateSyntaxError as error:
        unbalanced_lines.add(error.lineno or 1)
    unbalanced_lines.update(line for _, line in stack)
    return tuple(sorted(raw_lines)), tuple(sorted(unbalanced_lines))


def lint_markdown_document(
    path: Path,
    text: str,
    profile: dict,
) -> list[str]:
    """Apply objective structure and Google-style checks to one document."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    errors: list[str] = []
    label = _path_label(path)
    lines = protected_markdown_lines(text)
    headings: list[tuple[int, int]] = []
    proper_names = style_profile_names(profile)
    lowercase_initials = style_profile_lowercase_prefixes(profile)
    hard_checks = profile.get("hard_checks", {})
    vague_links = {
        value.casefold()
        for value in hard_checks.get("vague_link_text", [])
        if isinstance(value, str)
    }
    discouraged = [
        value
        for value in hard_checks.get("discouraged_phrases", [])
        if isinstance(value, str)
    ]
    raw_lines, unbalanced_lines = _jinja_source_issues(text)
    for number in raw_lines:
        errors.append(
            f"[CommonMark/project] {label}:{number}: Jinja raw blocks are "
            "unsupported in linted documentation"
        )
    for number in unbalanced_lines:
        errors.append(
            f"[CommonMark/project] {label}:{number}: unbalanced Jinja source "
            "is unsupported in linted documentation"
        )

    for line in lines:
        if line.heading_level is not None:
            level = line.heading_level
            if not line.heading_has_content:
                errors.append(
                    f"[CommonMark/project] {label}:{line.number}: empty heading"
                )
            headings.append((line.number, level))
            visible_body = line.heading_text.strip()
            if visible_body:
                heading_initials = set(lowercase_initials)
                if line.protected_heading_prefix:
                    first_visible = STYLE_WORD_RE.search(visible_body)
                    if first_visible:
                        heading_initials.add(first_visible.group(0).casefold())
                violation = sentence_case_violation(
                    visible_body,
                    proper_names=proper_names,
                    lowercase_initials=heading_initials,
                )
                if violation:
                    bad_word, bad_position = violation
                    heading_words = list(STYLE_WORD_RE.finditer(visible_body))
                    word_index = next(
                        (
                            index
                            for index, match in enumerate(heading_words)
                            if match.start() == bad_position
                        ),
                        0,
                    )
                    rendered_heading, rendered_offsets = (
                        _rendered_prose_with_offsets(line.prose_fragments)
                    )
                    rendered_words = list(
                        STYLE_WORD_RE.finditer(rendered_heading)
                    )
                    bad_word_offset = (
                        rendered_offsets[rendered_words[word_index].start()]
                        if word_index < len(rendered_words)
                        and rendered_words[word_index].start() < len(rendered_offsets)
                        else 0
                    )
                    errors.append(
                        f"[Google style] {label}:{line.number + bad_word_offset}: "
                        "heading must use sentence case; unexpected "
                        f"{bad_word!r}"
                    )

        for link in line.links:
            normalized = _plain_link_text(link.text)
            if (
                not normalized
                or normalized in vague_links
                or re.fullmatch(r"https?://\S+", normalized)
            ):
                errors.append(
                    f"[Google style] {label}:{line.number + link.line_offset}: "
                    f"use descriptive link text instead of {link.text!r}"
                )

        for offset in line.missing_html_alt_offsets:
            errors.append(
                f"[Google style] {label}:{line.number + offset}: image requires "
                "an alt attribute"
            )

        for offset in line.unsupported_html_heading_offsets:
            errors.append(
                f"[CommonMark/project] {label}:{line.number + offset}: raw "
                "HTML headings are unsupported; use Markdown heading syntax"
            )

        for offset, visible_line in _prose_by_line(line.ampersand_fragments):
            if STYLE_RAW_AMPERSAND_RE.search(visible_line):
                errors.append(
                    f"[Google style] {label}:{line.number + offset}: use 'and' "
                    "instead of an ampersand in prose"
                )
        for phrase in discouraged:
            for offset in _phrase_line_offsets(line.prose_fragments, phrase):
                errors.append(
                    f"[Google style] {label}:{line.number + offset}: replace "
                    f"deterministic directional phrase {phrase!r}"
                )

    h1_count = sum(level == 1 for _, level in headings)
    if h1_count != 1:
        errors.append(
            f"[CommonMark/project] {label}: expected exactly one level-1 "
            f"heading; found {h1_count}"
        )
    previous_level = 0
    for number, level in headings:
        if level > previous_level + 1:
            errors.append(
                f"[CommonMark/project] {label}:{number}: heading level jumps "
                f"from {previous_level} to {level}"
            )
        previous_level = level
    return errors


def documentation_style_paths(
    profile: dict,
    *,
    check_generated: bool,
) -> tuple[list[Path], list[str]]:
    """Resolve reviewed source and optional generated documentation paths."""

    errors: list[str] = []
    paths: list[Path] = []
    repository = REPO_ROOT.resolve()
    documents = profile.get("documents", {})
    for relative in documents.get("source", []):
        path = REPO_ROOT / relative
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(repository)
            or path.is_symlink()
            or not path.is_file()
        ):
            errors.append(
                f"config/documentation-style.yaml: unsafe or missing document {relative!r}"
            )
        else:
            paths.append(path)
    if check_generated and BUILD_ROOT.exists():
        generated_glob = documents.get("generated_glob", "")
        for path in sorted(REPO_ROOT.glob(generated_glob)):
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(repository)
                or path.is_symlink()
                or not path.is_file()
            ):
                errors.append(
                    f"config/documentation-style.yaml: unsafe generated document {path}"
                )
            else:
                paths.append(path)
    return paths, errors


def lint_documentation_style(
    profile: dict,
    *,
    check_generated: bool,
) -> list[str]:
    """Lint all configured documentation without treating advice as errors."""

    paths, errors = documentation_style_paths(
        profile,
        check_generated=check_generated,
    )
    for path in paths:
        errors.extend(lint_markdown_document(path, read_text(path), profile))
    return errors


def uncaptured_command(command: str) -> str:
    return re.sub(
        r"(?i)^\s*(?:(?:capture|quietly|noisily)\s+)+",
        "",
        command,
    ).strip()


def lint_config(
    config: dict,
    style_profile: dict | None = None,
) -> list[str]:
    errors: list[str] = []
    style_profile = style_profile or load_documentation_style_profile()
    proper_names = style_profile_names(style_profile)
    lowercase_initials = style_profile_lowercase_prefixes(style_profile)
    if config.get("schema_version") != 1:
        errors.append("config/skills.yaml: schema_version must be 1")
    expected_resolution = {
        "exact_stems_field": "provenance.local_help_topics",
        "explicit_globs_field": "provenance.local_help_globs",
        "resolved_files_field": "provenance.local_help_files",
        "source_topics_role": (
            "Reader-facing topic labels only; never used for filesystem matching."
        ),
    }
    if config.get("source_resolution") != expected_resolution:
        errors.append(
            "config/skills.yaml: source_resolution must make provenance selectors authoritative"
        )
    skills = config.get("skills")
    if not isinstance(skills, dict) or not skills:
        return [*errors, "config/skills.yaml: skills must be a nonempty mapping"]

    names: set[str] = set()
    folders: set[str] = set()
    content_dirs: set[str] = set()
    for skill_key, skill in skills.items():
        if not isinstance(skill, dict):
            errors.append(f"config/skills.yaml: skill {skill_key} must be a mapping")
            continue
        for field in (
            "name",
            "folder",
            "content_dir",
            "route_dir",
            "heading",
            "description",
            "summary",
            "section_order",
            "validation_modes",
            "rules",
            "critical_gotchas",
            "interface",
        ):
            if field not in skill:
                errors.append(f"config/skills.yaml: skill {skill_key} missing {field}")
        for field in ("heading", "description", "summary"):
            if not is_nonempty_string(skill.get(field)):
                errors.append(
                    f"config/skills.yaml: skill {skill_key} {field} must be nonempty"
                )
        heading = skill.get("heading")
        if isinstance(heading, str) and (
            bad_word := sentence_case_error(
                heading,
                proper_names=proper_names,
                lowercase_initials=lowercase_initials,
            )
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} heading must use "
                f"sentence case; unexpected {bad_word!r}"
            )
        for field, seen in (
            ("name", names),
            ("folder", folders),
            ("content_dir", content_dirs),
        ):
            value = skill.get(field)
            if not is_nonempty_string(value):
                errors.append(f"config/skills.yaml: skill {skill_key} {field} must be nonempty")
            elif value in seen:
                errors.append(f"config/skills.yaml: duplicate {field} {value}")
            else:
                seen.add(value)
        for field in ("folder", "content_dir", "route_dir"):
            value = skill.get(field)
            if isinstance(value, str) and (
                Path(value).is_absolute()
                or Path(value).name != value
                or value in {".", ".."}
            ):
                errors.append(
                    f"config/skills.yaml: skill {skill_key} {field} "
                    "must be one safe directory name"
                )
        sections = skill.get("section_order")
        if (
            not isinstance(sections, list)
            or not sections
            or any(not is_nonempty_string(section) for section in sections)
            or len(sections) != len(set(sections))
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} section_order must contain unique names"
            )
        elif sections:
            for section in sections:
                if bad_word := sentence_case_error(
                    section,
                    proper_names=proper_names,
                    lowercase_initials=lowercase_initials,
                ):
                    errors.append(
                        f"config/skills.yaml: skill {skill_key} section "
                        f"must use sentence case; unexpected {bad_word!r}"
                    )
        modes = skill.get("validation_modes")
        if (
            not isinstance(modes, list)
            or not modes
            or not set(modes).issubset(VALIDATION_MODES)
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} has invalid validation_modes"
            )
        rules = skill.get("rules")
        if (
            not isinstance(rules, list)
            or not rules
            or any(not is_nonempty_string(rule) for rule in rules)
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} rules must be a "
                "nonempty string list"
            )
        gotchas = skill.get("critical_gotchas")
        if not isinstance(gotchas, list) or any(
            not isinstance(item, dict)
            or not is_nonempty_string(item.get("title"))
            or not is_nonempty_string(item.get("body"))
            for item in gotchas if isinstance(gotchas, list)
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} critical_gotchas "
                "must contain title/body mappings"
            )
        interface = skill.get("interface")
        if not isinstance(interface, dict) or any(
            not is_nonempty_string(interface.get(field))
            for field in ("display_name", "short_description", "default_prompt")
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} interface metadata "
                "is incomplete"
            )
        elif bad_word := sentence_case_error(
            interface["display_name"],
            proper_names=proper_names,
            lowercase_initials=lowercase_initials,
        ):
            errors.append(
                f"config/skills.yaml: skill {skill_key} display_name must use "
                f"sentence case; unexpected {bad_word!r}"
            )
    boundaries = config.get("routing_boundaries")
    if not isinstance(boundaries, list):
        errors.append("config/skills.yaml: routing_boundaries must be a list")
    return errors


def lint_provenance(
    source_label: str,
    skill_key: str,
    provenance: object,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(provenance, dict):
        return [f"{source_label}: provenance must be a mapping"]
    for field in (
        "local_help_topics",
        "local_help_globs",
        "local_help_files",
        "upstream_files",
        "upstream_only",
        "package_only",
        "reviewed",
    ):
        if field not in provenance:
            errors.append(f"{source_label}: provenance missing {field}")
    if provenance.get("reviewed") is not True:
        errors.append(f"{source_label}: provenance.reviewed must be true")
    if not isinstance(provenance.get("upstream_only"), bool):
        errors.append(f"{source_label}: provenance.upstream_only must be boolean")
    if not isinstance(provenance.get("package_only"), bool):
        errors.append(f"{source_label}: provenance.package_only must be boolean")

    topics = provenance.get("local_help_topics", [])
    globs = provenance.get("local_help_globs", [])
    files = provenance.get("local_help_files", [])
    upstream = provenance.get("upstream_files", [])
    for field, values in (
        ("local_help_topics", topics),
        ("local_help_globs", globs),
        ("local_help_files", files),
        ("upstream_files", upstream),
    ):
        if not isinstance(values, list) or any(not is_nonempty_string(item) for item in values):
            errors.append(f"{source_label}: provenance.{field} must be a string list")
    if isinstance(topics, list):
        for topic in topics:
            if isinstance(topic, str) and (
                any(token in topic for token in ("*", "?", "[", "]", "/", "\\"))
                or topic.endswith(".sthlp")
            ):
                errors.append(
                    f"{source_label}: local help topic {topic!r} is not an exact .sthlp stem"
                )
    if isinstance(globs, list):
        for pattern in globs:
            if isinstance(pattern, str) and (
                not pattern.endswith(".sthlp")
                or not any(token in pattern for token in ("*", "?", "["))
                or Path(pattern).is_absolute()
                or ".." in Path(pattern).parts
            ):
                errors.append(
                    f"{source_label}: local_help_glob {pattern!r} must be a safe explicit .sthlp glob"
                )
    if isinstance(files, list):
        if len(files) != len(set(files)):
            errors.append(f"{source_label}: duplicate local_help_files")
        for value in files:
            if isinstance(value, str) and (
                not value.startswith("ado/base/")
                or not value.endswith(".sthlp")
                or Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                errors.append(f"{source_label}: unsafe local help path {value!r}")
    if isinstance(upstream, list):
        for value in upstream:
            if isinstance(value, str) and (
                Path(value).is_absolute() or ".." in Path(value).parts
            ):
                errors.append(f"{source_label}: unsafe upstream path {value!r}")

    source_bases = sum(
        (
            bool(files),
            provenance.get("upstream_only") is True,
            provenance.get("package_only") is True,
        )
    )
    if source_bases != 1:
        errors.append(
            f"{source_label}: exactly one source basis is required "
            "(local help, upstream_only, or package_only)"
        )
    if provenance.get("package_only") is True and skill_key != "packages":
        errors.append(f"{source_label}: package_only is valid only for package entries")
    return errors


def lint_entry(
    skill_key: str,
    path: Path,
    entry: object,
    skill: dict,
    style_profile: dict | None = None,
) -> list[str]:
    source_label = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
    if not isinstance(entry, dict):
        return [f"{source_label}: content must be a mapping"]
    errors: list[str] = []
    style_profile = style_profile or load_documentation_style_profile()
    missing = sorted(REQUIRED_FIELDS - set(entry))
    for field in missing:
        errors.append(f"{source_label}: missing field {field}")
    if missing:
        return errors

    slug = entry.get("slug")
    if not is_safe_slug(slug):
        errors.append(f"{source_label}: invalid slug")
    elif path.stem != slug:
        errors.append(f"{source_label}: filename must match slug {slug}")
    if entry.get("skill") != skill_key:
        errors.append(f"{source_label}: skill must be {skill_key}")
    if entry.get("section") not in skill["section_order"]:
        errors.append(f"{source_label}: unrecognized section {entry.get('section')!r}")
    if not isinstance(entry.get("order"), int) or isinstance(entry.get("order"), bool):
        errors.append(f"{source_label}: order must be an integer")
    for field in ("title", "trigger", "validation_case"):
        if not is_nonempty_string(entry.get(field)):
            errors.append(f"{source_label}: {field} must be nonempty")
    title = entry.get("title")
    if isinstance(title, str) and (
        bad_word := sentence_case_error(
            title,
            proper_names=style_profile_names(style_profile),
            lowercase_initials=style_profile_lowercase_prefixes(style_profile),
        )
    ):
        errors.append(
            f"{source_label}: title must use sentence case; "
            f"unexpected {bad_word!r}"
        )
    trigger = entry.get("trigger")
    if isinstance(trigger, str):
        if limit_error := copy_text_limit_error(trigger):
            errors.append(f"{source_label}: trigger {limit_error}")
    for field in LIST_FIELDS:
        value = entry.get(field)
        if not isinstance(value, list) or any(not is_nonempty_string(item) for item in value):
            errors.append(f"{source_label}: {field} must be a string list")
        elif field in NONEMPTY_LIST_FIELDS and not value:
            errors.append(f"{source_label}: {field} must not be empty")
        elif len(value) != len(set(value)):
            errors.append(f"{source_label}: {field} contains duplicates")
    routing_terms = entry.get("routing_terms", [])
    if isinstance(routing_terms, list):
        normalized_pairs = [
            (term, normalized_routing_term(term))
            for term in routing_terms
            if isinstance(term, str)
        ]
        normalized_terms = [normalized for _, normalized in normalized_pairs]
        if any(not term for term in normalized_terms):
            errors.append(f"{source_label}: routing_terms contains an empty normalized term")
        if len(normalized_terms) != len(set(normalized_terms)):
            errors.append(
                f"{source_label}: routing_terms contains normalized duplicates"
            )
        for raw_term, normalized_term in normalized_pairs:
            if any(
                copy_equivalent(raw_term, generic_term)
                for generic_term in GENERIC_ROUTING_TERMS
            ):
                errors.append(
                    f"{source_label}: routing term {raw_term!r} is too generic"
                )
    preflight = entry.get("preflight_commands", [])
    if not isinstance(preflight, list) or any(
        not is_nonempty_string(item) for item in preflight
    ):
        errors.append(f"{source_label}: preflight_commands must be a string list")
    elif any(
        not READ_ONLY_PREFLIGHT_RE.match(uncaptured_command(command))
        for command in preflight
    ):
        errors.append(
            f"{source_label}: preflight_commands must contain only read-only "
            "discovery commands"
        )

    mode = entry.get("validation_mode")
    if mode not in skill["validation_modes"]:
        errors.append(
            f"{source_label}: validation_mode {mode!r} is not allowed for {skill_key}"
        )
    smoke_test = entry.get("smoke_test")
    if smoke_test is not None and not is_nonempty_string(smoke_test):
        errors.append(f"{source_label}: smoke_test must be null or a nonempty string")
    if mode == "stata" and not is_nonempty_string(smoke_test):
        errors.append(f"{source_label}: stata validation requires smoke_test")
    if mode == "manual-review" and smoke_test:
        errors.append(f"{source_label}: manual-review entries must not claim an executable smoke_test")
    if mode == "compilation":
        case = normalized_text(str(entry.get("validation_case", "")))
        if not any(word in case for word in ("compile", "build")) or not any(
            word in case for word in ("assert", "verify", "confirm", "pass", "fail")
        ):
            errors.append(
                f"{source_label}: compilation validation_case must state a compile assertion"
            )
    if mode == "manual-review":
        case = normalized_text(str(entry.get("validation_case", "")))
        if not any(word in case for word in ("review", "inspect", "audit", "document", "compare")):
            errors.append(
                f"{source_label}: manual-review validation_case must state the review action"
            )

    for field in ("syntax_patterns", "gotchas", "assumptions", "workflows"):
        for value in entry.get(field, []):
            if not isinstance(value, str):
                continue
            normalized = normalized_text(value)
            if (
                any(
                    copy_equivalent(value, generic_text)
                    for generic_text in GENERIC_TEXT
                )
                or len(normalized.split()) < 3
            ):
                errors.append(
                    f"{source_label}: {field} contains generic content {value!r}"
                )
    validation_case = str(entry.get("validation_case", ""))
    if (
        any(
            copy_equivalent(validation_case, generic_text)
            for generic_text in GENERIC_TEXT
        )
        or copy_startswith(
            validation_case,
            "run a small batch mode example that exercises",
        )
    ):
        errors.append(f"{source_label}: validation_case is generic")

    for command in [*preflight, *entry.get("install_commands", [])]:
        for pattern, message in UNSAFE_INSTALL_PATTERNS:
            if pattern.search(command):
                errors.append(f"{source_label}: install/preflight command {message}: {command}")

    errors.extend(
        lint_provenance(source_label, skill_key, entry.get("provenance"))
    )
    return errors


def lint_routing_collisions(
    config: dict,
    entries: list[tuple[str, Path, dict]],
) -> list[str]:
    """Require every normalized cross-route collision to be reviewed."""

    errors: list[str] = []
    known_routes = {
        f"{skill_key}/{entry.get('slug')}"
        for skill_key, _, entry in entries
        if isinstance(entry, dict) and is_nonempty_string(entry.get("slug"))
    }
    observed: dict[str, set[str]] = {}
    for skill_key, _, entry in entries:
        if not isinstance(entry, dict):
            continue
        route = f"{skill_key}/{entry.get('slug')}"
        for term in entry.get("routing_terms", []):
            if not isinstance(term, str):
                continue
            normalized = normalized_routing_term(term)
            if normalized:
                observed.setdefault(normalized, set()).add(route)

    declared: dict[str, set[str]] = {}
    for index, boundary in enumerate(config.get("routing_boundaries", []), start=1):
        label = f"config/skills.yaml: routing boundary {index}"
        if not isinstance(boundary, dict):
            errors.append(f"{label} must be a mapping")
            continue
        term = boundary.get("term")
        routes = boundary.get("routes")
        if not is_nonempty_string(term):
            errors.append(f"{label} requires a nonempty term")
            continue
        normalized = normalized_routing_term(term)
        if normalized in declared:
            errors.append(f"{label} duplicates normalized term {normalized!r}")
            continue
        if (
            not isinstance(routes, list)
            or len(routes) < 2
            or any(not is_nonempty_string(route) for route in routes)
            or len(routes) != len(set(routes))
        ):
            errors.append(f"{label} routes must contain at least two unique routes")
            continue
        route_set = set(routes)
        unknown = sorted(route_set - known_routes)
        if unknown:
            errors.append(f"{label} has unknown routes: {', '.join(unknown)}")
        if boundary.get("action") != "clarify":
            errors.append(f"{label} action must be clarify")
        if not is_nonempty_string(boundary.get("guidance")):
            errors.append(f"{label} requires concise clarification guidance")
        declared[normalized] = route_set

    collisions = {
        term: routes for term, routes in observed.items() if len(routes) > 1
    }
    for term, routes in sorted(collisions.items()):
        if term not in declared:
            errors.append(
                "content/: undeclared normalized routing collision "
                f"{term!r}: {', '.join(sorted(routes))}"
            )
        elif declared[term] != routes:
            errors.append(
                f"config/skills.yaml: routing boundary {term!r} must list "
                f"exactly {', '.join(sorted(routes))}"
            )
    for term in sorted(set(declared) - set(collisions)):
        errors.append(
            f"config/skills.yaml: orphan routing boundary {term!r}"
        )
    return errors


def lint_route_aliases(
    config: dict,
    slug_to_skill: dict[str, str],
    route_paths: set[str],
) -> list[str]:
    errors: list[str] = []
    aliases = config.get("route_aliases", [])
    if not isinstance(aliases, list):
        return ["config/skills.yaml: route_aliases must be a list"]
    seen: set[tuple[str, str]] = set()
    for alias in aliases:
        if not isinstance(alias, dict):
            errors.append("config/skills.yaml: route alias must be a mapping")
            continue
        for field in (
            "from_skill",
            "from_route",
            "aliases",
            "task_intent",
            "to_skill",
            "to_slug",
        ):
            if field not in alias:
                errors.append(f"config/skills.yaml: route alias missing {field}")
        key = (alias.get("from_skill"), alias.get("from_route"))
        if key in seen:
            errors.append(f"config/skills.yaml: duplicate route alias {key}")
        seen.add(key)
        if alias.get("from_skill") not in config["skills"]:
            errors.append(f"config/skills.yaml: unknown alias source skill {key[0]}")
        else:
            from_route = alias.get("from_route")
            source_route_dir = config["skills"][alias["from_skill"]]["route_dir"]
            if (
                not is_nonempty_string(from_route)
                or Path(str(from_route)).is_absolute()
                or ".." in Path(str(from_route)).parts
                or Path(str(from_route)).suffix != ".md"
                or Path(str(from_route)).parent != Path(source_route_dir)
            ):
                errors.append(
                    f"config/skills.yaml: alias route {from_route!r} must be "
                    f"a safe Markdown path under {source_route_dir}/"
                )
            qualified_from = (
                f"{config['skills'][alias['from_skill']]['name']}/"
                f"{from_route}"
            )
            if qualified_from in route_paths:
                errors.append(f"config/skills.yaml: alias shadows canonical route {key}")
        target_slug = alias.get("to_slug")
        if target_slug not in slug_to_skill:
            errors.append(f"config/skills.yaml: alias target {target_slug!r} does not exist")
        elif slug_to_skill[target_slug] != alias.get("to_skill"):
            errors.append(f"config/skills.yaml: alias target skill mismatch for {target_slug}")
        if not isinstance(alias.get("aliases"), list) or not alias.get("aliases"):
            errors.append(f"config/skills.yaml: alias {key} requires aliases")
        if not is_nonempty_string(alias.get("task_intent")):
            errors.append(f"config/skills.yaml: alias {key} requires task_intent")
    return errors


def lint_prompt_cases(
    config: dict,
    canonical_paths: set[str],
    alias_paths: set[str],
    prompt_path: Path = PROMPT_CASES_PATH,
    canonical_triggers: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    data = read_yaml(prompt_path)
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 2
        or not isinstance(data.get("cases"), list)
    ):
        return [f"{prompt_path}: expected schema_version 2 and cases list"]
    valid_skills = {skill["name"] for skill in config["skills"].values()}
    valid_actions = {"route", "clarify", "abstain"}
    ids: set[str] = set()
    covered: Counter[str] = Counter()
    boundary_by_skill: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    clarify_terms: Counter[str] = Counter()
    for index, case in enumerate(data["cases"]):
        label = f"{prompt_path}: case {index + 1}"
        if not isinstance(case, dict):
            errors.append(f"{label} must be a mapping")
            continue
        for field in (
            "id",
            "prompt",
            "action",
            "expected_skill",
            "expected_refs",
            "forbidden_routes",
            "boundary",
        ):
            if field not in case:
                errors.append(f"{label} missing {field}")
        case_id = case.get("id")
        if not is_nonempty_string(case_id) or case_id in ids:
            errors.append(f"{label} has invalid or duplicate id")
        else:
            ids.add(case_id)
        prompt = case.get("prompt")
        prompt_copy_safe = False
        if not is_nonempty_string(prompt):
            errors.append(f"{label} prompt must be nonempty")
        elif "i need task-specific guidance" in prompt.casefold():
            errors.append(
                f"{label} prompt is generated scaffold text, not an independent fixture"
            )
        elif limit_error := copy_text_limit_error(prompt):
            errors.append(f"{label} prompt {limit_error}")
        else:
            prompt_copy_safe = True
        if prompt_copy_safe and canonical_triggers:
            for route, trigger in sorted(canonical_triggers.items()):
                copy_kind = trigger_copy_kind(prompt, trigger)
                if copy_kind:
                    errors.append(
                        f"{label} prompt is a {copy_kind} copy of the canonical "
                        f"trigger for {route}"
                    )
        action = case.get("action")
        if action not in valid_actions:
            errors.append(f"{label} action must be route, clarify, or abstain")
        else:
            action_counts[action] += 1
        skill_name = case.get("expected_skill")
        expected = case.get("expected_refs")
        forbidden = case.get("forbidden_routes")
        if not isinstance(expected, list):
            errors.append(f"{label} expected_refs must be a list")
            expected = []
        elif any(not is_nonempty_string(route) for route in expected):
            errors.append(f"{label} expected_refs must contain only nonempty strings")
            expected = [route for route in expected if is_nonempty_string(route)]
        if not isinstance(forbidden, list):
            errors.append(f"{label} forbidden_routes must be a list")
            forbidden = []
        elif any(not is_nonempty_string(route) for route in forbidden):
            errors.append(
                f"{label} forbidden_routes must contain only nonempty strings"
            )
            forbidden = [
                route for route in forbidden if is_nonempty_string(route)
            ]
        if not isinstance(case.get("boundary"), bool):
            errors.append(f"{label} boundary must be boolean")
        if action == "route":
            if skill_name not in valid_skills:
                errors.append(f"{label} expected_skill is unknown")
            if not expected:
                errors.append(f"{label} route action requires expected_refs")
        elif action in {"clarify", "abstain"}:
            if case.get("boundary") is not True:
                errors.append(f"{label} {action} action requires boundary true")
            if skill_name is not None:
                errors.append(f"{label} {action} action requires expected_skill null")
            if expected:
                errors.append(f"{label} {action} action requires empty expected_refs")
            if forbidden:
                errors.append(f"{label} {action} action requires empty forbidden_routes")
        if action == "clarify":
            routing_term = case.get("routing_term")
            if not is_nonempty_string(routing_term):
                errors.append(f"{label} clarify action requires routing_term")
            else:
                normalized_term = normalized_routing_term(routing_term)
                clarify_terms[normalized_term] += 1
                if (
                    is_nonempty_string(prompt)
                    and not contains_routing_term(prompt, routing_term)
                ):
                    errors.append(
                        f"{label} prompt must state its ambiguous routing_term"
                    )
        elif "routing_term" in case:
            errors.append(f"{label} routing_term is valid only for clarify actions")
        for route in expected:
            if route not in canonical_paths:
                errors.append(f"{label} expected ref {route!r} is not canonical")
            else:
                covered[route] += 1
                if (
                    action == "route"
                    and skill_name in valid_skills
                    and route.partition("/")[0] != skill_name
                ):
                    errors.append(
                        f"{label} expected ref {route!r} does not belong to "
                        f"expected_skill {skill_name!r}"
                    )
            if route in forbidden:
                errors.append(f"{label} route {route!r} is both expected and forbidden")
        for route in forbidden:
            if route not in canonical_paths | alias_paths:
                errors.append(f"{label} forbidden route {route!r} is unknown")
        if case.get("boundary") is True and action == "route":
            if not forbidden:
                errors.append(f"{label} boundary case requires forbidden_routes")
            if skill_name in valid_skills:
                boundary_by_skill[skill_name] += 1
    for route in sorted(canonical_paths):
        if not covered[route]:
            errors.append(f"{prompt_path}: canonical route lacks prompt coverage: {route}")
    for skill_name in sorted(valid_skills):
        if not boundary_by_skill[skill_name]:
            errors.append(f"{prompt_path}: {skill_name} lacks a boundary case")
    if action_counts["clarify"] < 3:
        errors.append(f"{prompt_path}: requires at least three clarify cases")
    if action_counts["abstain"] < 2:
        errors.append(f"{prompt_path}: requires at least two abstain cases")
    declared_terms = {
        normalized_routing_term(boundary["term"])
        for boundary in config.get("routing_boundaries", [])
        if isinstance(boundary, dict) and is_nonempty_string(boundary.get("term"))
    }
    if set(clarify_terms) != declared_terms:
        missing = sorted(declared_terms - set(clarify_terms))
        extra = sorted(set(clarify_terms) - declared_terms)
        if missing:
            errors.append(
                f"{prompt_path}: routing boundaries lack clarify cases: "
                + ", ".join(missing)
            )
        if extra:
            errors.append(
                f"{prompt_path}: clarify cases lack declared boundaries: "
                + ", ".join(extra)
            )
    for term, count in clarify_terms.items():
        if count != 1:
            errors.append(
                f"{prompt_path}: routing boundary {term!r} requires exactly one clarify case"
            )
    return errors


def lint_upstream_lock(entries: list[tuple[str, Path, dict]]) -> list[str]:
    path = LOCK_ROOT / "upstream.yaml"
    lock = read_yaml(path)
    errors: list[str] = []
    if lock.get("schema_version") != 1:
        return [f"{path}: schema_version must be 1"]
    repository = lock.get("repository", {})
    if not isinstance(repository, dict):
        return [f"{path}: repository must be a mapping"]
    commit = str(repository.get("commit", ""))
    expected_commit = str(repository.get("expected_commit", ""))
    if not COMMIT_RE.fullmatch(commit):
        errors.append(f"{path}: repository.commit must be a full Git SHA")
    if not COMMIT_RE.fullmatch(expected_commit):
        errors.append(f"{path}: repository.expected_commit must be a full Git SHA")
    elif expected_commit != commit:
        errors.append(
            f"{path}: repository commit drift requires explicit lock review"
        )
    if repository.get("url") != UPSTREAM_REPO_URL:
        errors.append(
            f"{path}: repository.url must exactly match the configured upstream "
            f"repository {UPSTREAM_REPO_URL}"
        )
    files = lock.get("files")
    if not isinstance(files, dict):
        return [*errors, f"{path}: files must be a mapping"]
    declared = {
        upstream_path
        for _, _, entry in entries
        for upstream_path in entry.get("provenance", {}).get("upstream_files", [])
    }
    if set(files) != declared:
        missing = sorted(declared - set(files))
        extra = sorted(set(files) - declared)
        if missing:
            errors.append(f"{path}: missing declared upstream files: {', '.join(missing)}")
        if extra:
            errors.append(f"{path}: orphan upstream files: {', '.join(extra)}")
    for file_path, metadata in files.items():
        if not isinstance(metadata, dict) or not SHA256_RE.fullmatch(
            str(metadata.get("sha256", ""))
        ):
            errors.append(f"{path}: {file_path} requires sha256")
    return errors


def lint_stata_help_lock(entries: list[tuple[str, Path, dict]]) -> list[str]:
    path = LOCK_ROOT / "stata-help.yaml"
    lock = read_yaml(path)
    errors: list[str] = []
    if lock.get("schema_version") != 1:
        return [f"{path}: schema_version must be 1"]
    release = lock.get("stata_release")
    if not isinstance(release, dict):
        errors.append(f"{path}: stata_release must be a mapping")
    else:
        for field in ("edition", "bundle_identifier", "bundle_version", "executable"):
            if not is_nonempty_string(release.get(field)):
                errors.append(f"{path}: stata_release missing {field}")
    selectors = lock.get("selectors")
    files = lock.get("files")
    if not isinstance(selectors, dict) or not isinstance(files, dict):
        return [*errors, f"{path}: selectors and files must be mappings"]

    expected_keys: set[str] = set()
    declared_files: set[str] = set()
    for skill_key, _, entry in entries:
        provenance = entry.get("provenance", {})
        local_files = provenance.get("local_help_files", [])
        if not local_files:
            continue
        key = f"{skill_key}/{entry.get('slug')}"
        expected_keys.add(key)
        selected = selectors.get(key)
        if not isinstance(selected, dict):
            errors.append(f"{path}: missing selector lock for {key}")
            continue
        expected_topics = provenance.get("local_help_topics", [])
        expected_globs = provenance.get("local_help_globs", [])
        if selected.get("exact_stems") != expected_topics:
            errors.append(f"{path}: exact_stems drift for {key}")
        if selected.get("globs", []) != expected_globs:
            errors.append(f"{path}: globs drift for {key}")
        if selected.get("files") != local_files:
            errors.append(f"{path}: resolved files drift for {key}")
        declared_files.update(local_files)
    if set(selectors) != expected_keys:
        extra = sorted(set(selectors) - expected_keys)
        if extra:
            errors.append(f"{path}: orphan selector locks: {', '.join(extra)}")
    if set(files) != declared_files:
        missing = sorted(declared_files - set(files))
        extra = sorted(set(files) - declared_files)
        if missing:
            errors.append(f"{path}: missing help file hashes: {', '.join(missing)}")
        if extra:
            errors.append(f"{path}: orphan help file hashes: {', '.join(extra)}")
    for file_path, metadata in files.items():
        if not isinstance(metadata, dict) or not SHA256_RE.fullmatch(
            str(metadata.get("sha256", ""))
        ):
            errors.append(f"{path}: {file_path} requires sha256")
    return errors


def lint_package_lock_payload(
    package_path: Path,
    slug: str,
    package: object,
) -> list[str]:
    """Validate one parsed per-package lock without rereading live bytes."""

    errors: list[str] = []
    if not isinstance(package, dict):
        return [f"{package_path}: package lock must be a mapping"]
    if (
        package.get("schema_version") != 1
        or package.get("slug") != slug
    ):
        return [
            f"{package_path}: schema_version must be 1 and slug must match filename"
        ]
    distributions = package.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        return [f"{package_path}: distributions must be a nonempty list"]
    seen_descriptors: set[str] = set()
    for index, distribution in enumerate(distributions):
        label = f"{package_path}: distribution {index + 1}"
        if not isinstance(distribution, dict):
            errors.append(f"{label} must be a mapping")
            continue
        for field in ("source", "descriptor", "distribution_date", "files"):
            if field not in distribution:
                errors.append(f"{label} missing {field}")
        if not re.match(
            r"^https?://", str(distribution.get("source", ""))
        ):
            errors.append(f"{label} source must be an HTTP(S) provenance URL")
        descriptor = distribution.get("descriptor")
        if not is_nonempty_string(descriptor) or descriptor in seen_descriptors:
            errors.append(f"{label} descriptor must be unique and nonempty")
        else:
            seen_descriptors.add(descriptor)
        if not re.fullmatch(
            r"\d{8}", str(distribution.get("distribution_date", ""))
        ):
            errors.append(f"{label} distribution_date must be YYYYMMDD")
        file_map = distribution.get("files", {})
        if not isinstance(file_map, dict) or not file_map:
            errors.append(f"{label} files must be nonempty")
        else:
            for relative, sha in file_map.items():
                if (
                    Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                    or relative in TRACK_METADATA_FILES
                ):
                    errors.append(f"{label} unsafe locked path {relative}")
                if not SHA256_RE.fullmatch(str(sha)):
                    errors.append(f"{label} {relative} invalid sha256")
    generated_files = package.get("generated_files", {})
    if not isinstance(generated_files, dict):
        errors.append(f"{package_path}: generated_files must be a mapping")
    else:
        for relative, sha in generated_files.items():
            if (
                Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or relative in TRACK_METADATA_FILES
            ):
                errors.append(
                    f"{package_path}: unsafe generated path {relative}"
                )
            if not SHA256_RE.fullmatch(str(sha)):
                errors.append(
                    f"{package_path}: {relative} invalid generated sha256"
                )
    return errors


def lint_distribution_locks(
    entries: list[tuple[str, Path, dict]],
) -> list[str]:
    errors: list[str] = []
    required = {
        entry["slug"]
        for skill_key, _, entry in entries
        if skill_key == "packages" and entry.get("install_commands")
    }
    try:
        package_root_metadata = PACKAGE_LOCK_ROOT.lstat()
        if not stat.S_ISDIR(package_root_metadata.st_mode):
            return [
                f"{PACKAGE_LOCK_ROOT}: package lock root must be a real directory"
            ]
        package_children = sorted(PACKAGE_LOCK_ROOT.iterdir())
    except OSError as error:
        return [f"{PACKAGE_LOCK_ROOT}: could not inspect package locks: {error}"]
    lock_paths: list[Path] = []
    for child in package_children:
        try:
            child_metadata = child.lstat()
        except OSError as error:
            errors.append(f"{child}: could not inspect package lock: {error}")
            continue
        if (
            not stat.S_ISREG(child_metadata.st_mode)
            or child.suffix != ".yaml"
        ):
            errors.append(
                f"{child}: package lock root may contain only regular .yaml files"
            )
            continue
        lock_paths.append(child)
    observed = {path.stem for path in lock_paths}
    missing = sorted(required - observed)
    extra = sorted(observed - required)
    if missing:
        errors.append(
            f"{PACKAGE_LOCK_ROOT}: missing installable packages: {', '.join(missing)}"
        )
    if extra:
        errors.append(
            f"{PACKAGE_LOCK_ROOT}: orphan package locks: {', '.join(extra)}"
        )
    for package_path in lock_paths:
        slug = package_path.stem
        package = read_yaml(package_path)
        errors.extend(
            lint_package_lock_payload(
                package_path,
                slug,
                package,
            )
        )

    sdk_path = LOCK_ROOT / "plugin-sdk.yaml"
    lock = read_yaml(sdk_path)
    errors.extend(lint_plugin_sdk_lock_payload(sdk_path, lock))
    return errors


def lint_plugin_sdk_lock_payload(
    sdk_path: Path,
    lock: object,
) -> list[str]:
    """Validate every SDK download field before it can become a path or URL."""

    errors: list[str] = []
    if not isinstance(lock, dict):
        return [f"{sdk_path}: invalid or missing plugin SDK lock schema"]
    sources = lock.get("sources")
    if (
        lock.get("schema_version") != 1
        or not isinstance(sources, list)
        or not sources
    ):
        errors.append(f"{sdk_path}: invalid or missing plugin SDK lock schema")
    else:
        names: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                errors.append(f"{sdk_path}: source must be a mapping")
                continue
            name = source.get("filename")
            if (
                not is_nonempty_string(name)
                or name in {".", ".."}
                or "\x00" in name
                or Path(name).is_absolute()
                or Path(name).parts != (name,)
                or Path(str(name)).name != name
                or name in names
            ):
                errors.append(f"{sdk_path}: duplicate or invalid filename")
            else:
                names.add(name)
            if not str(source.get("url", "")).startswith("https://"):
                errors.append(f"{sdk_path}: {name} URL must use HTTPS")
            if not SHA256_RE.fullmatch(str(source.get("sha256", ""))):
                errors.append(f"{sdk_path}: {name} invalid sha256")
    return errors


def lint_manifests(entries: list[tuple[str, Path, dict]]) -> list[str]:
    errors: list[str] = []
    by_skill: dict[str, set[str]] = {}
    for skill_key, _, entry in entries:
        by_skill.setdefault(skill_key, set()).add(entry.get("slug"))
    names = {
        "core": "topic-map.yaml",
        "packages": "package-map.yaml",
        "plugins": "plugin-map.yaml",
    }
    for skill_key, filename in names.items():
        path = MANIFEST_ROOT / filename
        data = read_yaml(path)
        if data.get("schema_version") != 2 or data.get("role") != "provenance-lock-index":
            errors.append(f"{path}: manifest must be schema v2 provenance-lock-index")
            continue
        manifest_entries = data.get("entries")
        if not isinstance(manifest_entries, list):
            errors.append(f"{path}: entries must be a list")
            continue
        slugs: set[str] = set()
        for entry in manifest_entries:
            if not isinstance(entry, dict):
                errors.append(f"{path}: entry must be a mapping")
                continue
            forbidden = MANIFEST_EXECUTABLE_FIELDS & set(entry)
            if forbidden:
                errors.append(
                    f"{path}: executable fields are forbidden: {', '.join(sorted(forbidden))}"
                )
            slugs.add(entry.get("slug"))
        if slugs != by_skill.get(skill_key, set()):
            errors.append(f"{path}: provenance entries do not match canonical content slugs")
    return errors


def tree_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def lint_generated_drift() -> list[str]:
    if not BUILD_ROOT.exists():
        return []
    from render_skills import _retained_workspace_scope, render_all

    retained_root = Path(
        tempfile.mkdtemp(prefix="stata-render-check-")
    ).resolve()
    try:
        with _retained_workspace_scope(
            retained_root,
            "generated-drift",
        ):
            expected_root = retained_root / "generated"
            render_all(output_root=expected_root)
            expected = tree_snapshot(expected_root)
            actual = tree_snapshot(BUILD_ROOT)
    except Exception as error:
        return [f"generated render failed: {error}"]
    if expected == actual:
        return []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        path for path in set(expected) & set(actual) if expected[path] != actual[path]
    )
    details: list[str] = []
    if missing:
        details.append(f"missing={','.join(missing)}")
    if extra:
        details.append(f"extra={','.join(extra)}")
    if changed:
        details.append(f"changed={','.join(changed)}")
    return [f"build/generated is stale ({'; '.join(details)})"]


def lint_repo(check_generated: bool = True) -> list[str]:
    errors: list[str] = []
    style_profile = load_documentation_style_profile()
    errors.extend(lint_documentation_style_profile(style_profile))
    if errors:
        return errors
    config = load_skill_config()
    errors.extend(lint_config(config, style_profile))
    if errors:
        return errors
    errors.extend(
        lint_documentation_style(
            style_profile,
            check_generated=False,
        )
    )
    if errors:
        return errors

    configured_dirs = {
        skill["content_dir"] for skill in config.get("skills", {}).values()
    }
    actual_dirs = {
        path.name for path in CONTENT_ROOT.iterdir() if path.is_dir()
    }
    if actual_dirs != configured_dirs:
        errors.append(
            "content/: configured content directories do not match filesystem directories"
        )
    orphan_files = [
        path
        for path in CONTENT_ROOT.rglob("*")
        if path.is_file() and path.suffix != ".yaml"
    ]
    for path in orphan_files:
        errors.append(f"{path}: orphan non-YAML content file")

    entries = iter_content_entries(CONTENT_ROOT, config)
    slugs: dict[str, str] = {}
    orders: dict[str, set[int]] = {
        skill_key: set() for skill_key in config["skills"]
    }
    route_paths: set[str] = set()
    route_triggers: dict[str, str] = {}
    repeated_text: Counter[str] = Counter()
    for skill_key, path, entry in entries:
        skill = config["skills"][skill_key]
        errors.extend(
            lint_entry(
                skill_key,
                path,
                entry,
                skill,
                style_profile,
            )
        )
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str):
            if slug in slugs:
                errors.append(f"{path}: duplicate global slug {slug} also used by {slugs[slug]}")
            else:
                slugs[slug] = skill_key
            route_path = f"{skill['name']}/{skill['route_dir']}/{slug}.md"
            route_paths.add(route_path)
            if (
                is_nonempty_string(entry.get("trigger"))
                and copy_text_within_limits(entry["trigger"])
            ):
                route_triggers[route_path] = entry["trigger"]
        order = entry.get("order")
        if isinstance(order, int) and not isinstance(order, bool):
            if order in orders[skill_key]:
                errors.append(f"{path}: duplicate order {order} within {skill_key}")
            orders[skill_key].add(order)
        for field in ("gotchas", "assumptions", "workflows"):
            for value in entry.get(field, []) if isinstance(entry.get(field), list) else []:
                if isinstance(value, str):
                    repeated_text[normalized_text(value)] += 1

    legacy_targets = {
        Path(alias.get("from_route", "")).stem: alias.get("to_slug")
        for alias in config.get("route_aliases", [])
        if isinstance(alias, dict)
    }
    for _, path, entry in entries:
        if not isinstance(entry, dict):
            continue
        for related in entry.get("related_refs", []):
            target = legacy_targets.get(related, related)
            if target not in slugs:
                errors.append(f"{path}: unresolved related reference {related}")

    for text, count in repeated_text.items():
        if text and count >= 3:
            errors.append(
                f"content/: repeated generic-looking text occurs {count} times: {text!r}"
            )

    alias_route_paths = {
        f"{config['skills'][alias['from_skill']]['name']}/{alias['from_route']}"
        for alias in config.get("route_aliases", [])
        if isinstance(alias, dict) and alias.get("from_skill") in config["skills"]
    }
    errors.extend(lint_route_aliases(config, slugs, route_paths))
    errors.extend(lint_routing_collisions(config, entries))
    errors.extend(
        lint_prompt_cases(
            config,
            route_paths,
            alias_route_paths,
            canonical_triggers=route_triggers,
        )
    )
    errors.extend(lint_upstream_lock(entries))
    errors.extend(lint_stata_help_lock(entries))
    errors.extend(lint_distribution_locks(entries))
    errors.extend(lint_manifests(entries))
    if check_generated and not errors:
        errors.extend(lint_generated_drift())
    if check_generated and not errors:
        errors.extend(
            lint_documentation_style(
                style_profile,
                check_generated=True,
            )
        )
    return errors


def _style_command_candidates(entry: dict) -> tuple[str, ...]:
    """Return conservative code-font candidates for one canonical reference."""

    candidates: set[str] = set()
    for command in entry.get("commands", []):
        if not isinstance(command, str):
            continue
        normalized = command.strip()
        if not normalized:
            continue
        if (
            normalized.casefold() in STYLE_COMMON_COMMAND_WORDS
            and re.fullmatch(r"[A-Za-z]+", normalized)
        ):
            continue
        candidates.add(normalized)
    return tuple(sorted(candidates, key=lambda value: (-len(value), value)))


def _style_generated_command_map(
    config: dict,
    entries: list[tuple[str, Path, dict]],
) -> dict[Path, tuple[str, ...]]:
    """Map generated canonical references to their declared commands."""

    result: dict[Path, tuple[str, ...]] = {}
    for skill_key, _, entry in entries:
        if not isinstance(entry, dict) or not is_safe_slug(entry.get("slug")):
            continue
        skill = config["skills"][skill_key]
        path = (
            BUILD_ROOT
            / skill["folder"]
            / skill["route_dir"]
            / f"{entry['slug']}.md"
        )
        result[path.resolve()] = _style_command_candidates(entry)
    return result


def _style_code_omission(
    visible: str,
    candidates: tuple[str, ...],
) -> str | None:
    """Return the first declared command that remains in visible prose."""

    for candidate in candidates:
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])",
            visible,
            re.IGNORECASE,
        ):
            return candidate
    return None


def markdown_style_advisories(
    path: Path,
    text: str,
    profile: dict,
    *,
    commands: tuple[str, ...] = (),
) -> list[str]:
    """Return non-blocking editorial candidates for one Markdown document."""

    label = _path_label(path)
    lines = protected_markdown_lines(text)
    advisory_config = profile.get("advisory", {})
    max_words = advisory_config.get("max_sentence_words", 25)
    advisories: list[str] = []
    pending_procedure: tuple[int, str] | None = None
    for line in lines:
        if line.heading_level is not None:
            normalized_heading = normalized_text(line.visible)
            pending_procedure = (
                (line.number, normalized_heading)
                if normalized_heading in STYLE_PROCEDURE_HEADINGS
                else None
            )
            continue

        visible = line.visible.strip()
        if not visible:
            continue
        if pending_procedure is not None:
            if line.block_kind == "bullet":
                advisories.append(
                    f"[Google style advisory] {label}:{line.number}: "
                    f"consider a numbered list for the "
                    f"{pending_procedure[1]!r} procedure"
                )
            pending_procedure = None
        if line.block_kind == "table":
            continue

        prose = visible
        for sentence in STYLE_SENTENCE_RE.split(prose):
            word_count = len(STYLE_WORD_RE.findall(sentence))
            if word_count > max_words:
                advisories.append(
                    f"[Google style advisory] {label}:{line.number}: "
                    f"sentence has {word_count} words (target at most {max_words})"
                )
            if word_count > 18 and (
                ";" in sentence or sentence.count(",") >= 4
            ):
                advisories.append(
                    f"[Google style advisory] {label}:{line.number}: "
                    "review the sentence for avoidable clause complexity"
                )
        if STYLE_PASSIVE_RE.search(prose):
            advisories.append(
                f"[Google style advisory] {label}:{line.number}: "
                "review possible passive voice"
            )
        if commands and (
            candidate := _style_code_omission(line.visible, commands)
        ):
            advisories.append(
                f"[Google style advisory] {label}:{line.number}: "
                f"consider code font for declared command {candidate!r}"
            )
    return advisories


def _bounded_advisory_report(
    advisories: list[str],
    max_items: int,
) -> StyleAdvisoryReport:
    """Bound displayed advisories without losing the true candidate count."""

    return StyleAdvisoryReport(
        items=tuple(advisories[:max_items]),
        total_count=len(advisories),
    )


def documentation_style_advisories(
    profile: dict,
    *,
    check_generated: bool,
) -> StyleAdvisoryReport:
    """Return bounded, explicitly non-blocking editorial candidates."""

    paths, path_errors = documentation_style_paths(
        profile,
        check_generated=check_generated,
    )
    if path_errors:
        items = tuple(
            f"[profile advisory unavailable] {error}" for error in path_errors
        )
        return StyleAdvisoryReport(items=items, total_count=len(items))
    config = load_skill_config()
    entries = iter_content_entries(CONTENT_ROOT, config)
    commands_by_path = _style_generated_command_map(config, entries)
    advisory_config = profile.get("advisory", {})
    max_items = advisory_config.get("max_report_items", 200)
    advisories: list[str] = []

    for path in paths:
        commands = commands_by_path.get(path.resolve(), ())
        advisories.extend(
            markdown_style_advisories(
                path,
                read_text(path),
                profile,
                commands=commands,
            )
        )

    return _bounded_advisory_report(advisories, max_items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-generated-check",
        action="store_true",
        help="Validate sources and locks without comparing an existing build directory.",
    )
    parser.add_argument(
        "--style-report",
        action="store_true",
        help=(
            "Print bounded editorial candidates after hard lint passes; "
            "advisories never change the exit status."
        ),
    )
    args = parser.parse_args(argv)
    check_generated = not args.no_generated_check
    errors = lint_repo(check_generated=check_generated)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    if args.style_report:
        profile = load_documentation_style_profile()
        report = documentation_style_advisories(
            profile,
            check_generated=check_generated,
        )
        print(
            "Documentation style advisory report "
            f"({report.total_count} total; {len(report.items)} shown; "
            "non-blocking candidates)"
        )
        for advisory in report.items:
            print(f"ADVISORY: {advisory}")
    print("Lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
