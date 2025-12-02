from __future__ import annotations

import sqlite3
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .entity_analysis import normalize_token
from .utils import get_internal_cache_path, get_user_settings_dir

_DB_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


def _store_path() -> Path:
    try:
        base_dir = Path(get_user_settings_dir())
    except ModuleNotFoundError:
        base_dir = Path(get_internal_cache_path("pronunciations"))
    target = base_dir / "pronunciations.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        try:
            legacy_dir = Path(get_internal_cache_path("pronunciations"))
            legacy_path = legacy_dir / "pronunciations.db"
            if legacy_path.exists() and legacy_path != target:
                shutil.move(str(legacy_path), target)
        except Exception:
            pass

    return target


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_store_path())
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized TEXT NOT NULL,
            token TEXT NOT NULL,
            language TEXT NOT NULL,
            pronunciation TEXT,
            voice TEXT,
            notes TEXT,
            context TEXT,
            usage_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(normalized, language)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
        """
    )
    row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "normalized": row["normalized"],
        "token": row["token"],
        "language": row["language"],
        "pronunciation": row["pronunciation"],
        "voice": row["voice"],
        "notes": row["notes"],
        "context": row["context"],
        "usage_count": row["usage_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def load_overrides(language: str, tokens: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    normalized_tokens = {normalize_token(token) for token in tokens if token}
    if not normalized_tokens:
        return {}
    # Use parameterized queries to prevent SQL injection
    placeholders = ",".join("?" for _ in normalized_tokens)
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT * FROM overrides WHERE language=? AND normalized IN ({placeholders})",
                (language, *normalized_tokens),
            )
            results: Dict[str, Dict[str, Any]] = {}
            for row in cursor.fetchall():
                payload = _row_to_dict(row)
                results[payload["normalized"]] = payload
            return results
        finally:
            conn.close()


def search_overrides(language: str, query: str, *, limit: int = 15) -> List[Dict[str, Any]]:
    if not query:
        return []
    pattern = f"%{query.lower()}%"
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM overrides
                WHERE language = ? AND (normalized LIKE ? OR LOWER(token) LIKE ?)
                ORDER BY usage_count DESC, updated_at DESC
                LIMIT ?
                """,
                (language, pattern, pattern, limit),
            )
            return [_row_to_dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


def save_override(
    *,
    language: str,
    token: str,
    pronunciation: Optional[str] = None,
    voice: Optional[str] = None,
    notes: Optional[str] = None,
    context: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = normalize_token(token)
    if not normalized:
        raise ValueError("Provide a token to override")
    timestamp = time.time()
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO overrides (normalized, token, language, pronunciation, voice, notes, context, usage_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(normalized, language) DO UPDATE SET
                    token=excluded.token,
                    pronunciation=excluded.pronunciation,
                    voice=excluded.voice,
                    notes=excluded.notes,
                    context=excluded.context,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized,
                    token,
                    language,
                    pronunciation,
                    voice,
                    notes,
                    context,
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM overrides WHERE normalized=? AND language=?",
                (normalized, language),
            ).fetchone()
            if row is None:  # pragma: no cover - defensive guard
                raise RuntimeError("Override save failed")
            return _row_to_dict(row)
        finally:
            conn.close()


def delete_override(*, language: str, token: str) -> None:
    normalized = normalize_token(token)
    if not normalized:
        return
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute("DELETE FROM overrides WHERE normalized=? AND language=?", (normalized, language))
            conn.commit()
        finally:
            conn.close()


def all_overrides(language: str) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM overrides WHERE language=? ORDER BY updated_at DESC",
                (language,),
            )
            return [_row_to_dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


def increment_usage(*, language: str, token: str, amount: int = 1) -> None:
    normalized = normalize_token(token)
    if not normalized:
        return
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "UPDATE overrides SET usage_count = usage_count + ?, updated_at = ? WHERE normalized=? AND language=?",
                (amount, time.time(), normalized, language),
            )
            conn.commit()
        finally:
            conn.close()


def get_override_stats(language: str) -> Dict[str, int]:
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN pronunciation IS NOT NULL AND pronunciation != '' THEN 1 END) as with_pronunciation,
                    COUNT(CASE WHEN voice IS NOT NULL AND voice != '' THEN 1 END) as with_voice
                FROM overrides 
                WHERE language=?
                """,
                (language,),
            )
            row = cursor.fetchone()
            return {
                "total": row[0],
                "filtered": row[0],
                "with_pronunciation": row[1],
                "with_voice": row[2],
            }
        finally:
            conn.close()
