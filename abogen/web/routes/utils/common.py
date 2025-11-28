from typing import Any, Optional, Tuple, Iterable, List
from pathlib import Path

def split_profile_spec(value: Any) -> Tuple[str, Optional[str]]:
    text = str(value or "").strip()
    if not text:
        return "", None
    if text.lower().startswith("profile:"):
        _, _, remainder = text.partition(":")
        name = remainder.strip()
        return "", name or None
    return text, None

def existing_paths(paths: Optional[Iterable[Path]]) -> List[Path]:
    if not paths:
        return []
    return [p for p in paths if p.exists()]
