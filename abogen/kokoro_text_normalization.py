from __future__ import annotations

import json
import re
import unicodedata
from fractions import Fraction
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
try:  # pragma: no cover - optional dependency guard
    from num2words import num2words
except Exception:  # pragma: no cover - graceful degradation
    num2words = None  # type: ignore

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from abogen.llm_client import LLMCompletion

from abogen.spacy_contraction_resolver import resolve_ambiguous_contractions

# ---------- Configuration Dataclass ----------

@dataclass
class ApostropheConfig:
    contraction_mode: str = "expand"              # expand|collapse|keep
    possessive_mode: str = "keep"                 # keep|collapse
    plural_possessive_mode: str = "collapse"      # keep|collapse
    irregular_possessive_mode: str = "keep"       # keep|expand (expand just means keep or add hints; modify if needed)
    sibilant_possessive_mode: str = "mark"        # keep|mark|approx
    fantasy_mode: str = "keep"                    # keep|mark|collapse_internal
    acronym_possessive_mode: str = "keep"         # keep|collapse_add_s
    decades_mode: str = "expand"                  # keep|expand
    leading_elision_mode: str = "expand"          # keep|expand
    ambiguous_past_modal_mode: str = "contextual" # keep|expand_prefer_would|expand_prefer_had|contextual
    add_phoneme_hints: bool = True                # Whether to emit markers like ‹IZ›
    fantasy_marker: str = "‹FAP›"                 # Marker inserted if fantasy_mode == mark
    sibilant_iz_marker: str = "‹IZ›"              # Marker for /ɪz/ insertion
    joiner: str = ""                              # Replacement used when collapsing internal apostrophes
    lowercase_for_matching: bool = True           # Normalize to lower for rule matching (not output)
    protect_cultural_names: bool = True           # Always keep O'Brien, D'Angelo, etc.
    convert_numbers: bool = True                  # Convert grouped numbers such as 12,500 to words
    number_lang: str = "en"                       # num2words language code

# ---------- Dictionaries / Patterns ----------

# Common contraction expansions (straightforward unambiguous)
CONTRACTIONS_EXACT = {
    "let's": "let us",
    "i'm": "i am",
    "you're": "you are",
    "we're": "we are",
    "they're": "they are",
    "i've": "i have",
    "you've": "you have",
    "we've": "we have",
    "they've": "they have",
    "i'll": "i will",
    "you'll": "you will",
    "he'll": "he will",
    "she'll": "she will",
    "we'll": "we will",
    "they'll": "they will",
    "can't": "can not",   # or "cannot"
    "won't": "will not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "haven't": "have not",
    "hasn't": "has not",
    "hadn't": "had not",
    "couldn't": "could not",
    "shouldn't": "should not",
    "wouldn't": "would not",
    "mustn't": "must not",
    "mightn't": "might not",
    "shan't": "shall not",
}

# For ambiguous 'd and 's we handle separately
_NUMBER_WITH_GROUP_RE = re.compile(r"(?<![\w\d])(-?\d{1,3}(?:,\d{3})+)(?![\w\d])")
_NUMBER_RANGE_SEPARATORS = "-‐‑–—−"
_NUMBER_RANGE_CLASS = re.escape(_NUMBER_RANGE_SEPARATORS)
_WIDE_RANGE_SEPARATORS = {"–", "—"}
_NUMBER_RANGE_RE = re.compile(
    rf"(?<!\w)(?P<left>-?\d+)(?P<sep>\s*[{_NUMBER_RANGE_CLASS}]\s*)(?P<right>-?\d+)(?![\w{_NUMBER_RANGE_CLASS}/])"
)
_FRACTION_SLASHES = "/⁄"
_FRACTION_SLASH_CLASS = re.escape(_FRACTION_SLASHES)
_FRACTION_RE = re.compile(
    rf"(?<!\w)(?P<numerator>-?\d+)\s*[{_FRACTION_SLASH_CLASS}]\s*(?P<denominator>-?\d+)(?![\w{_FRACTION_SLASH_CLASS}])"
)


def _int_to_words(value: int, language: str) -> Optional[str]:
    """Convert integer to spelled-out words using configured language."""
    if num2words is None:
        return None

    try:
        words = num2words(abs(value), lang=language)
    except Exception:  # pragma: no cover - unsupported locale
        return None

    if value < 0:
        return f"minus {words}"
    return words


def _int_to_ordinal_words(value: int, language: str) -> Optional[str]:
    if num2words is None:
        return None

    try:
        return num2words(value, lang=language, ordinal=True)
    except Exception:  # pragma: no cover - unsupported locale
        return None


def _pluralize_fraction_word(base: str) -> str:
    if base == "half":
        return "halves"
    if base == "calf":  # defensive; unlikely but keeps pattern predictable
        return "calves"
    if base.endswith("f"):
        return base[:-1] + "ves"
    if base.endswith("fe"):
        return base[:-2] + "ves"
    return base + "s"


def _fraction_denominator_word(denominator: int, numerator: int, language: str) -> Optional[str]:
    """Return spoken form for fraction denominator respecting plurality."""
    if denominator == 0:
        return None

    numerator_abs = abs(numerator)
    if denominator == 1:
        return ""
    if denominator == 2:
        return "half" if numerator_abs == 1 else "halves"
    if denominator == 4:
        return "quarter" if numerator_abs == 1 else "quarters"

    base = _int_to_ordinal_words(denominator, language)
    if base is None:
        return None
    if numerator_abs == 1:
        return base
    return _pluralize_fraction_word(base)


def _format_fraction_words(numerator: int, denominator: int, language: str) -> Optional[str]:
    """Return spoken representation of a simple fraction."""
    if denominator == 0:
        return None

    fraction = Fraction(numerator, denominator)
    num = fraction.numerator
    den = fraction.denominator

    if abs(den) > 100:
        return None

    numerator_words = _int_to_words(abs(num), language)
    if numerator_words is None:
        return None

    denom_word = _fraction_denominator_word(den, num, language)
    if denom_word is None:
        return None

    if denom_word:
        if num < 0:
            numerator_words = f"minus {numerator_words}"
        return f"{numerator_words} {denom_word}".strip()

    # If denominator collapses to 1, just speak the integer value.
    spoken = _int_to_words(num, language)
    return spoken


def _replace_number_range(match: re.Match[str], language: str) -> str:
    left_raw = match.group("left")
    right_raw = match.group("right")
    separator_text = match.group("sep") or ""
    separator_char = next((ch for ch in separator_text if ch in _NUMBER_RANGE_SEPARATORS), "-")
    has_whitespace = any(ch.isspace() for ch in separator_text)

    left_digits = len(left_raw.lstrip("-"))
    right_digits = len(right_raw.lstrip("-"))

    if (left_digits >= 4 or right_digits >= 4) and separator_char not in _WIDE_RANGE_SEPARATORS and not has_whitespace:
        return match.group(0)
    if {left_digits, right_digits} == {3, 4} and separator_char not in _WIDE_RANGE_SEPARATORS and not has_whitespace:
        return match.group(0)
    try:
        left = int(left_raw)
        right = int(right_raw)
    except ValueError:
        return match.group(0)

    left_words = _int_to_words(left, language)
    right_words = _int_to_words(right, language)
    if not left_words or not right_words:
        return match.group(0)

    return f"{left_words} to {right_words}"


def _replace_fraction(match: re.Match[str], language: str) -> str:
    numerator_raw = match.group("numerator")
    denominator_raw = match.group("denominator")
    try:
        numerator = int(numerator_raw)
        denominator = int(denominator_raw)
    except ValueError:
        return match.group(0)

    spoken = _format_fraction_words(numerator, denominator, language)
    if not spoken:
        return match.group(0)
    return spoken
AMBIGUOUS_D_BASES = {"i","you","he","she","we","they"}
AMBIGUOUS_S_BASES = {"it","that","what","where","who","when","how","there","here"}


def _is_ambiguous_d(token: str) -> bool:
    low = token.lower()
    return low.endswith("'d") and low[:-2] in AMBIGUOUS_D_BASES


def _is_ambiguous_s(token: str) -> bool:
    low = token.lower()
    return low.endswith("'s") and low[:-2] in AMBIGUOUS_S_BASES

# Irregular possessives that are not formed by simple + 's logic
IRREGULAR_POSSESSIVES = {
    "children's": "children's",
    "men's": "men's",
    "women's": "women's",
    "people's": "people's",
    "geese's": "geese's",
    "mouse's": "mouse's",   # singular irregular
}

SIBILANT_END_RE = re.compile(r"(?:[sxz]|(?:ch|sh))$", re.IGNORECASE)

DECADE_RE = re.compile(r"^'\d0s$", re.IGNORECASE)          # '90s, '80s
LEADING_ELISION = {
    "'tis": "it is",
    "'twas": "it was",
    "'cause": "because",
    "'em": "them",
    "'round": "around",
    "'til": "until",
}

CULTURAL_NAME_PATTERNS = [
    re.compile(r"^O'[A-Z][a-z]+$"),
    re.compile(r"^D'[A-Z][a-z]+$"),
    re.compile(r"^L'[A-Za-z].*$"),
    re.compile(r"^Mc[A-Z].*$"),   # not apostrophe, but often relevant (kept anyway)
]

ACRONYM_POSSESSIVE_RE = re.compile(r"^[A-Z]{2,}'s$")

INTERNAL_APOSTROPHE_RE = re.compile(r"[A-Za-z]'.+[A-Za-z]")  # apostrophe not at edge

# Capture contiguous runs of Unicode letters/digits/apostrophes/hyphens, otherwise fall back to
# single-character tokens (punctuation, symbols, etc.).
WORD_TOKEN_RE = re.compile(
    r"[0-9A-Za-z'’\u00C0-\u1FFF\u2C00-\uD7FF\-]+|[^0-9A-Za-z\s]",
    re.UNICODE,
)

APOSTROPHE_CHARS = "’`´ꞌʼ"

TERMINAL_PUNCTUATION = {".", "?", "!", "…", ";", ":"}
CLOSING_PUNCTUATION = '"\'”’)]}»›'
ELLIPSIS_SUFFIXES = ("...", "…")
_LINE_SPLIT_RE = re.compile(r"(\n+)")

TITLE_ABBREVIATIONS = {
    "mr": "mister",
    "mrs": "missus",
    "ms": "miz",
    "dr": "doctor",
    "prof": "professor",
    "rev": "reverend",
}

SUFFIX_ABBREVIATIONS = {
    "jr": "junior",
    "sr": "senior",
}

_TITLE_PATTERN = re.compile(
    r"\b(?P<abbr>" + "|".join(sorted(TITLE_ABBREVIATIONS.keys(), key=len, reverse=True)) + r")\.",
    re.IGNORECASE,
)
_SUFFIX_PATTERN = re.compile(
    r"\b(?P<abbr>" + "|".join(sorted(SUFFIX_ABBREVIATIONS.keys(), key=len, reverse=True)) + r")\.",
    re.IGNORECASE,
)

# ---------- Utility Functions ----------

def normalize_unicode_apostrophes(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for ch in APOSTROPHE_CHARS:
        text = text.replace(ch, "'")
    return text

def tokenize(text: str) -> List[str]:
    # Simple tokenization preserving punctuation tokens
    return WORD_TOKEN_RE.findall(text)


def tokenize_with_spans(text: str) -> List[Tuple[str, int, int]]:
    return [(match.group(0), match.start(), match.end()) for match in WORD_TOKEN_RE.finditer(text)]


def _cleanup_spacing(text: str) -> str:
    if not text:
        return text

    for marker in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"):
        text = text.replace(marker, "")

    # Collapse spaces before closing punctuation.
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    text = re.sub(r"\s+([’\"”»›)\]\}])", r"\1", text)

    # Remove spaces directly after opening punctuation/quotes.
    text = re.sub(r"([«‹“‘\"'(\[\{])\s+", r"\1", text)

    # Ensure spaces exist after sentence punctuation when followed by a word/quote.
    text = re.sub(r"([,.;:!?%])(?![\s”'\"’»›)])", r"\1 ", text)
    text = re.sub(r"([”\"’])(?![\s.,;:!?\"”’»›)])", r"\1 ", text)

    # Tighten hyphen/em dash spacing between word characters.
    text = re.sub(r"(?<=\w)\s*([-–—])\s*(?=\w)", r"\1", text)

    # Normalize multiple spaces.
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


_ROMAN_VALUE_MAP = {
    "I": 1,
    "V": 5,
    "X": 10,
    "L": 50,
    "C": 100,
    "D": 500,
    "M": 1000,
}

_ROMAN_COMPOSE_ORDER = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]

_ROMAN_PREFIX_RE = re.compile(r"^(?P<roman>[IVXLCDM]+)(?P<sep>[\s\.:,;\-–—]*)", re.IGNORECASE)


def _roman_to_int(token: str) -> Optional[int]:
    if not token:
        return None
    total = 0
    prev = 0
    token_upper = token.upper()
    for char in reversed(token_upper):
        value = _ROMAN_VALUE_MAP.get(char)
        if value is None:
            return None
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    if total <= 0:
        return None
    if _int_to_roman(total) != token_upper:
        return None
    return total


def _int_to_roman(value: int) -> str:
    parts: List[str] = []
    remaining = value
    for amount, symbol in _ROMAN_COMPOSE_ORDER:
        while remaining >= amount:
            parts.append(symbol)
            remaining -= amount
    return "".join(parts)


def normalize_roman_numeral_titles(
    titles: Sequence[str],
    *,
    threshold: float = 0.5,
) -> List[str]:
    if not titles:
        return []

    normalized: List[str] = []
    matches: List[Tuple[int, str, int, str, str]] = []
    non_empty = 0

    for index, raw in enumerate(titles):
        title = "" if raw is None else str(raw)
        stripped = title.lstrip()
        leading_ws = title[: len(title) - len(stripped)]
        if not stripped:
            normalized.append(title)
            continue

        non_empty += 1
        match = _ROMAN_PREFIX_RE.match(stripped)
        if not match:
            normalized.append(title)
            continue

        roman_token = match.group("roman")
        separator = match.group("sep") or ""
        rest = stripped[match.end():]

        if not separator and rest and rest[:1].isalnum():
            normalized.append(title)
            continue

        numeric_value = _roman_to_int(roman_token)
        if numeric_value is None:
            normalized.append(title)
            continue

        matches.append((index, leading_ws, numeric_value, separator, rest))
        normalized.append(title)

    if not matches or non_empty == 0:
        return list(normalized)

    if len(matches) <= non_empty * threshold:
        return list(normalized)

    output = list(normalized)
    for idx, leading_ws, value, separator, rest in matches:
        new_title = f"{leading_ws}{value}"
        if separator:
            new_title += separator
        elif rest and not rest[0].isspace() and rest[0] not in ".-–—:;,":
            new_title += " "
        new_title += rest
        output[idx] = new_title

    return output


def _match_casing(template: str, replacement: str) -> str:
    if template.isupper():
        return replacement.upper()
    if template[:1].isupper() and template[1:].islower():
        return replacement.capitalize()
    if template[:1].isupper():
        # Mixed case (e.g., Mc), fall back to title case
        return replacement.capitalize()
    return replacement


def expand_titles_and_suffixes(text: str) -> str:
    def _replace(match: re.Match[str], mapping: dict[str, str]) -> str:
        abbr = match.group("abbr")
        lookup = mapping.get(abbr.lower())
        if not lookup:
            return match.group(0)
        return _match_casing(abbr, lookup)

    text = _TITLE_PATTERN.sub(lambda m: _replace(m, TITLE_ABBREVIATIONS), text)
    text = _SUFFIX_PATTERN.sub(lambda m: _replace(m, SUFFIX_ABBREVIATIONS), text)
    return text


def ensure_terminal_punctuation(text: str) -> str:
    def _amend(segment: str) -> str:
        if not segment or not segment.strip():
            return segment

        stripped = segment.rstrip()
        trailing_ws = segment[len(stripped) :]

        match = re.match(rf"^(.*?)([{re.escape(CLOSING_PUNCTUATION)}]*)$", stripped)
        if not match:
            return segment

        body, closers = match.groups()
        if not body:
            return segment

        normalized_body = body.rstrip()
        trailing_body_ws = body[len(normalized_body) :]

        if normalized_body.endswith(ELLIPSIS_SUFFIXES):
            return normalized_body + trailing_body_ws + closers + trailing_ws

        last_char = normalized_body[-1]
        if last_char in TERMINAL_PUNCTUATION:
            return normalized_body + trailing_body_ws + closers + trailing_ws

        return normalized_body + "." + trailing_body_ws + closers + trailing_ws

    parts = _LINE_SPLIT_RE.split(text)
    amended: List[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("\n"):
            amended.append(part)
        else:
            amended.append(_amend(part))
    if not parts:
        return _amend(text)
    return "".join(amended)


def is_cultural_name(token: str, cfg: ApostropheConfig) -> bool:
    if not cfg.protect_cultural_names:
        return False
    for pat in CULTURAL_NAME_PATTERNS:
        if pat.match(token):
            return True
    return False

def classify_token(token: str, cfg: ApostropheConfig) -> Tuple[str, str]:
    """
    Classify apostrophe usage and propose normalized form.
    Returns (category, normalized_token_or_same).
    Categories: contraction, ambiguous_contraction_s, ambiguous_contraction_d,
                plural_possessive, irregular_possessive, sibilant_possessive,
                singular_possessive, acronym_possessive, decade, leading_elision,
                fantasy_internal, other
    """
    if "'" not in token:
        return "other", token

    raw = token
    low = token.lower()

    # 1. Decades
    if DECADE_RE.match(token):
        if cfg.decades_mode == "expand":
            # '90s -> 1990s (you could also choose 90s)
            return "decade", f"19{token[2:4]}s"
        return "decade", token

    # 2. Leading elision
    if low in LEADING_ELISION:
        if cfg.leading_elision_mode == "expand":
            return "leading_elision", LEADING_ELISION[low]
        return "leading_elision", token

    # 3. Ambiguous 'd contractions
    if _is_ambiguous_d(token):
        base = low[:-2]
        mode = cfg.ambiguous_past_modal_mode
        if cfg.contraction_mode == "collapse":
            return "ambiguous_contraction_d", base + "d"
        if cfg.contraction_mode == "expand":
            if mode == "expand_prefer_would":
                return "ambiguous_contraction_d", base + " would"
            if mode == "expand_prefer_had":
                return "ambiguous_contraction_d", base + " had"
            if mode == "contextual":
                return "ambiguous_contraction_d", base + " would"
        return "ambiguous_contraction_d", token

    # 4. Ambiguous 's contractions
    if _is_ambiguous_s(token):
        base = low[:-2]
        if cfg.contraction_mode == "expand":
            return "ambiguous_contraction_s", base + " is"
        if cfg.contraction_mode == "collapse":
            return "ambiguous_contraction_s", base + "s"
        return "ambiguous_contraction_s", token

    # 5. Exact contraction
    if low in CONTRACTIONS_EXACT:
        if cfg.contraction_mode == "expand":
            return "contraction", CONTRACTIONS_EXACT[low]
        elif cfg.contraction_mode == "collapse":
            # collapse: remove apostrophe only (he's -> hes)
            return "contraction", low.replace("'", "")
        else:
            return "contraction", token

    # 6. Irregular possessives (keep or expand logic)
    if low in IRREGULAR_POSSESSIVES:
        if cfg.irregular_possessive_mode == "keep":
            return "irregular_possessive", token
        else:
            # 'expand': we might keep same or optionally add marker
            return "irregular_possessive", token

    # 7. Plural possessive pattern dogs'
    if re.match(r"^[A-Za-z0-9]+s'$", token):
        if cfg.plural_possessive_mode == "collapse":
            return "plural_possessive", token[:-1]  # remove trailing apostrophe
        return "plural_possessive", token

    # 8. Acronym possessive NASA's
    if ACRONYM_POSSESSIVE_RE.match(token):
        if cfg.acronym_possessive_mode == "collapse_add_s":
            return "acronym_possessive", token.replace("'", "")
        return "acronym_possessive", token

    # 9. Sibilant singular possessive boss's, church's
    if low.endswith("'s"):
        base = token[:-2]
        if SIBILANT_END_RE.search(base):
            if cfg.sibilant_possessive_mode == "keep":
                return "sibilant_possessive", token
            elif cfg.sibilant_possessive_mode == "approx":
                # convert to base + "es" (boss's -> bosses)
                # risk: loses possessive semantics visually
                return "sibilant_possessive", base + "es"
            elif cfg.sibilant_possessive_mode == "mark":
                # remove apostrophe, add IZ marker
                normalized = base
                if cfg.add_phoneme_hints:
                    normalized += cfg.sibilant_iz_marker
                else:
                    normalized += "es"
                return "sibilant_possessive", normalized

    # 10. Generic singular possessive (\w+'s)
    if re.match(r"^[A-Za-z0-9]+'s$", token):
        if cfg.possessive_mode == "collapse":
            # Just remove apostrophe
            return "singular_possessive", token.replace("'", "")
        return "singular_possessive", token

    # 11. Cultural names or fantasy internal
    if is_cultural_name(token, cfg):
        return "cultural_name", token

    # 12. Fantasy internal apostrophes
    if INTERNAL_APOSTROPHE_RE.search(token):
        if cfg.fantasy_mode == "keep":
            return "fantasy_internal", token
        elif cfg.fantasy_mode == "mark":
            out = token + (cfg.fantasy_marker if cfg.add_phoneme_hints else "")
            return "fantasy_internal", out
        elif cfg.fantasy_mode == "collapse_internal":
            # Remove internal apostrophes only
            inner = re.sub(r"(?<=\w)'+(?=\w)", cfg.joiner, token)
            return "fantasy_internal", inner

    # 13. Fallback: treat as other (maybe stray apostrophe)
    if cfg.fantasy_mode == "collapse_internal":
        # Remove any internal apostrophes
        return "other", token.replace("'", cfg.joiner)
    return "other", token

def normalize_apostrophes(text: str, cfg: ApostropheConfig | None = None) -> Tuple[str, List[Tuple[str,str,str]]]:
    """
    Normalize apostrophes per config.
    Returns normalized text AND a list of (original_token, category, normalized_token)
    so you can debug or post-process (e.g., apply phoneme replacement for ‹IZ›).
    """
    if cfg is None:
        cfg = ApostropheConfig()

    text = normalize_unicode_apostrophes(text)
    text = _normalize_grouped_numbers(text, cfg)
    token_entries = tokenize_with_spans(text)

    use_contextual_s = cfg.contraction_mode == "expand"
    use_contextual_d = cfg.contraction_mode == "expand" and cfg.ambiguous_past_modal_mode == "contextual"

    need_contextual = False
    if (use_contextual_s or use_contextual_d) and token_entries:
        for token_value, _, _ in token_entries:
            if use_contextual_s and _is_ambiguous_s(token_value):
                need_contextual = True
                break
            if use_contextual_d and _is_ambiguous_d(token_value):
                need_contextual = True
                break

    contextual_resolutions = resolve_ambiguous_contractions(text) if need_contextual else {}

    results: List[Tuple[str, str, str]] = []
    normalized_tokens: List[str] = []

    for tok, start, end in token_entries:
        category, norm = classify_token(tok, cfg)

        resolution = contextual_resolutions.get((start, end)) if contextual_resolutions else None
        if resolution is not None:
            if resolution.category == "ambiguous_contraction_s" and use_contextual_s:
                category = resolution.category
                norm = resolution.expansion
            elif resolution.category == "ambiguous_contraction_d" and use_contextual_d:
                category = resolution.category
                norm = resolution.expansion

        results.append((tok, category, norm))
        normalized_tokens.append(norm)

    filtered = [token for token in normalized_tokens if token]
    normalized_text = _cleanup_spacing(" ".join(filtered))
    return normalized_text, results

def _normalize_grouped_numbers(text: str, cfg: ApostropheConfig) -> str:
    if not text or not cfg.convert_numbers:
        return text

    language = (cfg.number_lang or "en").strip() or "en"

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        cleaned = token.replace(",", "")
        if not cleaned:
            return token
        negative = cleaned.startswith("-")
        cleaned_digits = cleaned[1:] if negative else cleaned

        if not cleaned_digits.isdigit():
            return cleaned_digits if not negative else f"-{cleaned_digits}"

        if num2words is None:
            return ("-" if negative else "") + cleaned_digits

        try:
            value = int(cleaned)
        except ValueError:
            return cleaned

        words = _int_to_words(value, language)
        if not words:
            return str(value)
        return words

    normalized = _NUMBER_WITH_GROUP_RE.sub(_replace, text)
    normalized = _NUMBER_RANGE_RE.sub(lambda m: _replace_number_range(m, language), normalized)
    normalized = _FRACTION_RE.sub(lambda m: _replace_fraction(m, language), normalized)
    return normalized

# ---------- Optional phoneme hint post-processing ----------

def apply_phoneme_hints(text: str, iz_marker="‹IZ›") -> str:
    """
    Replace markers with an orthographic sequence that
    your phonemizer will reliably convert to /ɪz/.
    """
    return text.replace(iz_marker, " iz")


DEFAULT_APOSTROPHE_CONFIG = ApostropheConfig()


_MUSTACHE_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")
_LLM_SYSTEM_PROMPT = (
    "You assist with audiobook preparation. Review the sentence, identify any apostrophes or "
    "contractions that should be expanded for clarity, and respond by calling the "
    "apply_regex_replacements tool. Each replacement must target a single token, include a precise "
    "regex pattern, and provide the exact replacement text. If no changes are required, call the tool "
    "with an empty replacements list. Do not rewrite the sentence directly."
)

_LLM_REGEX_TOOL_NAME = "apply_regex_replacements"
_LLM_REGEX_TOOL = {
    "type": "function",
    "function": {
        "name": _LLM_REGEX_TOOL_NAME,
        "description": (
            "Return regex substitutions to normalize apostrophes or contractions in the provided sentence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "replacements": {
                    "description": "Ordered substitutions to apply to the sentence.",
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Regular expression that matches the token to replace.",
                            },
                            "replacement": {
                                "type": "string",
                                "description": "Replacement text for the match.",
                            },
                            "flags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional re flags such as IGNORECASE.",
                            },
                            "count": {
                                "type": "integer",
                                "description": "Optional maximum number of replacements (default all).",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Short explanation of why the replacement is needed.",
                            },
                        },
                        "required": ["pattern", "replacement"],
                    },
                }
            },
            "required": ["replacements"],
        },
    },
}
_LLM_REGEX_TOOL_CHOICE = {"type": "function", "function": {"name": _LLM_REGEX_TOOL_NAME}}
_LLM_ALLOWED_REGEX_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}


def _render_mustache(template: str, context: Mapping[str, str]) -> str:
    if not template:
        return ""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, "")

    return _MUSTACHE_PATTERN.sub(_replace, template)


_SENTENCE_CAPTURE_RE = re.compile(r"[^.!?]+[.!?]+|[^.!?]+$", re.MULTILINE)


def _split_sentences_for_llm(text: str) -> List[str]:
    sentences = [segment.strip() for segment in _SENTENCE_CAPTURE_RE.findall(text or "")]
    return [segment for segment in sentences if segment]


def _normalize_with_llm(
    text: str,
    *,
    settings: Mapping[str, Any],
    config: ApostropheConfig,
) -> str:
    from abogen.normalization_settings import build_llm_configuration, DEFAULT_LLM_PROMPT
    from abogen.llm_client import generate_completion, LLMClientError

    llm_config = build_llm_configuration(settings)
    if not llm_config.is_configured():
        raise LLMClientError("LLM configuration is incomplete")

    prompt_template = str(settings.get("llm_prompt") or DEFAULT_LLM_PROMPT)
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    normalized_lines: List[str] = []
    for raw_line in lines:
        newline = ""
        if raw_line.endswith(("\r", "\n")):
            stripped_newline = raw_line.rstrip("\r\n")
            newline = raw_line[len(stripped_newline):]
            line_body = stripped_newline
        else:
            line_body = raw_line

        if not line_body.strip():
            normalized_lines.append(line_body + newline)
            continue

        leading_ws = line_body[: len(line_body) - len(line_body.lstrip())]
        trailing_ws = line_body[len(line_body.rstrip()):]
        core = line_body[len(leading_ws) : len(line_body) - len(trailing_ws)]

        sentences = _split_sentences_for_llm(core)
        if not sentences:
            normalized_lines.append(line_body + newline)
            continue

        paragraph_context = core
        rewritten_sentences: List[str] = []
        for sentence in sentences:
            prompt_context = {
                "text": sentence,
                "sentence": sentence,
                "paragraph": paragraph_context,
            }
            prompt = _render_mustache(prompt_template, prompt_context)
            completion = generate_completion(
                llm_config,
                system_message=_LLM_SYSTEM_PROMPT,
                user_message=prompt,
                tools=[_LLM_REGEX_TOOL],
                tool_choice=_LLM_REGEX_TOOL_CHOICE,
            )
            rewritten_sentences.append(
                _apply_llm_regex_replacements(sentence, completion)
            )

        normalized_core = " ".join(filter(None, rewritten_sentences)) or core

        rebuilt = f"{leading_ws}{normalized_core}{trailing_ws}{newline}"
        normalized_lines.append(rebuilt)

    result = "".join(normalized_lines)
    return result if result else text


def _apply_llm_regex_replacements(sentence: str, completion: "LLMCompletion") -> str:
    replacements = _extract_llm_replacements(completion)
    if not replacements:
        return sentence

    updated = sentence
    for spec in replacements:
        updated = _apply_single_regex_replacement(updated, spec)
    return updated


def _extract_llm_replacements(completion: "LLMCompletion") -> List[Dict[str, Any]]:
    if completion is None:
        return []

    for call in getattr(completion, "tool_calls", ()):  # type: ignore[attr-defined]
        if getattr(call, "name", None) != _LLM_REGEX_TOOL_NAME:
            continue
        payload = _safe_load_json(getattr(call, "arguments", None))
        replacements = _coerce_replacement_list(payload)
        if replacements:
            return replacements

    if getattr(completion, "content", None):
        payload = _safe_load_json(completion.content)
        replacements = _coerce_replacement_list(payload)
        if replacements:
            return replacements

    return []


def _safe_load_json(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _coerce_replacement_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, Mapping):
        candidates = raw.get("replacements")
    else:
        candidates = raw

    if not isinstance(candidates, list):
        return []

    replacements: List[Dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        replacement = str(item.get("replacement") or "")
        entry: Dict[str, Any] = {"pattern": pattern, "replacement": replacement}

        flags = _normalize_flag_field(item.get("flags"))
        if flags:
            entry["flags"] = flags

        count = item.get("count")
        if isinstance(count, int) and count >= 0:
            entry["count"] = count

        replacements.append(entry)

    return replacements


def _normalize_flag_field(raw: Any) -> List[str]:
    if not raw:
        return []

    if isinstance(raw, str):
        raw_iterable: Iterable[Any] = [raw]
    elif isinstance(raw, Iterable) and not isinstance(raw, (bytes, str, Mapping)):
        raw_iterable = raw
    else:
        return []

    normalized: List[str] = []
    seen: set[str] = set()
    for value in raw_iterable:
        candidate = str(value or "").strip().upper()
        if not candidate or candidate not in _LLM_ALLOWED_REGEX_FLAGS or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _apply_single_regex_replacement(text: str, spec: Mapping[str, Any]) -> str:
    pattern = str(spec.get("pattern") or "")
    replacement = str(spec.get("replacement") or "")
    if not pattern:
        return text

    flags_value = 0
    flag_names = spec.get("flags")
    if isinstance(flag_names, str):
        flag_iterable: Iterable[Any] = [flag_names]
    elif isinstance(flag_names, Iterable) and not isinstance(flag_names, (bytes, str, Mapping)):
        flag_iterable = flag_names
    else:
        flag_iterable = []

    for flag_name in flag_iterable:
        lookup = str(flag_name or "").strip().upper()
        flags_value |= _LLM_ALLOWED_REGEX_FLAGS.get(lookup, 0)

    count = spec.get("count")
    count_value = count if isinstance(count, int) and count >= 0 else 0

    try:
        return re.sub(pattern, replacement, text, count=count_value, flags=flags_value)
    except re.error:
        return text


def normalize_for_pipeline(
    text: str,
    *,
    config: Optional[ApostropheConfig] = None,
    settings: Optional[Mapping[str, Any]] = None,
) -> str:
    """Normalize text for the synthesis pipeline with runtime settings."""

    from abogen.normalization_settings import build_apostrophe_config, get_runtime_settings
    from abogen.llm_client import LLMClientError

    runtime_settings = settings or get_runtime_settings()
    base_config = config or DEFAULT_APOSTROPHE_CONFIG
    cfg = build_apostrophe_config(settings=runtime_settings, base=base_config)

    mode = str(runtime_settings.get("normalization_apostrophe_mode", "spacy")).lower()
    normalized = text

    if mode == "off":
        normalized = normalize_unicode_apostrophes(text)
        if cfg.convert_numbers:
            normalized = _normalize_grouped_numbers(normalized, cfg)
        normalized = _cleanup_spacing(normalized)
    elif mode == "llm":
        try:
            normalized = _normalize_with_llm(text, settings=runtime_settings, config=cfg)
        except LLMClientError:
            raise
        if cfg.convert_numbers:
            normalized = _normalize_grouped_numbers(normalized, cfg)
        normalized = _cleanup_spacing(normalized)
    else:
        normalized, _ = normalize_apostrophes(text, cfg)

    if runtime_settings.get("normalization_titles", True):
        normalized = expand_titles_and_suffixes(normalized)
    if runtime_settings.get("normalization_terminal", True):
        normalized = ensure_terminal_punctuation(normalized)

    if cfg.add_phoneme_hints:
        normalized = apply_phoneme_hints(normalized, iz_marker=cfg.sibilant_iz_marker)

    return normalized

# ---------- Example Usage ----------

if __name__ == "__main__":
    sample = "Bob's boss's chair. The dogs' collars. It's cold. Ta'veren and Sha'hal. O'Brien's code in the '90s. Boss's orders."
    config = ApostropheConfig()
    norm_text, details = normalize_apostrophes(sample, config)
    norm_text = apply_phoneme_hints(norm_text)
    print("Original:", sample)
    print("Normalized:", norm_text)
    for orig, cat, norm in details:
        print(f"{orig:15} -> {norm:15} [{cat}]")