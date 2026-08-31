"""P1 scope-aware notes storage and runtime integration.

This module replaces the legacy global note registry/search path with a
scope-aware SQLite repository.  It intentionally leaves the WebUI Notes API
fail-closed because those HTTP routes do not carry a trusted AstrBot event.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

QUARANTINE_SCOPE = "__quarantine__"
PUBLIC_SCOPE = "public"
SCOPED_NOTE_ROOT = ".angel/scoped"
SCOPED_NOTE_DB_NAME = "scoped_notes.sqlite3"
SCOPED_NOTE_SCHEMA_VERSION = 1
SCOPED_NOTE_BOOTSTRAP_KEY = "raw_bootstrap_v1"
MAX_SCOPE_LENGTH = 128
MAX_SEARCH_LIMIT = 200
MAX_FALLBACK_CANDIDATES = 5000

_LOGGER = logging.getLogger(__name__)
_INSTALL_LOCK = threading.RLock()
_INSTALLED = False
_CURRENT_NOTE_SCOPE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "angel_memory_current_note_scope",
    default=None,
)


class NoteScopeError(ValueError):
    """Raised when a note operation cannot establish a safe memory scope."""


class NotePathError(ValueError):
    """Raised when a note path is invalid or leaves the configured raw root."""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_scope(value: Any, *, allow_quarantine: bool = False) -> str:
    scope = _clean_text(value)
    if not scope:
        raise NoteScopeError("memory_scope 不能为空")
    if len(scope) > MAX_SCOPE_LENGTH:
        raise NoteScopeError("memory_scope 过长")
    if any(ch in scope for ch in ("\x00", "\r", "\n", "/", "\\")):
        raise NoteScopeError("memory_scope 包含非法字符")
    if scope == QUARANTINE_SCOPE and not allow_quarantine:
        raise NoteScopeError("隔离域不得用于普通笔记读写")
    return scope


def readable_scopes(current_scope: Any) -> Tuple[str, ...]:
    current = normalize_scope(current_scope)
    if current == PUBLIC_SCOPE:
        return (PUBLIC_SCOPE,)
    return (current, PUBLIC_SCOPE)


def scope_is_readable(record_scope: Any, current_scope: Any) -> bool:
    try:
        record = normalize_scope(record_scope, allow_quarantine=True)
        return record in readable_scopes(current_scope)
    except NoteScopeError:
        return False


def scope_is_owned(record_scope: Any, current_scope: Any) -> bool:
    try:
        record = normalize_scope(record_scope, allow_quarantine=True)
        current = normalize_scope(current_scope)
    except NoteScopeError:
        return False
    return record == current


def scope_to_segment(scope: Any) -> str:
    normalized = normalize_scope(scope, allow_quarantine=True)
    encoded = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii")
    return "s-" + encoded.rstrip("=")


def segment_to_scope(segment: Any) -> str:
    text = _clean_text(segment)
    if not text.startswith("s-"):
        raise NoteScopeError("非法 scope 路径片段")
    payload = text[2:]
    if not payload or not re.fullmatch(r"[A-Za-z0-9_-]+", payload):
        raise NoteScopeError("非法 scope 路径片段")
    padded = payload + "=" * ((4 - len(payload) % 4) % 4)
    try:
        scope = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise NoteScopeError("scope 路径片段无法解码") from exc
    scope = normalize_scope(scope, allow_quarantine=True)
    if scope_to_segment(scope) != text:
        raise NoteScopeError("scope 路径片段不是规范编码")
    return scope


def normalize_relative_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text or "\x00" in text:
        raise NotePathError("笔记相对路径不能为空")
    if re.match(r"^[A-Za-z]:", text):
        raise NotePathError("笔记路径不得包含 Windows 驱动器前缀")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise NotePathError("笔记路径不得为绝对路径")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise NotePathError("笔记路径包含越界片段")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized.startswith("../") or normalized == "..":
        raise NotePathError("笔记路径越界")
    return normalized


def scoped_relative_directory(scope: Any) -> str:
    return f"{SCOPED_NOTE_ROOT}/{scope_to_segment(scope)}/note"


def infer_scope_from_relative_path(relative_path: Any) -> Optional[str]:
    try:
        normalized = normalize_relative_path(relative_path)
    except NotePathError:
        return None
    parts = PurePosixPath(normalized).parts
    root_parts = PurePosixPath(SCOPED_NOTE_ROOT).parts
    if len(parts) < len(root_parts) + 3:
        return None
    if tuple(parts[: len(root_parts)]) != tuple(root_parts):
        return None
    if parts[len(root_parts) + 1] != "note":
        return None
    try:
        return segment_to_scope(parts[len(root_parts)])
    except NoteScopeError:
        return None


def note_path_matches_scope(relative_path: Any, memory_scope: Any) -> bool:
    """Verify that the physical scoped directory agrees with DB metadata."""

    try:
        scope = normalize_scope(memory_scope, allow_quarantine=True)
    except NoteScopeError:
        return False
    inferred = infer_scope_from_relative_path(relative_path)
    if scope == QUARANTINE_SCOPE:
        return inferred in (None, QUARANTINE_SCOPE)
    return inferred == scope


def resolve_note_scope(explicit_scope: Any = None) -> str:
    candidate = explicit_scope
    if candidate is None or not _clean_text(candidate):
        candidate = _CURRENT_NOTE_SCOPE.get()
    if candidate is None or not _clean_text(candidate):
        raise NoteScopeError("缺少可信 memory_scope，笔记操作已拒绝")
    return normalize_scope(candidate)


def _normalize_search_text(text: Any) -> str:
    result = _clean_text(text).lower()
    result = re.sub(
        r"[\uff01-\uff5e]",
        lambda match: chr(ord(match.group(0)) - 0xFEE0),
        result,
    )
    result = result.replace("\u3000", " ")
    return result


def _query_terms(query: Any) -> List[str]:
    normalized = _normalize_search_text(query)
    if not normalized:
        return []
    terms = [
        item.strip()
        for item in re.split(r"\s*\|\s*|\s+", normalized)
        if item.strip()
    ]
    return list(dict.fromkeys(terms))


def _search_fragments(text: Any) -> List[str]:
    normalized = _normalize_search_text(text)
    if not normalized:
        return []
    fragments: List[str] = []
    seen: set[str] = set()
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", normalized):
        if re.search(r"[\u4e00-\u9fff]", part):
            sizes = (1, 2) if len(part) > 1 else (1,)
            for size in sizes:
                for index in range(max(0, len(part) - size + 1)):
                    fragment = part[index : index + size]
                    if fragment and fragment not in seen:
                        seen.add(fragment)
                        fragments.append(fragment)
        elif part not in seen:
            seen.add(part)
            fragments.append(part)
    return fragments


def _required_match_fragments(query: Any) -> List[str]:
    fragments: List[str] = []
    seen: set[str] = set()
    for term in _query_terms(query):
        if re.search(r"[\u4e00-\u9fff]", term) and len(term) > 2:
            values = [term[index : index + 2] for index in range(len(term) - 1)]
        else:
            values = [term]
        for value in values:
            if value and value not in seen:
                seen.add(value)
                fragments.append(value)
    return fragments


def build_search_text(content: Any, source_file_path: Any = "") -> str:
    fragments = _search_fragments(
        f"{_clean_text(source_file_path)} {_clean_text(content)}"
    )
    return " ".join(fragments)


def _fts_query_for(query: Any) -> str:
    tokens = _search_fragments(query)[:64]
    quoted = ['"' + token.replace('"', '""') + '"' for token in tokens if token]
    return " OR ".join(quoted)


def _score_candidate(candidate: Mapping[str, Any], query: Any) -> float:
    content = _normalize_search_text(candidate.get("content"))
    path = _normalize_search_text(candidate.get("source_file_path"))
    haystack = f"{path}\n{content}"
    terms = _query_terms(query)
    required = _required_match_fragments(query)
    exact_hits = sum(1 for term in terms if term and term in haystack)
    fragment_hits = sum(1 for fragment in required if fragment in haystack)
    if terms and exact_hits == 0 and fragment_hits == 0:
        return -1.0
    raw_rank = float(candidate.get("_rank", 0.0) or 0.0)
    bm25_bonus = max(0.0, -raw_rank)
    own_scope_bonus = 0.05 if candidate.get("_is_own_scope") else 0.0
    return exact_hits * 10.0 + fragment_hits + bm25_bonus + own_scope_bonus


class ScopedNoteRepository:
    """Thread-safe scope-aware note registry and chunk search repository."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connections: Dict[int, sqlite3.Connection] = {}
        self._fts_available = False
        try:
            self._init_db()
        except Exception:
            self.close()
            raise

    def _get_conn(self) -> sqlite3.Connection:
        thread_id = threading.get_ident()
        with self._lock:
            conn = self._connections.get(thread_id)
            if conn is None:
                conn = sqlite3.connect(
                    self.db_path,
                    timeout=30,
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                self._connections[thread_id] = conn
            return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scoped_note_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            version_row = conn.execute(
                "SELECT value FROM scoped_note_meta WHERE key = 'schema_version'"
            ).fetchone()
            existing_files_table = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'scoped_note_files'
                LIMIT 1
                """
            ).fetchone()
            if version_row is not None:
                current_version = str(version_row["value"])
                if current_version != str(SCOPED_NOTE_SCHEMA_VERSION):
                    raise RuntimeError(
                        "不支持的 scoped notes 数据库版本: "
                        f"{current_version} (需要 {SCOPED_NOTE_SCHEMA_VERSION})"
                    )
            elif existing_files_table is not None:
                raise RuntimeError(
                    "检测到没有 schema_version 的 scoped notes 数据库；"
                    "为避免误判旧结构，已拒绝自动写入"
                )

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scoped_note_files (
                    note_short_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL CHECK(length(file_id) > 0),
                    memory_scope TEXT NOT NULL CHECK(length(memory_scope) > 0),
                    source_file_path TEXT NOT NULL UNIQUE
                        CHECK(length(source_file_path) > 0),
                    heading_h1 TEXT NOT NULL DEFAULT '',
                    total_lines INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK(state IN ('active', 'quarantine', 'missing'))
                        CHECK(
                            (memory_scope = '__quarantine__'
                                AND state IN ('quarantine', 'missing'))
                            OR
                            (memory_scope != '__quarantine__'
                                AND state IN ('active', 'missing'))
                        )
                );

                CREATE TABLE IF NOT EXISTS scoped_note_chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_short_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    memory_scope TEXT NOT NULL,
                    source_file_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(note_short_id)
                        REFERENCES scoped_note_files(note_short_id)
                        ON DELETE CASCADE,
                    UNIQUE(note_short_id, chunk_index)
                );

                CREATE INDEX IF NOT EXISTS idx_scoped_note_files_scope_state
                    ON scoped_note_files(memory_scope, state, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_scoped_note_files_path
                    ON scoped_note_files(source_file_path);
                CREATE INDEX IF NOT EXISTS idx_scoped_note_chunks_scope
                    ON scoped_note_chunks(memory_scope, file_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_scoped_note_chunks_note
                    ON scoped_note_chunks(note_short_id, chunk_index);

                CREATE TRIGGER IF NOT EXISTS trg_scoped_note_chunks_insert
                BEFORE INSERT ON scoped_note_chunks
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM scoped_note_files AS f
                    WHERE f.note_short_id = NEW.note_short_id
                      AND f.file_id = NEW.file_id
                      AND f.memory_scope = NEW.memory_scope
                      AND f.source_file_path = NEW.source_file_path
                      AND f.state = 'active'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'scoped note chunk ownership mismatch');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_scoped_note_chunks_update
                BEFORE UPDATE ON scoped_note_chunks
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM scoped_note_files AS f
                    WHERE f.note_short_id = NEW.note_short_id
                      AND f.file_id = NEW.file_id
                      AND f.memory_scope = NEW.memory_scope
                      AND f.source_file_path = NEW.source_file_path
                      AND f.state = 'active'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'scoped note chunk ownership mismatch');
                END;
                """
            )
            if version_row is None:
                conn.execute(
                    "INSERT INTO scoped_note_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCOPED_NOTE_SCHEMA_VERSION),),
                )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS scoped_note_chunks_fts
                    USING fts5(search_text, tokenize='unicode61')
                    """
                )
                self._fts_available = True
            except sqlite3.OperationalError as exc:
                self._fts_available = False
                _LOGGER.warning(
                    "SQLite FTS5 不可用，scoped notes 将降级为本地扫描: %s",
                    exc,
                )
            conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._get_conn().execute(
            "SELECT value FROM scoped_note_meta WHERE key = ?",
            (_clean_text(key),),
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO scoped_note_meta(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (_clean_text(key), str(value)),
            )
            conn.commit()

    def _delete_chunks_for_note(
        self,
        conn: sqlite3.Connection,
        note_short_id: int,
    ) -> None:
        chunk_rows = conn.execute(
            "SELECT chunk_id FROM scoped_note_chunks WHERE note_short_id = ?",
            (int(note_short_id),),
        ).fetchall()
        chunk_ids = [int(row["chunk_id"]) for row in chunk_rows]
        if self._fts_available and chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            conn.execute(
                f"DELETE FROM scoped_note_chunks_fts WHERE rowid IN ({placeholders})",
                tuple(chunk_ids),
            )
        conn.execute(
            "DELETE FROM scoped_note_chunks WHERE note_short_id = ?",
            (int(note_short_id),),
        )

    def upsert_file(
        self,
        *,
        memory_scope: Any,
        file_id: Any,
        source_file_path: Any,
        heading_h1: Any,
        total_lines: int,
        updated_at: int,
        content: str,
        chunks: Sequence[Mapping[str, Any]],
    ) -> Dict[str, int]:
        scope = normalize_scope(memory_scope, allow_quarantine=True)
        normalized_path = normalize_relative_path(source_file_path)
        normalized_file_id = _clean_text(file_id)
        if not normalized_file_id:
            raise ValueError("file_id 不能为空")
        if not note_path_matches_scope(normalized_path, scope):
            raise NotePathError(
                "笔记路径编码与 memory_scope 不一致，拒绝写入 scoped 数据库"
            )
        state = "quarantine" if scope == QUARANTINE_SCOPE else "active"
        digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        normalized_chunks = [
            {
                "chunk_index": int(item.get("chunk_index", index)),
                "line_start": max(0, int(item.get("line_start", 0))),
                "line_end": max(0, int(item.get("line_end", 0))),
                "content": str(item.get("content") or "").strip(),
            }
            for index, item in enumerate(chunks or [])
            if str(item.get("content") or "").strip()
        ]
        if state == "quarantine":
            # Keep only file-level evidence.  Legacy private bodies must not be
            # copied into an active/searchable chunk database before ownership
            # is explicitly classified.
            normalized_chunks = []

        with self._lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row_by_path = conn.execute(
                    """
                    SELECT note_short_id, memory_scope
                    FROM scoped_note_files
                    WHERE source_file_path = ?
                    """,
                    (normalized_path,),
                ).fetchone()
                if row_by_path is not None and str(row_by_path["memory_scope"]) != scope:
                    raise sqlite3.IntegrityError(
                        "existing note path belongs to a different memory_scope"
                    )

                note_short_id = (
                    int(row_by_path["note_short_id"])
                    if row_by_path is not None
                    else None
                )
                if note_short_id is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO scoped_note_files(
                            file_id, memory_scope, source_file_path,
                            heading_h1, total_lines, updated_at,
                            content_sha256, state
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_file_id,
                            scope,
                            normalized_path,
                            "" if state == "quarantine" else _clean_text(heading_h1),
                            max(0, int(total_lines)),
                            int(updated_at),
                            digest,
                            state,
                        ),
                    )
                    note_short_id = int(cursor.lastrowid)
                else:
                    conn.execute(
                        """
                        UPDATE scoped_note_files
                        SET file_id = ?,
                            source_file_path = ?,
                            heading_h1 = ?,
                            total_lines = ?,
                            updated_at = ?,
                            content_sha256 = ?,
                            state = ?
                        WHERE note_short_id = ?
                        """,
                        (
                            normalized_file_id,
                            normalized_path,
                            "" if state == "quarantine" else _clean_text(heading_h1),
                            max(0, int(total_lines)),
                            int(updated_at),
                            digest,
                            state,
                            int(note_short_id),
                        ),
                    )

                self._delete_chunks_for_note(conn, int(note_short_id))
                now = time.time()
                inserted = 0
                for item in normalized_chunks:
                    search_text = build_search_text(
                        item["content"],
                        normalized_path,
                    )
                    cursor = conn.execute(
                        """
                        INSERT INTO scoped_note_chunks(
                            note_short_id, file_id, memory_scope,
                            source_file_path, chunk_index, line_start,
                            line_end, content, search_text, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(note_short_id),
                            normalized_file_id,
                            scope,
                            normalized_path,
                            item["chunk_index"],
                            item["line_start"],
                            item["line_end"],
                            item["content"],
                            search_text,
                            now,
                        ),
                    )
                    chunk_id = int(cursor.lastrowid)
                    if self._fts_available and state == "active" and search_text:
                        conn.execute(
                            """
                            INSERT INTO scoped_note_chunks_fts(rowid, search_text)
                            VALUES(?, ?)
                            """,
                            (chunk_id, search_text),
                        )
                    inserted += 1

                conn.commit()
                return {
                    "note_short_id": int(note_short_id),
                    "chunks": inserted,
                }
            except Exception:
                conn.rollback()
                raise

    def mark_missing_by_file_id(self, file_id: Any) -> int:
        normalized_file_id = _clean_text(file_id)
        if not normalized_file_id:
            return 0
        with self._lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT note_short_id
                    FROM scoped_note_files
                    WHERE file_id = ? AND state != 'missing'
                    """,
                    (normalized_file_id,),
                ).fetchall()
                for row in rows:
                    self._delete_chunks_for_note(
                        conn,
                        int(row["note_short_id"]),
                    )
                cursor = conn.execute(
                    """
                    UPDATE scoped_note_files
                    SET state = 'missing'
                    WHERE file_id = ? AND state != 'missing'
                    """,
                    (normalized_file_id,),
                )
                conn.commit()
                return max(0, int(cursor.rowcount or 0))
            except Exception:
                conn.rollback()
                raise

    def mark_missing_by_path(self, source_file_path: Any) -> int:
        normalized_path = normalize_relative_path(source_file_path)
        with self._lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT note_short_id
                    FROM scoped_note_files
                    WHERE source_file_path = ? AND state != 'missing'
                    """,
                    (normalized_path,),
                ).fetchall()
                for row in rows:
                    self._delete_chunks_for_note(
                        conn,
                        int(row["note_short_id"]),
                    )
                cursor = conn.execute(
                    """
                    UPDATE scoped_note_files
                    SET state = 'missing'
                    WHERE source_file_path = ? AND state != 'missing'
                    """,
                    (normalized_path,),
                )
                conn.commit()
                return max(0, int(cursor.rowcount or 0))
            except Exception:
                conn.rollback()
                raise

    def get_note_for_read(
        self,
        note_short_id: int,
        current_scope: Any,
    ) -> Optional[Dict[str, Any]]:
        allowed = readable_scopes(current_scope)
        placeholders = ",".join("?" for _ in allowed)
        row = self._get_conn().execute(
            f"""
            SELECT note_short_id, file_id, memory_scope, source_file_path,
                   heading_h1, total_lines, updated_at, content_sha256, state
            FROM scoped_note_files
            WHERE note_short_id = ?
              AND state = 'active'
              AND memory_scope IN ({placeholders})
            LIMIT 1
            """,
            (int(note_short_id), *allowed),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not note_path_matches_scope(
            item.get("source_file_path"),
            item.get("memory_scope"),
        ):
            return None
        return item

    def get_note_owned(
        self,
        note_short_id: int,
        current_scope: Any,
    ) -> Optional[Dict[str, Any]]:
        scope = normalize_scope(current_scope)
        row = self._get_conn().execute(
            """
            SELECT note_short_id, file_id, memory_scope, source_file_path,
                   heading_h1, total_lines, updated_at, content_sha256, state
            FROM scoped_note_files
            WHERE note_short_id = ?
              AND state = 'active'
              AND memory_scope = ?
            LIMIT 1
            """,
            (int(note_short_id), scope),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not note_path_matches_scope(
            item.get("source_file_path"),
            item.get("memory_scope"),
        ):
            return None
        return item

    def _fts_candidates(
        self,
        *,
        query: str,
        allowed_scopes: Sequence[str],
        candidate_limit: int,
        current_scope: str,
    ) -> List[Dict[str, Any]]:
        fts_query = _fts_query_for(query)
        if not self._fts_available or not fts_query:
            return []
        placeholders = ",".join("?" for _ in allowed_scopes)
        rows = self._get_conn().execute(
            f"""
            SELECT c.chunk_id, c.note_short_id, c.file_id, c.memory_scope,
                   c.source_file_path, c.chunk_index, c.line_start,
                   c.line_end, c.content,
                   bm25(scoped_note_chunks_fts) AS rank
            FROM scoped_note_chunks_fts
            JOIN scoped_note_chunks AS c
              ON c.chunk_id = scoped_note_chunks_fts.rowid
            JOIN scoped_note_files AS f
              ON f.note_short_id = c.note_short_id
             AND f.file_id = c.file_id
             AND f.memory_scope = c.memory_scope
             AND f.source_file_path = c.source_file_path
            WHERE scoped_note_chunks_fts MATCH ?
              AND c.memory_scope IN ({placeholders})
              AND f.state = 'active'
            ORDER BY rank ASC, c.note_short_id ASC, c.chunk_index ASC
            LIMIT ?
            """,
            (fts_query, *allowed_scopes, int(candidate_limit)),
        ).fetchall()
        results = [dict(row) for row in rows]
        for item in results:
            item["_rank"] = float(item.pop("rank", 0.0) or 0.0)
            item["_is_own_scope"] = item.get("memory_scope") == current_scope
        return results

    def _fallback_candidates(
        self,
        *,
        allowed_scopes: Sequence[str],
        candidate_limit: int,
        current_scope: str,
    ) -> List[Dict[str, Any]]:
        del candidate_limit
        placeholders = ",".join("?" for _ in allowed_scopes)
        rows = self._get_conn().execute(
            f"""
            SELECT c.chunk_id, c.note_short_id, c.file_id, c.memory_scope,
                   c.source_file_path, c.chunk_index, c.line_start,
                   c.line_end, c.content
            FROM scoped_note_chunks AS c
            JOIN scoped_note_files AS f
              ON f.note_short_id = c.note_short_id
             AND f.file_id = c.file_id
             AND f.memory_scope = c.memory_scope
             AND f.source_file_path = c.source_file_path
            WHERE c.memory_scope IN ({placeholders})
              AND f.state = 'active'
            ORDER BY f.updated_at DESC, c.note_short_id ASC, c.chunk_index ASC
            LIMIT ?
            """,
            (*allowed_scopes, MAX_FALLBACK_CANDIDATES),
        ).fetchall()
        results = [dict(row) for row in rows]
        for item in results:
            item["_rank"] = 0.0
            item["_is_own_scope"] = item.get("memory_scope") == current_scope
        return results

    def search(
        self,
        *,
        query: Any,
        current_scope: Any,
        limit: int = 20,
        max_chunks_per_file: int = 5,
    ) -> List[Dict[str, Any]]:
        query_text = _clean_text(query)
        if not query_text:
            return []
        scope = normalize_scope(current_scope)
        allowed = readable_scopes(scope)
        final_limit = min(MAX_SEARCH_LIMIT, max(0, int(limit)))
        if final_limit <= 0:
            return []
        per_file_cap = max(1, int(max_chunks_per_file or 1))
        candidate_limit = min(
            MAX_FALLBACK_CANDIDATES,
            max(100, final_limit * 20),
        )
        try:
            candidates = self._fts_candidates(
                query=query_text,
                allowed_scopes=allowed,
                candidate_limit=candidate_limit,
                current_scope=scope,
            )
        except sqlite3.Error as exc:
            _LOGGER.warning("scoped note FTS 查询失败，降级扫描: %s", exc)
            candidates = []
        if not candidates:
            candidates = self._fallback_candidates(
                allowed_scopes=allowed,
                candidate_limit=candidate_limit,
                current_scope=scope,
            )

        scored: List[Dict[str, Any]] = []
        for candidate in candidates:
            candidate_scope = candidate.get("memory_scope")
            if candidate_scope not in allowed:
                continue
            if not note_path_matches_scope(
                candidate.get("source_file_path"),
                candidate_scope,
            ):
                continue
            score = _score_candidate(candidate, query_text)
            if score < 0:
                continue
            copied = dict(candidate)
            copied["score"] = float(score)
            copied.pop("_rank", None)
            copied.pop("_is_own_scope", None)
            scored.append(copied)

        scored.sort(
            key=lambda item: (
                float(item.get("score", 0.0)),
                int(item.get("note_short_id", 0)),
                -int(item.get("chunk_index", 0)),
            ),
            reverse=True,
        )

        output: List[Dict[str, Any]] = []
        counts: Dict[int, int] = {}
        seen: set[Tuple[int, int]] = set()
        for item in scored:
            note_short_id = int(item.get("note_short_id", 0))
            key = (note_short_id, int(item.get("chunk_index", 0)))
            if key in seen:
                continue
            file_key = note_short_id
            if counts.get(file_key, 0) >= per_file_cap:
                continue
            seen.add(key)
            counts[file_key] = counts.get(file_key, 0) + 1
            output.append(item)
            if len(output) >= final_limit:
                break
        return output

    def stats(self) -> Dict[str, int]:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_files,
                SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) AS active_files,
                SUM(CASE WHEN state = 'quarantine' THEN 1 ELSE 0 END) AS quarantined_files,
                SUM(CASE WHEN state = 'missing' THEN 1 ELSE 0 END) AS missing_files
            FROM scoped_note_files
            """
        ).fetchone()
        chunk_count = int(
            conn.execute("SELECT COUNT(*) FROM scoped_note_chunks").fetchone()[0]
            or 0
        )
        return {
            "total_files": int(row["total_files"] or 0),
            "active_files": int(row["active_files"] or 0),
            "quarantined_files": int(row["quarantined_files"] or 0),
            "missing_files": int(row["missing_files"] or 0),
            "chunks": chunk_count,
            "fts_available": int(self._fts_available),
        }

    def close(self) -> None:
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
            for conn in connections:
                try:
                    conn.close()
                except Exception:
                    pass


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def _repository_for(note_service: Any) -> ScopedNoteRepository:
    repository = getattr(note_service, "_scoped_note_repository", None)
    if repository is not None:
        return repository
    plugin_context = getattr(note_service, "plugin_context", None)
    if plugin_context is None:
        raise RuntimeError("NoteService 缺少 plugin_context")
    base_dir = Path(plugin_context.get_index_dir()) / "scoped_notes"
    base_dir.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(base_dir, 0o700)
    repository = ScopedNoteRepository(base_dir / SCOPED_NOTE_DB_NAME)
    _best_effort_chmod(Path(repository.db_path), 0o600)
    setattr(note_service, "_scoped_note_repository", repository)
    return repository


def _raw_root_for(plugin_context: Any) -> Path:
    root = Path(plugin_context.get_path_manager().get_raw_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_scoped_note_directory(
    plugin_context: Any,
    scope: Any,
) -> Tuple[Path, str]:
    raw_root = _raw_root_for(plugin_context)
    relative = scoped_relative_directory(scope)
    current = raw_root
    parts = PurePosixPath(relative).parts
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise NotePathError("scope 笔记目录不得经过符号链接")
        current.mkdir(exist_ok=True)
        if current.is_symlink():
            raise NotePathError("scope 笔记目录不得经过符号链接")
        resolved = current.resolve()
        if resolved != raw_root and raw_root not in resolved.parents:
            raise NotePathError("scope 笔记目录越界")

    resolved_directory = current.resolve()
    actual_relative = resolved_directory.relative_to(raw_root).as_posix()
    if actual_relative != relative:
        raise NotePathError("scope 笔记目录发生重定向")
    if len(parts) >= 2:
        _best_effort_chmod(current.parent, 0o700)
    _best_effort_chmod(current, 0o700)
    return resolved_directory, relative


def _resolve_file_under_raw(
    plugin_context: Any,
    file_path: Any,
    relative_path: Any = None,
) -> Tuple[Path, str]:
    raw_root = _raw_root_for(plugin_context)
    target = Path(file_path).resolve()
    if target != raw_root and raw_root not in target.parents:
        raise NotePathError("笔记文件路径越界")
    actual_relative = target.relative_to(raw_root).as_posix()
    actual_relative = normalize_relative_path(actual_relative)
    if relative_path is not None:
        supplied = normalize_relative_path(relative_path)
        if supplied != actual_relative:
            raise NotePathError("笔记相对路径与实际文件不一致")
    return target, actual_relative


def _extract_heading_h1(content: str) -> str:
    for raw_line in str(content).splitlines():
        line = raw_line.strip()
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return ""


def _format_scoped_results(raw_results: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    formatted: List[Dict[str, Any]] = []
    for item in raw_results or []:
        short_id = int(item.get("note_short_id", -1))
        chunk_index = int(item.get("chunk_index", 0))
        formatted.append(
            {
                "id": f"{short_id}#{chunk_index}",
                "content": str(item.get("content") or "").strip(),
                "metadata": {
                    "source_file_path": str(
                        item.get("source_file_path") or ""
                    ),
                    "file_id": str(item.get("file_id") or ""),
                    "memory_scope": str(item.get("memory_scope") or ""),
                    "line_start": int(item.get("line_start", 0)),
                    "line_end": int(item.get("line_end", 0)),
                    "chunk_index": chunk_index,
                    "note_short_id": short_id,
                },
                "tags": [],
                "similarity": float(item.get("score", 0.0)),
            }
        )
    return formatted


def _format_note_read_result(
    *,
    note_short_id: int,
    row: Mapping[str, Any],
    text: str,
    offset: int,
    limit: Optional[int],
    max_chars: int,
) -> str:
    lines = text.splitlines()
    total_lines = len(lines)
    start = max(1, int(offset))
    rel_path = str(row.get("source_file_path") or "")
    if start > total_lines:
        return (
            "[angel_note_read]\n"
            f"note_short_id: {note_short_id}\n"
            f"source_file_path: {rel_path}\n"
            f"memory_scope: {row.get('memory_scope', '')}\n"
            f"total_lines: {total_lines}\n"
            f"offset: {start}\n"
            "内容已到末尾，无更多行。"
        )

    selected: List[str] = []
    char_count = 0
    line_limit = max(1, int(limit)) if limit is not None else max(1, total_lines)
    index = start - 1
    while index < total_lines and len(selected) < line_limit:
        line = lines[index]
        if selected and char_count + len(line) + 1 > max_chars:
            break
        selected.append(line)
        char_count += len(line) + 1
        index += 1

    actual_end = start + len(selected) - 1
    has_more = actual_end < total_lines
    result = (
        "[angel_note_read]\n"
        f"note_short_id: {note_short_id}\n"
        f"source_file_path: {rel_path}\n"
        f"memory_scope: {row.get('memory_scope', '')}\n"
        f"total_lines: {total_lines}\n"
        f"returned: L{start}-{actual_end}\n"
        f"has_more: {'true' if has_more else 'false'}"
    )
    if has_more:
        result += f" (继续读取请用 offset={actual_end + 1})"
    return result + "\n\n" + "\n".join(selected)


def _bootstrap_scoped_notes(file_monitor: Any) -> Dict[str, int]:
    note_service = getattr(file_monitor, "note_service", None)
    if note_service is None:
        return {"scanned": 0, "indexed": 0, "failed": 0}
    repository = _repository_for(note_service)
    if repository.get_meta(SCOPED_NOTE_BOOTSTRAP_KEY) == "complete":
        return {"scanned": 0, "indexed": 0, "failed": 0}

    raw_root = Path(getattr(file_monitor, "raw_directory")).resolve()
    scanned = 0
    indexed = 0
    failed = 0
    for root, _, filenames in os.walk(str(raw_root)):
        for filename in filenames:
            if Path(filename).suffix.lower() not in {".md", ".txt"}:
                continue
            scanned += 1
            target = Path(root) / filename
            try:
                relative = target.resolve().relative_to(raw_root).as_posix()
                note_service.parse_and_store_file_sync(
                    str(target),
                    relative,
                    update_search_index=False,
                )
                indexed += 1
            except Exception as exc:
                failed += 1
                _LOGGER.error(
                    "scoped notes 初始扫描失败 path=%s error=%s",
                    target,
                    exc,
                )

    repository.set_meta("raw_bootstrap_last_scanned", scanned)
    repository.set_meta("raw_bootstrap_last_indexed", indexed)
    repository.set_meta("raw_bootstrap_last_failed", failed)
    if failed == 0:
        repository.set_meta(SCOPED_NOTE_BOOTSTRAP_KEY, "complete")
    return {"scanned": scanned, "indexed": indexed, "failed": failed}


def _restore_note_config(deepmind: Any) -> None:
    config = getattr(deepmind, "config", None)
    if isinstance(config, Mapping):
        note_assistant = config.get("note_assistant", {})
    else:
        note_assistant = (
            getattr(config, "note_assistant", {})
            if config is not None
            else {}
        )
    if isinstance(note_assistant, dict):
        enabled = bool(note_assistant.get("enable_recall", True))
        try:
            top_k = max(0, int(note_assistant.get("top_k", 3)))
        except (TypeError, ValueError):
            top_k = 3
    else:
        enabled = True
        try:
            top_k = max(0, int(getattr(config, "note_top_k", 3)))
        except (TypeError, ValueError):
            top_k = 3
    deepmind.note_recall_enabled = enabled
    deepmind.note_inject_top_k = top_k
    deepmind.note_candidate_top_k = top_k * 7


def install_scoped_notes() -> None:
    """Install the P1 scoped note implementation after P0 containment."""

    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        from .deepmind import DeepMind
        from .file_monitor import FileMonitorService
        from .services.retrieval_service import DeepMindRetrievalService
        from .utils.memory_formatter import MemoryFormatter
        from ..llm_memory.parser.note_chunker import chunk_file
        from ..llm_memory.service.note_service import NoteService
        from ..tools.angel_note_create import NoteCreateTool
        from ..tools.angel_note_read import NoteRecallTool
        from ..tools.angel_recall import CoreMemoryRecallTool

        original_remove_by_id = NoteService.remove_file_data_by_file_id
        original_close = NoteService.close
        original_monitor_start = FileMonitorService.start_monitoring
        original_deepmind_init = DeepMind.__init__
        original_retrieve = DeepMindRetrievalService.retrieve_memories_and_notes

        def get_scoped_note_repository(self: Any) -> ScopedNoteRepository:
            return _repository_for(self)

        async def scoped_search_notes(
            self: Any,
            query: str,
            max_results: int = 10,
            tag_filter: Optional[List[str]] = None,
            threshold: float = 0.5,
            memory_scope: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            del tag_filter, threshold
            import asyncio

            scope = resolve_note_scope(memory_scope)
            raw = await asyncio.to_thread(
                _repository_for(self).search,
                query=query,
                current_scope=scope,
                limit=max_results,
            )
            return _format_scoped_results(raw)

        async def scoped_search_notes_by_top_k(
            self: Any,
            query: str,
            recall_count: int = 100,
            top_k: int = 20,
            tag_filter: Optional[List[str]] = None,
            vector: Optional[List[float]] = None,
            memory_scope: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            del recall_count, tag_filter, vector
            import asyncio

            scope = resolve_note_scope(memory_scope)
            raw = await asyncio.to_thread(
                _repository_for(self).search,
                query=query,
                current_scope=scope,
                limit=top_k,
            )
            return _format_scoped_results(raw)

        def _parse_file(
            self: Any,
            file_path: str,
            relative_path: Optional[str],
            *,
            explicit_scope: Optional[str],
        ) -> Tuple[int, Dict[str, float]]:
            started = time.time()
            target, actual_relative = _resolve_file_under_raw(
                self.plugin_context,
                file_path,
                relative_path,
            )
            if not target.exists() or not target.is_file():
                return 0, {}
            if target.suffix.lower() not in {".md", ".txt"}:
                return 0, {}

            inferred_scope = infer_scope_from_relative_path(actual_relative)
            if explicit_scope is not None:
                scope = normalize_scope(explicit_scope)
                if inferred_scope != scope:
                    raise NotePathError(
                        "显式 memory_scope 与 scope 编码目录不一致"
                    )
            else:
                scope = inferred_scope or QUARANTINE_SCOPE

            text = target.read_text(encoding="utf-8", errors="ignore")
            file_timestamp = int(target.stat().st_mtime)
            file_id = self.id_service.file_to_id(
                actual_relative,
                file_timestamp,
            )
            parse_started = time.time()
            chunks = chunk_file(text, actual_relative)
            parse_ms = (time.time() - parse_started) * 1000
            result = _repository_for(self).upsert_file(
                memory_scope=scope,
                file_id=str(file_id),
                source_file_path=actual_relative,
                heading_h1=_extract_heading_h1(text),
                total_lines=len(text.splitlines()),
                updated_at=file_timestamp,
                content=text,
                chunks=chunks,
            )
            total_ms = (time.time() - started) * 1000
            return int(result["chunks"]), {
                "parse": parse_ms,
                "store_total": max(0.0, total_ms - parse_ms),
                "chunk_count": int(result["chunks"]),
                "scoped_total": total_ms,
            }

        def parse_and_store_file_sync(
            self: Any,
            file_path: str,
            relative_path: Optional[str] = None,
            *,
            update_search_index: bool = True,
        ) -> Tuple[int, Dict[str, float]]:
            del update_search_index
            return _parse_file(
                self,
                file_path,
                relative_path,
                explicit_scope=None,
            )

        def parse_scoped_file_sync(
            self: Any,
            file_path: str,
            relative_path: Optional[str] = None,
            *,
            memory_scope: str,
            update_search_index: bool = True,
        ) -> Tuple[int, Dict[str, float]]:
            del update_search_index
            return _parse_file(
                self,
                file_path,
                relative_path,
                explicit_scope=memory_scope,
            )

        def scoped_remove_by_file_id(self: Any, file_id: int) -> bool:
            scoped_ok = True
            relative = None
            try:
                relative = self.id_service.id_to_file(int(file_id))
                if relative:
                    _repository_for(self).mark_missing_by_path(relative)
                else:
                    self.logger.warning(
                        "scoped note 删除时无法从 file_id 解析路径，"
                        "为避免 ID 重用误删其他 scope，未修改 scoped registry: %s",
                        file_id,
                    )
            except Exception as exc:
                scoped_ok = False
                self.logger.error(
                    "scoped note 删除失败 file_id=%s path=%s error=%s",
                    file_id,
                    relative or "unknown",
                    exc,
                )
            try:
                original_remove_by_id(self, file_id)
            except Exception as exc:
                self.logger.warning(
                    "旧笔记派生索引清理失败（scoped 主链路不受影响）: %s",
                    exc,
                )
            return scoped_ok

        def scoped_remove_file_data(self: Any, file_path: str) -> bool:
            try:
                target, relative = _resolve_file_under_raw(
                    self.plugin_context,
                    file_path,
                )
                del target
                _repository_for(self).mark_missing_by_path(relative)

                # Never call file_to_id here: deletion must not manufacture a
                # fresh global ID.  Resolve only an existing index entry.
                existing_id = None
                manager = getattr(self.id_service, "file_manager", None)
                if manager is not None:
                    for item in manager.get_all_files():
                        if str(item.get("relative_path") or "") == relative:
                            existing_id = int(item["id"])
                            break
                if existing_id is not None:
                    try:
                        original_remove_by_id(self, existing_id)
                    except Exception as exc:
                        self.logger.warning(
                            "旧笔记派生索引按路径清理失败: %s",
                            exc,
                        )
                return True
            except Exception as exc:
                self.logger.error(
                    "scoped note 按路径删除失败 path=%s error=%s",
                    file_path,
                    exc,
                )
                return False

        def scoped_close(self: Any) -> None:
            repository = getattr(self, "_scoped_note_repository", None)
            if repository is not None:
                try:
                    repository.close()
                except Exception as exc:
                    self.logger.warning("关闭 scoped note repository 失败: %s", exc)
            original_close(self)

        async def scoped_note_create(
            self: Any,
            event: Any,
            title: str,
            content: str,
        ) -> str:
            body = str(content or "").strip()
            if not body:
                return "错误：笔记内容不能为空。"
            plugin_context = getattr(event, "plugin_context", None)
            if plugin_context is None:
                return "错误：无法获取插件上下文。"
            try:
                scope = await plugin_context.resolve_memory_scope_from_event(event)
                scope = normalize_scope(scope)
            except Exception as exc:
                self.logger.error("%s: scope 解析失败: %s", self.name, exc)
                return "错误：无法确定当前人格记忆域，笔记写入已拒绝。"

            note_service = plugin_context.get_component("note_service")
            if note_service is None:
                return "错误：笔记服务不可用，系统可能仍在初始化中。"

            try:
                target_dir, relative_dir = _ensure_scoped_note_directory(
                    plugin_context,
                    scope,
                )
            except Exception as exc:
                self.logger.error("%s: 创建 scope 笔记目录失败: %s", self.name, exc)
                return "错误：scope 笔记目录不安全，写入已拒绝。"

            safe_title = self._sanitize_title(title)[:48]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{safe_title}_{timestamp}_{secrets.token_hex(3)}.md"
            target = target_dir / filename
            relative = f"{relative_dir}/{filename}"

            if not body.lstrip().startswith("#"):
                body = f"# {title}\n\n{body}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            try:
                temporary.write_text(body, encoding="utf-8")
                _best_effort_chmod(temporary, 0o600)
                os.replace(temporary, target)
                _best_effort_chmod(target, 0o600)
            except Exception as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass
                self.logger.error("%s: 写入笔记失败: %s", self.name, exc)
                return f"错误：写入笔记失败：{exc}"

            try:
                import asyncio

                await asyncio.to_thread(
                    note_service.parse_scoped_file_sync,
                    str(target),
                    relative,
                    memory_scope=scope,
                )
            except Exception as exc:
                try:
                    _repository_for(note_service).set_meta(
                        SCOPED_NOTE_BOOTSTRAP_KEY,
                        "incomplete",
                    )
                except Exception:
                    pass
                self.logger.warning(
                    "%s: 笔记已写入但 scoped 索引同步失败: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                return (
                    f"笔记《{title}》已安全写入 {filename}，"
                    "但索引同步失败；下次文件扫描会重试。"
                )
            return (
                f"学习笔记《{title}》已保存并纳入当前人格的隔离知识库，"
                f"文件为 {filename}。"
            )

        async def scoped_note_read(
            self: Any,
            event: Any,
            note_short_id: int,
            offset: int = 1,
            limit: Optional[int] = None,
        ) -> str:
            plugin_context = getattr(event, "plugin_context", None)
            if plugin_context is None:
                return "错误：无法获取插件上下文。"
            try:
                scope = await plugin_context.resolve_memory_scope_from_event(event)
                scope = normalize_scope(scope)
            except Exception as exc:
                self.logger.error("%s: scope 解析失败: %s", self.name, exc)
                return "错误：无法确定当前人格记忆域，笔记读取已拒绝。"

            note_service = plugin_context.get_component("note_service")
            if note_service is None:
                return "错误：笔记服务不可用。"
            repository = _repository_for(note_service)
            row = repository.get_note_for_read(int(note_short_id), scope)
            if row is None:
                return (
                    f"错误：未找到 note_short_id={int(note_short_id)}，"
                    "或该笔记不属于当前可读记忆域。"
                )

            relative = normalize_relative_path(row["source_file_path"])
            raw_root = _raw_root_for(plugin_context)
            try:
                target, verified_relative = _resolve_file_under_raw(
                    plugin_context,
                    raw_root / Path(relative),
                    relative,
                )
            except NotePathError:
                return "错误：笔记路径发生越界或重定向。"
            if verified_relative != relative:
                return "错误：笔记路径与登记信息不一致。"
            if not target.exists() or not target.is_file():
                return f"错误：文件不存在：{relative}"
            try:
                text = target.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                return f"错误：读取文件失败：{exc}"
            return _format_note_read_result(
                note_short_id=int(note_short_id),
                row=row,
                text=text,
                offset=offset,
                limit=limit,
                max_chars=int(getattr(self, "MAX_CHARS", 30000)),
            )

        async def scoped_core_recall(
            self: Any,
            event: Any,
            query: str,
        ) -> str:
            query_text = _clean_text(query)
            if not query_text:
                return "参数错误：query 为必填且不能为空。"
            plugin_context = getattr(event, "plugin_context", None)
            if plugin_context is None:
                return "错误：无法获取插件上下文。"
            try:
                memory_runtime = plugin_context.get_component("memory_runtime")
                if memory_runtime is None:
                    raise ValueError("memory_runtime 未注册。")
                scope = await plugin_context.resolve_memory_scope_from_event(event)
                scope = normalize_scope(scope)
            except Exception as exc:
                self.logger.error("%s: 获取安全上下文失败: %s", self.name, exc)
                return "错误：无法确定当前会话，检索已拒绝。"

            parts: List[str] = []
            try:
                memories = await memory_runtime.comprehensive_recall(
                    query=query_text,
                    limit=50,
                    event=event,
                    memory_scope=scope,
                )
                active = [memory for memory in memories if memory.is_active]
                if active:
                    manager = plugin_context.get_component("memory_sql_manager")
                    memory_text = MemoryFormatter.format_session_memories(
                        active,
                        short_id_registry=manager,
                    )
                    parts.append(
                        f"[记忆检索结果 ({len(active)}条)]\n{memory_text}"
                    )
                else:
                    parts.append("[记忆检索结果]\n无相关记忆。")
            except Exception as exc:
                self.logger.error(
                    "%s: 记忆检索失败: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                parts.append(f"[记忆检索结果]\n检索失败：{exc}")

            try:
                note_service = plugin_context.get_component("note_service")
                if note_service is None:
                    parts.append("[笔记检索结果]\n笔记服务不可用。")
                else:
                    note_results = await note_service.search_notes(
                        query=query_text,
                        max_results=50,
                        memory_scope=scope,
                    )
                    if note_results:
                        note_results = self._precise_filter(
                            note_results,
                            query_text,
                        )
                    if not note_results:
                        parts.append("[笔记检索结果]\n无相关笔记。")
                    else:
                        aggregated = self._aggregate_by_file(note_results)
                        note_lines: List[str] = []
                        for file_info in aggregated:
                            short_id = int(file_info["short_id"])
                            header = (
                                f"[{file_info['path']}] "
                                f"(ID:{short_id}, 命中{len(file_info['chunks'])}处)"
                            )
                            previews = [
                                "  "
                                f"L:{chunk['line_start']}-{chunk['line_end']} | "
                                f"{chunk['content'][:80]}"
                                for chunk in file_info["chunks"]
                            ]
                            note_lines.append(
                                header + "\n" + "\n".join(previews)
                            )
                        parts.append(
                            f"[笔记检索结果 ({len(aggregated)}个文件)]\n"
                            "[展开正文: angel_note_read(note_short_id, offset, limit)]"
                            "\n\n"
                            + "\n\n".join(note_lines)
                        )
            except Exception as exc:
                self.logger.error(
                    "%s: scoped 笔记检索失败: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                parts.append(f"[笔记检索结果]\n检索失败：{exc}")
            return "\n\n".join(parts)

        def scoped_monitor_start(self: Any) -> Any:
            try:
                report = _bootstrap_scoped_notes(self)
                if report["scanned"]:
                    self.logger.info(
                        "scoped notes 初始扫描完成 scanned=%s indexed=%s failed=%s",
                        report["scanned"],
                        report["indexed"],
                        report["failed"],
                    )
            except Exception as exc:
                self.logger.error(
                    "scoped notes 初始扫描失败（旧笔记仍保持不可读）: %s",
                    exc,
                    exc_info=True,
                )
            return original_monitor_start(self)

        def scoped_deepmind_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_deepmind_init(self, *args, **kwargs)
            _restore_note_config(self)

        async def scoped_retrieve(
            self: Any,
            event: Any,
            query: str,
            precompute_vectors: bool = False,
        ) -> Dict[str, Any]:
            scope = await self.deepmind.plugin_context.resolve_memory_scope_from_event(
                event
            )
            scope = normalize_scope(scope)
            token = _CURRENT_NOTE_SCOPE.set(scope)
            try:
                return await original_retrieve(
                    self,
                    event,
                    query,
                    precompute_vectors,
                )
            finally:
                _CURRENT_NOTE_SCOPE.reset(token)

        NoteService.get_scoped_note_repository = get_scoped_note_repository
        NoteService.search_notes = scoped_search_notes
        NoteService.search_notes_by_top_k = scoped_search_notes_by_top_k
        NoteService.parse_and_store_file_sync = parse_and_store_file_sync
        NoteService.parse_scoped_file_sync = parse_scoped_file_sync
        NoteService.remove_file_data = scoped_remove_file_data
        NoteService.remove_file_data_by_file_id = scoped_remove_by_file_id
        NoteService.close = scoped_close

        NoteCreateTool.run = scoped_note_create
        NoteRecallTool.run = scoped_note_read
        CoreMemoryRecallTool.run = scoped_core_recall
        FileMonitorService.start_monitoring = scoped_monitor_start
        DeepMind.__init__ = scoped_deepmind_init
        DeepMindRetrievalService.retrieve_memories_and_notes = scoped_retrieve

        _INSTALLED = True
