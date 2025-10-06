import json
import os
from typing import Dict, Iterable, List, Tuple

from abogen.constants import VOICES_INTERNAL
from abogen.utils import get_user_config_path


def _get_profiles_path():
    config_path = get_user_config_path()
    config_dir = os.path.dirname(config_path)
    return os.path.join(config_dir, "voice_profiles.json")


def load_profiles():
    """Load all voice profiles from JSON file."""
    path = _get_profiles_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # always expect abogen_voice_profiles wrapper
                if isinstance(data, dict) and "abogen_voice_profiles" in data:
                    return data["abogen_voice_profiles"]
                # fallback: treat as profiles dict
                if isinstance(data, dict):
                    return data
        except Exception:
            return {}
    return {}


def save_profiles(profiles):
    """Save all voice profiles to JSON file."""
    path = _get_profiles_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        # always save with abogen_voice_profiles wrapper
        json.dump({"abogen_voice_profiles": profiles}, f, indent=2)


def delete_profile(name):
    """Remove a profile by name."""
    profiles = load_profiles()
    if name in profiles:
        del profiles[name]
        save_profiles(profiles)


def duplicate_profile(src, dest):
    """Duplicate an existing profile."""
    profiles = load_profiles()
    if src in profiles and dest:
        profiles[dest] = profiles[src]
        save_profiles(profiles)


def export_profiles(export_path):
    """Export all profiles to specified JSON file."""
    profiles = load_profiles()
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump({"abogen_voice_profiles": profiles}, f, indent=2)


def serialize_profiles() -> Dict[str, Dict[str, Iterable[Tuple[str, float]]]]:
    """Return profiles in canonical dictionary form."""
    return load_profiles()


def _normalize_voice_entries(entries: Iterable) -> List[Tuple[str, float]]:
    normalized: List[Tuple[str, float]] = []
    for item in entries or []:
        if isinstance(item, dict):
            voice = item.get("id") or item.get("voice")
            weight = item.get("weight")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            voice, weight = item[0], item[1]
        else:
            continue
        if voice not in VOICES_INTERNAL:
            continue
        if weight is None:
            continue
        try:
            weight_val = float(weight)
        except (TypeError, ValueError):
            continue
        if weight_val <= 0:
            continue
        normalized.append((voice, weight_val))
    return normalized


def normalize_voice_entries(entries: Iterable) -> List[Tuple[str, float]]:
    """Public helper to normalize voice-weight pairs from arbitrary payloads."""

    return _normalize_voice_entries(entries)


def save_profile(name: str, *, language: str, voices: Iterable) -> None:
    """Persist a single profile after validating its data."""

    name = (name or "").strip()
    if not name:
        raise ValueError("Profile name is required")

    normalized = _normalize_voice_entries(voices)
    if not normalized:
        raise ValueError("At least one voice with a weight above zero is required")

    if not language:
        language = "a"

    profiles = load_profiles()
    profiles[name] = {"language": language, "voices": normalized}
    save_profiles(profiles)


def remove_profile(name: str) -> None:
    delete_profile(name)


def import_profiles_data(data: Dict, *, replace_existing: bool = False) -> List[str]:
    """Merge profiles from a dictionary structure and persist them.

    Returns the list of profile names that were added or updated.
    """

    if not isinstance(data, dict):
        raise ValueError("Invalid profile payload")

    if "abogen_voice_profiles" in data:
        data = data["abogen_voice_profiles"]

    if not isinstance(data, dict):
        raise ValueError("Invalid profile payload")

    current = load_profiles()
    updated: List[str] = []
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        voices = _normalize_voice_entries(entry.get("voices", []))
        if not voices:
            continue
        language = entry.get("language", "a")
        if name in current and not replace_existing:
            # skip duplicates unless explicit replacement is requested
            continue
        current[name] = {"language": language, "voices": voices}
        updated.append(name)

    if updated:
        save_profiles(current)
    return updated


def export_profiles_payload(names: Iterable[str] | None = None) -> Dict[str, Dict]:
    """Return profiles limited to the provided names for download/export."""

    profiles = load_profiles()
    if names is None:
        subset = profiles
    else:
        subset = {name: profiles[name] for name in names if name in profiles}
    return {"abogen_voice_profiles": subset}
