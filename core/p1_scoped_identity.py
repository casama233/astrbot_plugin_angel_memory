"""P1 scope-aware identity handling for central memory writes.

The scoped backup migration defines ``(memory_scope, judgment)`` as the stable
identity boundary.  Normal create/mirror writes must honor the same boundary;
otherwise a legitimate duplicate observation raises SQLite UNIQUE errors.

This module makes create idempotent inside one scope, keeps the same judgment
independent across scopes, and removes implicit-public defaults from the
central SQL mutation API installed at runtime.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict, List, Optional

QUARANTINE_SCOPE = "__quarantine__"
_INSTALLED = False


def _clean(value: Any) -> str:
    return str(value or "").strip()


def find_scope_identity_id(manager: Any, memory_scope: Any, judgment: Any) -> str:
    """Return the canonical ID for one exact ``(scope, judgment)`` pair."""

    scope = manager._normalize_scope(memory_scope)
    text = _clean(judgment)
    if not text:
        return ""
    with manager._connect() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM memory_records
            WHERE memory_scope = ? AND judgment = ?
            ORDER BY created_at ASC, rowid ASC
            LIMIT 1
            """,
            (scope, text),
        ).fetchone()
    if row is None:
        return ""
    try:
        return _clean(row["id"])
    except (TypeError, KeyError, IndexError):
        return _clean(row[0])


def load_canonical_memory(manager: Any, memory_id: Any) -> Any:
    canonical_id = _clean(memory_id)
    if not canonical_id:
        return None
    memories = manager._get_memories_by_ids_sync([canonical_id])
    return memories[0] if memories else None


def idempotent_remember_sync(
    manager: Any,
    original_remember_sync: Any,
    memory_type: str,
    judgment: str,
    reasoning: str,
    tags: List[str],
    is_active: bool = False,
    strength: Optional[int] = None,
    memory_scope: str = "",
) -> Any:
    """Create once per exact scope+judgment and survive concurrent duplicates."""

    scope = manager._normalize_scope(memory_scope)
    if scope == QUARANTINE_SCOPE:
        raise ValueError("禁止向隔离域创建正常记忆")
    text = _clean(judgment)
    if not text:
        raise ValueError("judgment 为空，拒绝写入")

    existing_id = find_scope_identity_id(manager, scope, text)
    if existing_id:
        existing = load_canonical_memory(manager, existing_id)
        if existing is not None:
            manager.logger.info(
                "[memory_scope身份] create 命中现有记忆，按 scope+judgment 幂等返回 "
                "scope=%s id=%s",
                scope,
                existing_id,
            )
            return existing

    try:
        return original_remember_sync(
            manager,
            memory_type,
            text,
            reasoning,
            tags,
            is_active,
            strength,
            scope,
        )
    except sqlite3.IntegrityError:
        # A concurrent writer may have inserted the same identity after our
        # pre-check.  Only swallow the error if that exact identity now exists.
        canonical_id = find_scope_identity_id(manager, scope, text)
        canonical = load_canonical_memory(manager, canonical_id)
        if canonical is None:
            raise
        manager.logger.info(
            "[memory_scope身份] 并发 create 竞争已收敛到现有记忆 "
            "scope=%s id=%s",
            scope,
            canonical_id,
        )
        return canonical


def idempotent_upsert_memory_sync(
    manager: Any,
    original_upsert_memory_sync: Any,
    memory: Any,
) -> Any:
    """Prefer the canonical scope+judgment row if a mirror uses another ID."""

    scope = manager._normalize_scope(getattr(memory, "memory_scope", ""))
    if scope == QUARANTINE_SCOPE:
        raise ValueError("禁止通过正常镜像入口写入隔离域")
    judgment = _clean(getattr(memory, "judgment", ""))
    if not judgment:
        raise ValueError("judgment 为空，拒绝镜像写入")

    incoming_id = _clean(getattr(memory, "id", ""))
    canonical_id = find_scope_identity_id(manager, scope, judgment)
    if canonical_id and canonical_id != incoming_id:
        canonical = load_canonical_memory(manager, canonical_id)
        if canonical is not None:
            manager.logger.warning(
                "[memory_scope身份] 镜像 ID 与现有 scope+judgment 身份冲突；"
                "保留中央库 canonical id=%s，忽略 incoming id=%s scope=%s",
                canonical_id,
                incoming_id or "(空)",
                scope,
            )
            return canonical

    try:
        return original_upsert_memory_sync(manager, memory)
    except sqlite3.IntegrityError:
        canonical_id = find_scope_identity_id(manager, scope, judgment)
        canonical = load_canonical_memory(manager, canonical_id)
        if canonical is None:
            raise
        manager.logger.warning(
            "[memory_scope身份] 并发镜像冲突已收敛到 canonical id=%s scope=%s",
            canonical_id,
            scope,
        )
        return canonical


def install_scoped_identity_writes() -> None:
    """Install idempotent scoped writes after the P0 guards are active."""

    global _INSTALLED
    if _INSTALLED:
        return

    from ..llm_memory.components.memory_sql_manager import MemorySqlManager
    from ..llm_memory.models.data_models import ValidationError

    original_remember_sync = MemorySqlManager._remember_sync
    original_upsert_memory_sync = MemorySqlManager._upsert_memory_sync
    original_process_feedback = MemorySqlManager.process_feedback

    def secure_remember_sync(
        self: Any,
        memory_type: str,
        judgment: str,
        reasoning: str,
        tags: List[str],
        is_active: bool = False,
        strength: Optional[int] = None,
        memory_scope: str = "",
    ) -> Any:
        try:
            return idempotent_remember_sync(
                self,
                original_remember_sync,
                memory_type,
                judgment,
                reasoning,
                tags,
                is_active,
                strength,
                memory_scope,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    async def secure_remember(
        self: Any,
        memory_type: str,
        judgment: str,
        reasoning: str,
        tags: List[str],
        is_active: bool = False,
        strength: Optional[int] = None,
        memory_scope: str = "",
    ) -> Any:
        scope = _clean(memory_scope)
        if not scope or scope == QUARANTINE_SCOPE:
            raise ValidationError("memory_scope 未明确解析，拒绝创建记忆")
        return await asyncio.to_thread(
            self._remember_sync,
            memory_type,
            judgment,
            reasoning,
            tags,
            is_active,
            strength,
            scope,
        )

    def secure_upsert_memory_sync(self: Any, memory: Any) -> Any:
        try:
            return idempotent_upsert_memory_sync(
                self,
                original_upsert_memory_sync,
                memory,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    async def secure_upsert_memory(self: Any, memory: Any) -> Any:
        return await asyncio.to_thread(self._upsert_memory_sync, memory)

    async def explicit_scope_process_feedback(
        self: Any,
        useful_memory_ids: Optional[List[str]] = None,
        recalled_memory_ids: Optional[List[str]] = None,
        memory_actions: Optional[List[Dict[str, Any]]] = None,
        memory_scope: str = "",
    ) -> List[Any]:
        scope = _clean(memory_scope)
        if not scope or scope == QUARANTINE_SCOPE:
            raise ValidationError("memory_scope 未明确解析，拒绝反馈写入")
        return await original_process_feedback(
            self,
            useful_memory_ids=useful_memory_ids,
            recalled_memory_ids=recalled_memory_ids,
            memory_actions=memory_actions,
            memory_scope=scope,
        )

    MemorySqlManager._remember_sync = secure_remember_sync
    MemorySqlManager.remember = secure_remember
    MemorySqlManager._upsert_memory_sync = secure_upsert_memory_sync
    MemorySqlManager.upsert_memory = secure_upsert_memory
    MemorySqlManager.process_feedback = explicit_scope_process_feedback
    _INSTALLED = True
