"""Scope-aware SimpleMemory backup replay.

P0 paused vector -> SQLite replay because the legacy implementation deduplicated
by ``judgment`` across the whole database. This module restores the feature
with ``(memory_scope, judgment)`` as the identity boundary and quarantines
records whose scope is absent.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Tuple

from .security_guard import QUARANTINE_SCOPE

_INSTALLED = False


def normalize_backup_scope(value: Any) -> str:
    """Return a persisted scope; unknown legacy rows go to quarantine."""

    scope = str(value or "").strip()
    return scope or QUARANTINE_SCOPE


def dedupe_backup_candidates(
    memories: Iterable[Mapping[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Keep the newest input row for each exact ``(scope, judgment)`` key."""

    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw_item in memories or []:
        if not isinstance(raw_item, Mapping):
            continue
        judgment = str(raw_item.get("judgment") or "").strip()
        if not judgment:
            continue
        scope = normalize_backup_scope(raw_item.get("memory_scope"))
        try:
            created_at = float(raw_item.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created_at = 0.0

        key = (scope, judgment)
        current = deduped.get(key)
        if current is None or created_at >= float(current.get("created_at") or 0.0):
            item = dict(raw_item)
            item["memory_scope"] = scope
            item["judgment"] = judgment
            item["created_at"] = created_at
            deduped[key] = item
    return deduped


def migrate_scope_judgment_identity(manager: Any) -> Dict[str, int]:
    """Quarantine blank scopes, dedupe within each scope, and add a unique index."""

    deleted_ids: List[str] = []
    quarantined = 0
    with manager._connect() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET memory_scope = ? "
            "WHERE TRIM(COALESCE(memory_scope, '')) = ''",
            (QUARANTINE_SCOPE,),
        )
        quarantined = int(cursor.rowcount or 0)
        duplicate_groups = conn.execute(
            """
            SELECT memory_scope, judgment
            FROM memory_records
            GROUP BY memory_scope, judgment
            HAVING COUNT(1) > 1
            """
        ).fetchall()
        for group in duplicate_groups:
            scope = str(group["memory_scope"] or "")
            judgment = str(group["judgment"] or "")
            rows = conn.execute(
                """
                SELECT id
                FROM memory_records
                WHERE memory_scope = ? AND judgment = ?
                ORDER BY created_at DESC, updated_at DESC, rowid DESC
                """,
                (scope, judgment),
            ).fetchall()
            duplicate_ids = [str(row["id"]) for row in rows[1:]]
            if not duplicate_ids:
                continue
            placeholders = ",".join("?" for _ in duplicate_ids)
            conn.execute(
                f"DELETE FROM memory_tag_rel "
                f"WHERE memory_id IN ({placeholders})",
                tuple(duplicate_ids),
            )
            conn.execute(
                f"DELETE FROM memory_records "
                f"WHERE memory_scope = ? AND id IN ({placeholders})",
                tuple([scope, *duplicate_ids]),
            )
            deleted_ids.extend(duplicate_ids)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_scope_judgment
            ON memory_records(memory_scope, judgment)
            """
        )
        conn.commit()

    manager.logger.info(
        "[memory_scope迁移] scope+judgment 唯一约束完成 "
        "quarantined=%s deduped=%s",
        quarantined,
        len(deleted_ids),
    )
    return {"quarantined": quarantined, "deduped": len(deleted_ids)}


def scoped_backup_upsert_sync(
    manager: Any,
    memories: List[Dict[str, Any]],
    parse_tags: Callable[[Any], Iterable[str]],
) -> Dict[str, int]:
    """Idempotently restore memories without crossing a scope boundary."""

    scanned = len(memories or [])
    candidates = dedupe_backup_candidates(memories or [])
    failed = 0
    upserted = 0
    now = time.time()
    fts_upsert_ids: List[str] = []
    fts_delete_ids: List[str] = []

    with manager._connect() as conn:
        for (memory_scope, judgment), raw in candidates.items():
            try:
                created_at = float(raw.get("created_at") or now)
                normalized_tags = manager._normalize_tags(
                    parse_tags(raw.get("tags", []))
                )
                reasoning = str(raw.get("reasoning") or "").strip()
                memory_type = (
                    str(raw.get("memory_type") or "知识记忆").strip()
                    or "知识记忆"
                )
                strength = int(raw.get("strength", 1) or 1)
                is_active = 1 if bool(raw.get("is_active", False)) else 0
                useful_count = int(raw.get("useful_count", 0) or 0)
                useful_score = float(raw.get("useful_score", 0.0) or 0.0)
                last_recalled_at = float(
                    raw.get("last_recalled_at", 0.0) or 0.0
                )

                existing_rows = conn.execute(
                    """
                    SELECT id, created_at
                    FROM memory_records
                    WHERE memory_scope = ? AND judgment = ?
                    ORDER BY created_at DESC, updated_at DESC
                    """,
                    (memory_scope, judgment),
                ).fetchall()

                if existing_rows:
                    keep_id = str(existing_rows[0]["id"])
                    existing_created_at = float(
                        existing_rows[0]["created_at"] or 0.0
                    )
                    if created_at >= existing_created_at:
                        conn.execute(
                            """
                            UPDATE memory_records
                            SET memory_type = ?, reasoning = ?, strength = ?,
                                is_active = ?, useful_count = ?, useful_score = ?,
                                last_recalled_at = ?, created_at = ?, updated_at = ?
                            WHERE id = ? AND memory_scope = ?
                            """,
                            (
                                memory_type,
                                reasoning,
                                strength,
                                is_active,
                                useful_count,
                                useful_score,
                                last_recalled_at,
                                created_at,
                                now,
                                keep_id,
                                memory_scope,
                            ),
                        )
                        manager._replace_memory_tags(
                            conn,
                            keep_id,
                            normalized_tags,
                        )
                        upserted += 1
                        fts_upsert_ids.append(keep_id)

                    duplicate_ids = [
                        str(row["id"]) for row in existing_rows[1:]
                    ]
                    if duplicate_ids:
                        placeholders = ",".join("?" for _ in duplicate_ids)
                        conn.execute(
                            f"DELETE FROM memory_tag_rel "
                            f"WHERE memory_id IN ({placeholders})",
                            tuple(duplicate_ids),
                        )
                        conn.execute(
                            f"DELETE FROM memory_records "
                            f"WHERE memory_scope = ? "
                            f"AND id IN ({placeholders})",
                            tuple([memory_scope, *duplicate_ids]),
                        )
                        fts_delete_ids.extend(duplicate_ids)
                else:
                    memory_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO memory_records(
                            id, memory_type, judgment, reasoning, strength,
                            is_active, useful_count, useful_score,
                            last_recalled_at, memory_scope, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            memory_id,
                            memory_type,
                            judgment,
                            reasoning,
                            strength,
                            is_active,
                            useful_count,
                            useful_score,
                            last_recalled_at,
                            memory_scope,
                            created_at,
                            now,
                        ),
                    )
                    manager._replace_memory_tags(
                        conn,
                        memory_id,
                        normalized_tags,
                    )
                    upserted += 1
                    fts_upsert_ids.append(memory_id)
            except Exception:
                failed += 1
                manager.logger.exception(
                    "SimpleMemory scoped backup failed (scope=%s judgment=%s)",
                    memory_scope,
                    judgment,
                )

        conn.execute(
            """
            DELETE FROM memory_tag_rel
            WHERE memory_id NOT IN (SELECT id FROM memory_records)
            """
        )
        conn.commit()

    manager._sync_memory_fts_batch_sync(
        upsert_ids=fts_upsert_ids,
        delete_ids=fts_delete_ids,
    )
    return {
        "scanned": scanned,
        "deduped": len(candidates),
        "upserted": upserted,
        "failed": failed,
    }


def install_scoped_backup_replay() -> None:
    """Replace the P0 pause with scope-safe replay implementations."""

    global _INSTALLED
    if _INSTALLED:
        return

    from ..llm_memory.components.memory_sql_manager import MemorySqlManager
    from ..llm_memory.models.data_models import BaseMemory
    from .services.simple_memory_backup_service import SimpleMemoryBackupService

    original_init_db = MemorySqlManager._init_db

    def scoped_init_db(self: Any) -> None:
        original_init_db(self)
        migrate_scope_judgment_identity(self)

    def sync_upsert(
        self: Any,
        memories: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        return scoped_backup_upsert_sync(
            self,
            memories,
            BaseMemory._parse_tags,
        )

    async def async_upsert(
        self: Any,
        memories: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        return await asyncio.to_thread(sync_upsert, self, memories)

    async def secure_backup_from_collection(
        self: Any,
        collection: Any,
        memory_sql_manager: Any,
        source: str,
        provider_id: str = "",
    ) -> Dict[str, int]:
        started_at = time.time()
        self.logger.info(
            "[simple_backup] 开始安全备份 source=%s provider=%s",
            source,
            provider_id or "unknown",
        )
        try:
            result = await asyncio.to_thread(
                collection.get,
                include=["metadatas", "documents"],
            )
        except Exception as exc:
            self.logger.error(
                "[simple_backup] 读取向量记忆失败 source=%s error=%s",
                source,
                exc,
                exc_info=True,
            )
            return {"scanned": 0, "deduped": 0, "upserted": 0, "failed": 1}

        result = result or {}
        ids = list(result.get("ids") or [])
        metadatas = list(result.get("metadatas") or [])
        documents = list(result.get("documents") or [])
        scanned = max(len(ids), len(metadatas), len(documents))
        normalized: List[Dict[str, Any]] = []
        skipped_no_judgment = 0
        quarantined = 0

        for index in range(scanned):
            metadata = (
                metadatas[index]
                if index < len(metadatas)
                and isinstance(metadatas[index], dict)
                else {}
            )
            document = (
                str(documents[index] or "").strip()
                if index < len(documents)
                else ""
            )
            judgment = str(metadata.get("judgment") or "").strip() or document
            if not judgment:
                skipped_no_judgment += 1
                continue
            reasoning = str(metadata.get("reasoning") or "").strip()
            if not reasoning and document and document != judgment:
                reasoning = document
            scope = normalize_backup_scope(metadata.get("memory_scope"))
            if scope == QUARANTINE_SCOPE:
                quarantined += 1
            normalized.append(
                {
                    "memory_type": metadata.get("memory_type", "知识记忆"),
                    "judgment": judgment,
                    "reasoning": reasoning,
                    "tags": metadata.get("tags", ""),
                    "strength": metadata.get("strength", 1),
                    "is_active": metadata.get("is_active", False),
                    "useful_count": metadata.get("useful_count", 0),
                    "useful_score": metadata.get("useful_score", 0.0),
                    "last_recalled_at": metadata.get("last_recalled_at", 0.0),
                    "memory_scope": scope,
                    "created_at": metadata.get("created_at") or int(time.time()),
                }
            )

        stats = await memory_sql_manager.upsert_memories_by_judgment(normalized)
        self.logger.info(
            "[simple_backup] 安全备份完成 source=%s scanned=%s deduped=%s "
            "upserted=%s failed=%s quarantined=%s skipped_no_judgment=%s "
            "cost_ms=%s",
            source,
            stats.get("scanned", 0),
            stats.get("deduped", 0),
            stats.get("upserted", 0),
            stats.get("failed", 0),
            quarantined,
            skipped_no_judgment,
            int((time.time() - started_at) * 1000),
        )
        return stats

    MemorySqlManager._init_db = scoped_init_db
    MemorySqlManager._upsert_memories_by_judgment_sync = sync_upsert
    MemorySqlManager.upsert_memories_by_judgment = async_upsert
    SimpleMemoryBackupService.backup_from_collection = (
        secure_backup_from_collection
    )
    _INSTALLED = True
