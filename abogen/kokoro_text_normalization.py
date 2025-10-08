from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Iterable, Callable

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
    ambiguous_past_modal_mode: str = "keep"       # keep|expand_prefer_would|expand_prefer_had
    add_phoneme_hints: bool = True                # Whether to emit markers like ‹IZ›
    fantasy_marker: str = "‹FAP›"                 # Marker inserted if fantasy_mode == mark
    sibilant_iz_marker: str = "‹IZ›"              # Marker for /ɪz/ insertion
    joiner: str = ""                              # Replacement used when collapsing internal apostrophes
    lowercase_for_matching: bool = True           # Normalize to lower for rule matching (not output)
    protect_cultural_names: bool = True           # Always keep O'Brien, D'Angelo, etc.

# ---------- Dictionaries / Patterns ----------

# Common contraction expansions (straightforward unambiguous)
CONTRACTIONS_EXACT = {
    "it's": "it is",
    "that's": "that is",
    "what's": "what is",
    "where's": "where is",
    "who's": "who is",
    "when's": "when is",
    "how's": "how is",
    "there's": "there is",
    "here's": "here is",
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
    "i'd": "i would",     # ambiguous (had/would), treat default
    "you'd": "you would",
    "he'd": "he would",
    "she'd": "she would",
    "we'd": "we would",
    "they'd": "they would",
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
AMBIGUOUS_D_BASES = {"i","you","he","she","we","they"}
AMBIGUOUS_S_BASES = {"it","that","what","where","who","when","how","there","here"}

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

WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9'’]+|[^A-Za-z0-9\s]")

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

    # 3. Exact contraction
    if low in CONTRACTIONS_EXACT:
        if cfg.contraction_mode == "expand":
            return "contraction", CONTRACTIONS_EXACT[low]
        elif cfg.contraction_mode == "collapse":
            # collapse: remove apostrophe only (it's -> its)
            return "contraction", low.replace("'", "")
        else:
            return "contraction", token

    # 4. Ambiguous 'd
    if low.endswith("'d"):
        base = low[:-2]
        if base in AMBIGUOUS_D_BASES:
            if cfg.ambiguous_past_modal_mode == "expand_prefer_would":
                return "ambiguous_contraction_d", base + " would"
            elif cfg.ambiguous_past_modal_mode == "expand_prefer_had":
                return "ambiguous_contraction_d", base + " had"
            elif cfg.contraction_mode == "collapse":
                return "ambiguous_contraction_d", base + "d"
            return "ambiguous_contraction_d", token

    # 5. Ambiguous 's
    if low.endswith("'s"):
        base = low[:-2]
        if base in AMBIGUOUS_S_BASES:
            # treat as contraction 'is' under chosen mode
            if cfg.contraction_mode == "expand":
                return "ambiguous_contraction_s", base + " is"
            elif cfg.contraction_mode == "collapse":
                return "ambiguous_contraction_s", base + "s"
            else:
                return "ambiguous_contraction_s", token

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
    tokens = tokenize(text)

    results = []
    normalized_tokens: List[str] = []

    for tok in tokens:
        category, norm = classify_token(tok, cfg)
        results.append((tok, category, norm))
        normalized_tokens.append(norm)

    # Simple rejoin heuristic:
    # If token is purely punctuation, attach without extra space.
    out_parts = []
    for i, (orig, cat, norm) in enumerate(results):
        if i == 0:
            out_parts.append(norm)
            continue
        prev = results[i-1][2]
        if re.match(r"^[.,;:!?)]$", norm):
            # Attach to previous
            out_parts[-1] = out_parts[-1] + norm
        elif re.match(r"^[(]$", norm):
            out_parts.append(norm)
        else:
            # Normal separation
            if not (re.match(r"^[.,;:!?)]$", prev) or prev.endswith("—")):
                out_parts.append(" " + norm)
            else:
                out_parts.append(norm)
    normalized_text = "".join(out_parts)
    return normalized_text, results

# ---------- Optional phoneme hint post-processing ----------

def apply_phoneme_hints(text: str, iz_marker="‹IZ›") -> str:
    """
    Replace markers with an orthographic sequence that
    your phonemizer will reliably convert to /ɪz/.
    """
    return text.replace(iz_marker, " iz")

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