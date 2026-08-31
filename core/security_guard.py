"""P0 containment guards for memory-scope isolation.

The current storage model still has a few global indexes and legacy migration
paths. These guards fail closed around those paths until scope-aware schemas
are implemented natively. They are deliberately conservative: unsafe backup
replay and legacy notes are paused rather than guessed.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

QUARANTINE_SCOPE = "__quarantine__"
LEGACY_NOTES_DISABLED_MESSAGE = (
    "安全模式：旧版笔记索引尚未按记忆域隔离，笔记读写与检索已暂时停用。"
)
BACKUP_REPLAY_DISABLED_MESSAGE = (
    "安全模式：旧版向量记忆回灌尚未实现按记忆域去重，已暂时停用。"
)

_INSTALLED = False


class MemoryScopeResolutionError(ValueError):
    """Raised when no explicit, validated memory scope can be resolved."""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_resolved_scope(
    scope: str,
    *,
    source: str,
    scope_pattern: Any = None,
) -> str:
    normalized = _clean_text(scope)
    if not normalized:
        raise MemoryScopeResolutionError(f"{source} 得到空 memory_scope")
    if normalized == QUARANTINE_SCOPE:
        raise MemoryScopeResolutionError(
            f"{source} 不得绑定保留隔离域 {QUARANTINE_SCOPE}"
        )
    if scope_pattern is not None and not scope_pattern.fullmatch(normalized):
        raise MemoryScopeResolutionError(
            f"{source} 得到非法 memory_scope: {normalized}"
        )
    return normalized


def resolve_scope_from_mapping(
    conversation_id: Any,
    persona_name: Any,
    scope_map: Mapping[str, str],
    default_scope: Any = "",
    *,
    scope_pattern: Any = None,
) -> Tuple[str, str, str]:
    """Resolve persona -> conversation -> explicit default, never implicit public."""

    normalized_id = _clean_text(conversation_id)
    if not normalized_id:
        raise MemoryScopeResolutionError(
            "conversation_id 为空，无法解析 memory_scope"
        )

    normalized_persona = _clean_text(persona_name)
    if normalized_persona:
        scope = _clean_text(scope_map.get(normalized_persona, ""))
        if scope:
            scope = _validate_resolved_scope(
                scope,
                source="人格映射",
                scope_pattern=scope_pattern,
            )
            return scope, "persona", normalized_persona

    scope = _clean_text(scope_map.get(normalized_id, ""))
    if scope:
        scope = _validate_resolved_scope(
            scope,
            source="会话映射",
            scope_pattern=scope_pattern,
        )
        return scope, "conversation", normalized_id

    explicit_default = _clean_text(default_scope) or _clean_text(
        scope_map.get("__default__", "")
    )
    if explicit_default:
        explicit_default = _validate_resolved_scope(
            explicit_default,
            source="显式默认记忆域",
            scope_pattern=scope_pattern,
        )
        return explicit_default, "explicit_default", explicit_default

    persona_hint = normalized_persona or "(空)"
    raise MemoryScopeResolutionError(
        "memory_scope 未命中且没有显式默认值；"
        f"conversation_id={normalized_id} persona={persona_hint}"
    )


def scope_is_readable(record_scope: Any, current_scope: Any) -> bool:
    """Private scopes may read public; public may never read private."""

    record = _clean_text(record_scope)
    current = _clean_text(current_scope)
    if not record or not current:
        return False
    if current == "public":
        return record == "public"
    return record in {current, "public"}


def scope_is_owned(record_scope: Any, current_scope: Any) -> bool:
    """Mutations require exact ownership; inherited public is read-only."""

    record = _clean_text(record_scope)
    current = _clean_text(current_scope)
    return bool(record and current and record == current)


def _filter_hits_by_scope(
    manager: Any,
    hits: Iterable[Mapping[str, Any]],
    memory_scope: str,
) -> List[Dict[str, Any]]:
    """Filter candidate IDs before their document text is built for rerank."""

    hit_list = [dict(item) for item in (hits or [])]
    ids = list(
        dict.fromkeys(
            _clean_text(item.get("id"))
            for item in hit_list
            if _clean_text(item.get("id"))
        )
    )
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    with manager._connect() as conn:
        rows = conn.execute(
            f"SELECT id, memory_scope FROM memory_records "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    allowed = {
        _clean_text(row["id"])
        for row in rows
        if scope_is_readable(row["memory_scope"], memory_scope)
    }
    return [
        item
        for item in hit_list
        if _clean_text(item.get("id")) in allowed
    ]


def _owned_ids(
    manager: Any,
    memory_ids: Iterable[Any],
    memory_scope: str,
) -> List[str]:
    ids = list(
        dict.fromkeys(
            _clean_text(memory_id)
            for memory_id in (memory_ids or [])
            if _clean_text(memory_id)
        )
    )
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    with manager._connect() as conn:
        rows = conn.execute(
            f"SELECT id, memory_scope FROM memory_records "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    owned = {
        _clean_text(row["id"])
        for row in rows
        if scope_is_owned(row["memory_scope"], memory_scope)
    }
    return [memory_id for memory_id in ids if memory_id in owned]


def _install_plugin_context_guard() -> None:
    from .plugin_context import PluginContext

    def strict_resolve_memory_scope_with_source(
        self: Any,
        conversation_id: str,
        persona_name: str = "",
    ) -> Tuple[str, str, str]:
        return resolve_scope_from_mapping(
            conversation_id=conversation_id,
            persona_name=persona_name,
            scope_map=self._conversation_scope_map,
            default_scope=self.config.get("default_memory_scope", ""),
            scope_pattern=self._scope_name_pattern,
        )

    PluginContext.resolve_memory_scope_with_source = (
        strict_resolve_memory_scope_with_source
    )


def _install_memory_sql_guards() -> None:
    from ..llm_memory.components.memory_sql_manager import MemorySqlManager
    from ..llm_memory.models.data_models import ValidationError

    def get_full_id_for_scope(
        self: Any,
        short_id: str,
        memory_scope: str,
        *,
        allow_public_read: bool = False,
    ) -> str:
        full_id = self.get_full_id(_clean_text(short_id))
        if not full_id:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT memory_scope FROM memory_records "
                "WHERE id = ? LIMIT 1",
                (full_id,),
            ).fetchone()
        if row is None:
            return ""
        record_scope = _clean_text(row["memory_scope"])
        allowed = (
            scope_is_readable(record_scope, memory_scope)
            if allow_public_read
            else scope_is_owned(record_scope, memory_scope)
        )
        return full_id if allowed else ""

    async def disabled_unsafe_backup_upsert(
        self: Any,
        memories: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        self.logger.error(BACKUP_REPLAY_DISABLED_MESSAGE)
        return {
            "scanned": len(memories or []),
            "deduped": 0,
            "upserted": 0,
            "failed": len(memories or []),
        }

    def disabled_unsafe_backup_upsert_sync(
        self: Any,
        memories: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        self.logger.error(BACKUP_REPLAY_DISABLED_MESSAGE)
        return {
            "scanned": len(memories or []),
            "deduped": 0,
            "upserted": 0,
            "failed": len(memories or []),
        }

    async def secure_recall_by_tags(
        self: Any,
        query: str,
        limit: int,
        memory_scope: str,
        vector_scores: Optional[Dict[str, float]] = None,
    ) -> List[Any]:
        """Scope-filter FTS/vector candidates before optional rerank."""

        text = _clean_text(query)
        if not text:
            return []
        resolved_scope = self._normalize_scope(memory_scope)
        self._ensure_fts_ready_sync()
        candidate_limit = max(20, int(limit) * 20)
        bm25_limit = max(50, int(limit) * 30)

        def scoped_bm25(q: str, k: int) -> List[Dict[str, Any]]:
            raw = self._fts_retriever.search_memory_bm25_only(
                query=q,
                limit=k,
            )
            return _filter_hits_by_scope(self, raw, resolved_scope)

        def scoped_fusion(
            q: str,
            k: int,
            bm25_k: int,
            scores: Optional[Dict[str, float]],
        ) -> List[Dict[str, Any]]:
            raw = self._fts_retriever.search_memory(
                query=q,
                limit=k,
                fts_limit=bm25_k,
                fts_weight=0.3,
                vector_weight=0.7,
                vector_scores=scores,
            )
            return _filter_hits_by_scope(self, raw, resolved_scope)

        def scoped_doc_map(ids: List[str]) -> Dict[str, str]:
            safe_hits = _filter_hits_by_scope(
                self,
                [{"id": memory_id} for memory_id in ids],
                resolved_scope,
            )
            safe_ids = [_clean_text(item.get("id")) for item in safe_hits]
            return self._build_memory_doc_text_map_by_ids(safe_ids)

        hits = await self._hybrid_engine.search_with_strategy(
            query=text,
            limit=candidate_limit,
            candidate_limit=candidate_limit,
            bm25_limit=bm25_limit,
            vector_scores=vector_scores,
            bm25_only_search=scoped_bm25,
            fusion_search=scoped_fusion,
            build_doc_text_map=scoped_doc_map,
        )
        if not hits:
            return []

        ordered_ids = [
            _clean_text(item.get("id"))
            for item in hits
            if _clean_text(item.get("id"))
        ]
        score_map = {
            _clean_text(item.get("id")): float(
                item.get("final_score", 0.0)
            )
            for item in hits
            if _clean_text(item.get("id"))
        }
        score_kind_map = {
            _clean_text(item.get("id")): (
                _clean_text(item.get("score_kind")) or "normalized"
            )
            for item in hits
            if _clean_text(item.get("id"))
        }
        memories = self._get_memories_by_ids_sync(ordered_ids)
        memory_map = {memory.id: memory for memory in memories}

        ordered: List[Any] = []
        for memory_id in ordered_ids:
            memory = memory_map.get(memory_id)
            if memory is None:
                continue
            if not scope_is_readable(
                getattr(memory, "memory_scope", ""),
                resolved_scope,
            ):
                continue
            final_score = float(score_map.get(memory_id, 0.0))
            if (
                score_kind_map.get(memory_id, "normalized") != "rrf"
                and final_score < 0.5
            ):
                continue
            memory.similarity = final_score
            ordered.append(memory)
            if len(ordered) >= int(limit):
                break
        return ordered

    original_process_feedback = MemorySqlManager.process_feedback

    async def secure_process_feedback(
        self: Any,
        useful_memory_ids: Optional[List[str]] = None,
        recalled_memory_ids: Optional[List[str]] = None,
        memory_actions: Optional[List[Dict[str, Any]]] = None,
        memory_scope: str = "public",
    ) -> List[Any]:
        resolved_scope = self._normalize_scope(memory_scope)
        useful_ids = _owned_ids(
            self,
            useful_memory_ids or [],
            resolved_scope,
        )
        recalled_ids = _owned_ids(
            self,
            recalled_memory_ids or [],
            resolved_scope,
        )

        filtered_actions: List[Dict[str, Any]] = []
        rejected_actions = 0
        for action_data in memory_actions or []:
            if not isinstance(action_data, dict):
                continue
            action = _clean_text(action_data.get("action")).lower()
            if action not in {"merge", "updata"}:
                filtered_actions.append(action_data)
                continue

            source_ids = action_data.get("source_memory_ids", [])
            if not isinstance(source_ids, list) or not source_ids:
                rejected_actions += 1
                continue
            normalized_source_ids = list(
                dict.fromkeys(
                    _clean_text(memory_id)
                    for memory_id in source_ids
                    if _clean_text(memory_id)
                )
            )
            owned_source_ids = _owned_ids(
                self,
                normalized_source_ids,
                resolved_scope,
            )
            if owned_source_ids != normalized_source_ids:
                rejected_actions += 1
                continue

            copied = dict(action_data)
            copied["source_memory_ids"] = owned_source_ids
            filtered_actions.append(copied)

        if rejected_actions:
            self.logger.warning(
                "[memory_scope安全] 已拒绝 %s 个跨域 update/merge 动作 "
                "(scope=%s)",
                rejected_actions,
                resolved_scope,
            )

        return await original_process_feedback(
            self,
            useful_memory_ids=useful_ids,
            recalled_memory_ids=recalled_ids,
            memory_actions=filtered_actions,
            memory_scope=resolved_scope,
        )

    original_merge_group_sync = MemorySqlManager._merge_group_sync

    def secure_merge_group_sync(
        self: Any,
        memory_ids: List[str],
    ) -> Optional[Any]:
        memories = self._get_memories_by_ids_sync(memory_ids)
        scopes = {
            self._normalize_scope(
                getattr(memory, "memory_scope", "")
            )
            for memory in memories
        }
        if len(scopes) > 1:
            raise ValidationError("禁止合并不同 memory_scope 的记忆")
        return original_merge_group_sync(self, memory_ids)

    original_merge_action_sync = MemorySqlManager._merge_action_sync

    def secure_merge_action_sync(
        self: Any,
        source_memory_ids: List[str],
        memory_data: Dict[str, Any],
        resolved_scope: str,
    ) -> Optional[Any]:
        scope = self._normalize_scope(resolved_scope)
        source_ids = list(
            dict.fromkeys(
                _clean_text(memory_id)
                for memory_id in (source_memory_ids or [])
                if _clean_text(memory_id)
            )
        )
        if not source_ids:
            return None
        if _owned_ids(self, source_ids, scope) != source_ids:
            raise ValidationError(
                "源记忆不属于当前 memory_scope，拒绝修改"
            )
        return original_merge_action_sync(
            self,
            source_ids,
            memory_data,
            scope,
        )

    MemorySqlManager.get_full_id_for_scope = get_full_id_for_scope
    MemorySqlManager.upsert_memories_by_judgment = (
        disabled_unsafe_backup_upsert
    )
    MemorySqlManager._upsert_memories_by_judgment_sync = (
        disabled_unsafe_backup_upsert_sync
    )
    MemorySqlManager.recall_by_tags = secure_recall_by_tags
    MemorySqlManager.process_feedback = secure_process_feedback
    MemorySqlManager._merge_group_sync = secure_merge_group_sync
    MemorySqlManager._merge_action_sync = secure_merge_action_sync


def _install_backup_guard() -> None:
    from .services.simple_memory_backup_service import (
        SimpleMemoryBackupService,
    )

    async def disabled_backup_from_collection(
        self: Any,
        collection: Any,
        memory_sql_manager: Any,
        source: str,
        provider_id: str = "",
    ) -> Dict[str, int]:
        del collection, memory_sql_manager
        self.logger.error(
            "%s source=%s provider=%s",
            BACKUP_REPLAY_DISABLED_MESSAGE,
            source,
            provider_id or "unknown",
        )
        return {
            "scanned": 0,
            "deduped": 0,
            "upserted": 0,
            "failed": 0,
        }

    SimpleMemoryBackupService.backup_from_collection = (
        disabled_backup_from_collection
    )


def _install_note_containment() -> None:
    from .utils.memory_formatter import MemoryFormatter
    from ..llm_memory.service.note_service import NoteService
    from ..tools.angel_note_create import NoteCreateTool
    from ..tools.angel_note_read import NoteRecallTool
    from ..tools.angel_recall import CoreMemoryRecallTool

    async def disabled_note_tool(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        self.logger.warning(LEGACY_NOTES_DISABLED_MESSAGE)
        return LEGACY_NOTES_DISABLED_MESSAGE

    async def disabled_note_search(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        self.logger.warning(LEGACY_NOTES_DISABLED_MESSAGE)
        return []

    async def memory_only_recall(
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
            memory_scope = (
                await plugin_context.resolve_memory_scope_from_event(event)
            )
        except Exception as exc:
            self.logger.error(
                "%s: 获取安全上下文失败: %s",
                self.name,
                exc,
            )
            return "错误：无法确定当前会话，检索已拒绝。"

        try:
            memories = await memory_runtime.comprehensive_recall(
                query=query_text,
                limit=50,
                event=event,
                memory_scope=memory_scope,
            )
            active = [memory for memory in memories if memory.is_active]
            if active:
                manager = plugin_context.get_component(
                    "memory_sql_manager"
                )
                memory_text = MemoryFormatter.format_session_memories(
                    active,
                    short_id_registry=manager,
                )
                memory_part = (
                    f"[记忆检索结果 ({len(active)}条)]\n{memory_text}"
                )
            else:
                memory_part = "[记忆检索结果]\n无相关记忆。"
        except Exception as exc:
            self.logger.error(
                "%s: 记忆检索失败: %s",
                self.name,
                exc,
                exc_info=True,
            )
            memory_part = f"[记忆检索结果]\n检索失败：{exc}"

        return (
            f"{memory_part}\n\n[笔记检索结果]\n"
            f"{LEGACY_NOTES_DISABLED_MESSAGE}"
        )

    NoteService.search_notes = disabled_note_search
    NoteService.search_notes_by_top_k = disabled_note_search
    NoteCreateTool.run = disabled_note_tool
    NoteRecallTool.run = disabled_note_tool
    CoreMemoryRecallTool.run = memory_only_recall


def _install_remember_tool_guard() -> None:
    from ..tools.angel_remember import CoreMemoryRememberTool

    original_run = CoreMemoryRememberTool.run

    async def secure_run(
        self: Any,
        event: Any,
        action: str,
        memory: dict,
        source_memory_ids: Optional[List[str]] = None,
    ) -> Any:
        normalized_action = _clean_text(action).lower()
        source_ids = source_memory_ids or []
        if normalized_action in {"update", "merge"} and source_ids:
            plugin_context = getattr(event, "plugin_context", None)
            if plugin_context is None:
                return "错误：无法获取插件上下文，记忆修改已拒绝。"
            try:
                scope = (
                    await plugin_context.resolve_memory_scope_from_event(event)
                )
                manager = plugin_context.get_component(
                    "memory_sql_manager"
                )
                if manager is None:
                    raise RuntimeError("memory_sql_manager 不可用")
                for short_id in source_ids:
                    full_id = manager.get_full_id_for_scope(
                        _clean_text(short_id),
                        scope,
                        allow_public_read=False,
                    )
                    if not full_id:
                        return (
                            "错误：源记忆不属于当前记忆域，修改已拒绝。"
                        )
            except Exception as exc:
                self.logger.error(
                    "%s: scope ownership check failed: %s",
                    self.name,
                    exc,
                )
                return "错误：无法验证源记忆归属，修改已拒绝。"

        return await original_run(
            self,
            event,
            action,
            memory,
            source_memory_ids,
        )

    CoreMemoryRememberTool.run = secure_run


def _install_deepmind_guards() -> None:
    from .deepmind import DeepMind

    original_init = DeepMind.__init__

    def secure_init(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_init(self, *args, **kwargs)
        requested = bool(
            getattr(self, "note_recall_enabled", False)
        )
        self.note_recall_enabled = False
        self.note_inject_top_k = 0
        self.note_candidate_top_k = 0
        if requested:
            self.logger.warning(LEGACY_NOTES_DISABLED_MESSAGE)

    original_build_input = DeepMind._build_reflection_input

    async def secure_build_reflection_input(
        self: Any,
        event: Any,
        response: Any,
        session_id: str,
    ) -> Any:
        scope = await self.plugin_context.resolve_memory_scope_from_event(event)
        result = await original_build_input(self, event, response, session_id)
        result.memory_scope = scope
        return result

    original_buffer = DeepMind._buffer_reflection_turn

    async def secure_buffer_reflection_turn(
        self: Any,
        event: Any,
        response: Any,
        session_id: str,
    ) -> None:
        try:
            await self.plugin_context.resolve_memory_scope_from_event(event)
        except Exception as exc:
            self.logger.warning(
                "[memory_scope安全] 反思入池已拒绝 "
                "session=%s error=%s",
                session_id,
                exc,
            )
            return
        await original_buffer(self, event, response, session_id)

    DeepMind.__init__ = secure_init
    DeepMind._build_reflection_input = secure_build_reflection_input
    DeepMind._buffer_reflection_turn = secure_buffer_reflection_turn


def install_security_guards() -> None:
    """Install all P0 guards once per interpreter process."""

    global _INSTALLED
    if _INSTALLED:
        return

    _install_plugin_context_guard()
    _install_memory_sql_guards()
    _install_backup_guard()
    _install_note_containment()
    _install_remember_tool_guard()
    _install_deepmind_guards()
    _INSTALLED = True
