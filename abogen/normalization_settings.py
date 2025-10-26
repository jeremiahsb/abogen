from __future__ import annotations

import os
from dataclasses import replace
from functools import lru_cache
from typing import Any, Dict, Mapping, Optional

from abogen.kokoro_text_normalization import ApostropheConfig
from abogen.llm_client import LLMConfiguration
from abogen.utils import load_config

DEFAULT_LLM_PROMPT = (
    "You are assisting with audiobook preparation. Analyze the sentence and identify any apostrophes or "
    "contractions that should be expanded for clarity. Call the apply_regex_replacements tool with precise "
    "regex substitutions for only the words that need adjustment. If no changes are required, return an empty list.\n"
    "Sentence: {{ sentence }}"
)

_SETTINGS_DEFAULTS: Dict[str, Any] = {
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "llm_timeout": 30.0,
    "llm_prompt": DEFAULT_LLM_PROMPT,
    "llm_context_mode": "sentence",
    "normalization_numbers": True,
    "normalization_titles": True,
    "normalization_terminal": True,
    "normalization_phoneme_hints": True,
    "normalization_apostrophe_mode": "spacy",
}

_ENVIRONMENT_KEYS: Dict[str, str] = {
    "llm_base_url": "ABOGEN_LLM_BASE_URL",
    "llm_api_key": "ABOGEN_LLM_API_KEY",
    "llm_model": "ABOGEN_LLM_MODEL",
    "llm_timeout": "ABOGEN_LLM_TIMEOUT",
    "llm_prompt": "ABOGEN_LLM_PROMPT",
    "llm_context_mode": "ABOGEN_LLM_CONTEXT_MODE",
}

NORMALIZATION_SAMPLE_TEXTS: Dict[str, str] = {
    "apostrophes": "I've heard the captain'll arrive by dusk, but they'd said the same yesterday.",
    "numbers": "The ledger listed 1,204 outstanding debts totaling $57,890.",
    "titles": "Dr. Smith met Mr. O'Leary outside St. John's Church on Jan. 4th.",
    "punctuation": "Meet me at the docks tonight We'll decide then",  # missing punctuation
}


@lru_cache(maxsize=1)
def _environment_defaults() -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for key, env_var in _ENVIRONMENT_KEYS.items():
        default = _SETTINGS_DEFAULTS.get(key)
        if default is None:
            continue
        value = os.environ.get(env_var)
        if value is None or value == "":
            continue
        if isinstance(default, bool):
            overrides[key] = _coerce_bool(value, default)
        elif isinstance(default, float):
            overrides[key] = _coerce_float(value, float(default))
        else:
            overrides[key] = value
    return overrides


def environment_llm_defaults() -> Dict[str, Any]:
    return dict(_environment_defaults())


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_settings(source: Mapping[str, Any]) -> Dict[str, Any]:
    env_defaults = _environment_defaults()
    extracted: Dict[str, Any] = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if key in source:
            raw_value = source.get(key)
        elif key in env_defaults:
            raw_value = env_defaults[key]
        else:
            raw_value = default
        if isinstance(default, bool):
            extracted[key] = _coerce_bool(raw_value, default)
        elif isinstance(default, float):
            extracted[key] = _coerce_float(raw_value, default)
        else:
            extracted[key] = str(raw_value or "") if isinstance(default, str) else raw_value
    return extracted


@lru_cache(maxsize=1)
def _cached_settings() -> Dict[str, Any]:
    config = load_config() or {}
    return _extract_settings(config)


def get_runtime_settings() -> Dict[str, Any]:
    return dict(_cached_settings())


def clear_cached_settings() -> None:
    _cached_settings.cache_clear()


def build_apostrophe_config(
    *,
    settings: Mapping[str, Any],
    base: Optional[ApostropheConfig] = None,
) -> ApostropheConfig:
    config = replace(base or ApostropheConfig())
    config.convert_numbers = bool(settings.get("normalization_numbers", True))
    config.add_phoneme_hints = bool(settings.get("normalization_phoneme_hints", True))
    return config


def build_llm_configuration(settings: Mapping[str, Any]) -> LLMConfiguration:
    return LLMConfiguration(
        base_url=str(settings.get("llm_base_url") or ""),
        api_key=str(settings.get("llm_api_key") or ""),
        model=str(settings.get("llm_model") or ""),
        timeout=_coerce_float(settings.get("llm_timeout"), float(_SETTINGS_DEFAULTS["llm_timeout"])),
    )


def apply_overrides(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(base)
    for key, value in overrides.items():
        if key not in _SETTINGS_DEFAULTS:
            continue
        merged[key] = value
    return merged
