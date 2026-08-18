#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
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
STYLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
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
STYLE_ENTITY_RE = re.compile(
    r"&(?:#(?:[0-9]+|[xX][0-9A-Fa-f]+)|[A-Za-z][A-Za-z0-9]+);"
)
STYLE_MARKDOWN_PARSER = MarkdownIt("commonmark", {"html": True}).enable("table")
STYLE_RAW_AMPERSAND_RE = re.compile(
    r"&(?!#(?:[0-9]+|[xX][0-9A-Fa-f]+);|[A-Za-z][A-Za-z0-9]+;)"
)
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
    links: tuple[str, ...] = ()
    missing_html_alt: int = 0
    heading_level: int | None = None
    heading_has_content: bool = False
    protected_heading_prefix: bool = False
    block_kind: str | None = None


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
            rf"(?<![A-Za-z0-9+.-]){re.escape(normalized_name)}"
            rf"(?![A-Za-z0-9+.-])",
            normalized_value,
        ):
            proper_spans.append(proper_match.span())

    previous_end = 0
    for index, match in enumerate(STYLE_WORD_RE.finditer(normalized_value)):
        word = match.group(0)
        reviewed_name = any(
            start <= match.start() and match.end() <= end
            for start, end in proper_spans
        )
        if not reviewed_name and any(
            segment and segment[0].isupper()
            for segment in word.split("-")[1:]
        ):
            return word
        initial_position = index == 0 or ":" in normalized_value[
            previous_end : match.start()
        ]
        if not reviewed_name and initial_position:
            if word[0].islower() and word.casefold() not in allowed_initials:
                return word
            letters = [character for character in word if character.isalpha()]
            if len(letters) > 1 and all(character.isupper() for character in letters):
                return word
        elif not reviewed_name and word[0].isupper() and not any(
            character.isdigit() for character in word
        ):
            return word
        previous_end = match.end()
    return None


def _mask_jinja(value: str) -> str:
    """Mask Jinja expressions and control blocks inside visible prose."""

    return re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", " ", value, flags=re.DOTALL)


def _mask_frontmatter_and_entities(text: str) -> str:
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
    return STYLE_ENTITY_RE.sub(
        lambda match: " " * len(match.group(0)),
        masked,
    )


class _StyleHTMLScanner(HTMLParser):
    """Expose HTML text while protecting tags, attributes, and literal code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.visible: list[str] = []
        self.links: list[str] = []
        self.missing_alt = 0
        self._literal_tags: list[str] = []
        self._anchors: list[list[str]] = []

    @property
    def in_literal(self) -> bool:
        return bool(self._literal_tags)

    def add_text(self, value: str) -> None:
        if self.in_literal:
            return
        self.visible.append(value)
        for anchor in self._anchors:
            anchor.append(value)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.casefold()
        if normalized in STYLE_HTML_LITERAL_TAGS:
            self._literal_tags.append(normalized)
            return
        if self.in_literal:
            return
        if normalized == "a" and any(
            name.casefold() == "href" for name, _ in attrs
        ):
            self._anchors.append([])
        elif normalized == "img" and not any(
            name.casefold() == "alt" for name, _ in attrs
        ):
            self.missing_alt += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in STYLE_HTML_LITERAL_TAGS:
            if self._literal_tags and self._literal_tags[-1] == normalized:
                self._literal_tags.pop()
            return
        if self.in_literal:
            return
        if normalized == "a" and self._anchors:
            self.links.append("".join(self._anchors.pop()))

    def handle_data(self, data: str) -> None:
        self.add_text(data)

    def handle_entityref(self, name: str) -> None:
        self.add_text(" ")

    def handle_charref(self, name: str) -> None:
        self.add_text(" ")


def _inline_style_data(
    token: Token,
) -> tuple[str, tuple[str, ...], int]:
    """Extract visible prose and link metadata from one inline token."""

    scanner = _StyleHTMLScanner()
    markdown_links: list[tuple[list[str], bool]] = []
    resolved_links: list[str] = []

    def add_text(value: str) -> None:
        masked = _mask_jinja(value)
        scanner.add_text(masked)
        if scanner.in_literal:
            return
        for parts, suppressed in markdown_links:
            if not suppressed:
                parts.append(masked)

    for child in token.children or []:
        if child.type == "text":
            add_text(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            add_text("\n")
        elif child.type == "html_inline":
            scanner.feed(child.content)
        elif child.type == "link_open":
            markdown_links.append(([], scanner.in_literal))
        elif child.type == "link_close" and markdown_links:
            parts, suppressed = markdown_links.pop()
            if not suppressed:
                resolved_links.append("".join(parts))
        elif child.type in {"code_inline", "image"}:
            continue
    scanner.close()
    return (
        "".join(scanner.visible),
        tuple(resolved_links + scanner.links),
        scanner.missing_alt,
    )


def _html_block_style_data(value: str) -> tuple[str, tuple[str, ...], int]:
    scanner = _StyleHTMLScanner()
    scanner.feed(value)
    scanner.close()
    return "".join(scanner.visible), tuple(scanner.links), scanner.missing_alt


def protected_markdown_lines(text: str) -> list[StyleMarkdownLine]:
    """Parse CommonMark and return only reader-visible prose blocks."""

    parsed_text = _mask_frontmatter_and_entities(text)
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
            visible, links, missing_alt = _inline_style_data(token)
        else:
            visible, links, missing_alt = _html_block_style_data(token.content)
        raw_heading = token.content.lstrip() if heading_level is not None else ""
        result.append(
            StyleMarkdownLine(
                number=start + 1,
                raw=raw,
                visible=visible,
                links=links,
                missing_html_alt=missing_alt,
                heading_level=heading_level,
                heading_has_content=bool(raw_heading.strip()),
                protected_heading_prefix=raw_heading.casefold().startswith(
                    ("`", "{{", "{%", "{#", "<code", "<kbd", "<pre")
                ),
                block_kind=block_kind,
            )
        )
    return result


def _plain_link_text(value: str) -> str:
    """Normalize link text for deterministic vague-text checks."""

    value = re.sub(r"[*_~]", "", value)
    return " ".join(value.split()).casefold()


def _path_label(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def lint_markdown_document(
    path: Path,
    text: str,
    profile: dict,
) -> list[str]:
    """Apply objective structure and Google-style checks to one document."""

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

    for line in lines:
        if line.heading_level is not None:
            level = line.heading_level
            if not line.heading_has_content:
                errors.append(
                    f"[CommonMark/project] {label}:{line.number}: empty heading"
                )
            headings.append((line.number, level))
            visible_body = line.visible.strip()
            if visible_body:
                heading_initials = set(lowercase_initials)
                if line.protected_heading_prefix:
                    first_visible = STYLE_WORD_RE.search(visible_body)
                    if first_visible:
                        heading_initials.add(first_visible.group(0).casefold())
                bad_word = sentence_case_error(
                    visible_body,
                    proper_names=proper_names,
                    lowercase_initials=heading_initials,
                )
                if bad_word:
                    errors.append(
                        f"[Google style] {label}:{line.number}: heading must use "
                        f"sentence case; unexpected {bad_word!r}"
                    )

        for link_text in line.links:
            normalized = _plain_link_text(link_text)
            if normalized in vague_links or re.fullmatch(r"https?://\S+", normalized):
                errors.append(
                    f"[Google style] {label}:{line.number}: use descriptive link "
                    f"text instead of {link_text!r}"
                )

        for _ in range(line.missing_html_alt):
            errors.append(
                f"[Google style] {label}:{line.number}: image requires an alt attribute"
            )

        if STYLE_RAW_AMPERSAND_RE.search(line.visible):
            errors.append(
                f"[Google style] {label}:{line.number}: use 'and' instead of an "
                "ampersand in prose"
            )
        for phrase in discouraged:
            if re.search(rf"\b{re.escape(phrase)}\b", line.visible, re.IGNORECASE):
                errors.append(
                    f"[Google style] {label}:{line.number}: replace deterministic "
                    f"directional phrase {phrase!r}"
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


def documentation_style_advisories(
    profile: dict,
    *,
    check_generated: bool,
) -> list[str]:
    """Return bounded, explicitly non-blocking editorial candidates."""

    paths, path_errors = documentation_style_paths(
        profile,
        check_generated=check_generated,
    )
    if path_errors:
        return [
            f"[profile advisory unavailable] {error}"
            for error in path_errors
        ]
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

    if len(advisories) > max_items:
        omitted = len(advisories) - max_items
        advisories = advisories[:max_items]
        advisories.append(
            f"[Google style advisory] {omitted} additional candidates omitted by "
            "the reviewed report limit"
        )
    return advisories


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
        advisories = documentation_style_advisories(
            profile,
            check_generated=check_generated,
        )
        print(
            "Documentation style advisory report "
            f"({len(advisories)} non-blocking candidates)"
        )
        for advisory in advisories:
            print(f"ADVISORY: {advisory}")
    print("Lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
