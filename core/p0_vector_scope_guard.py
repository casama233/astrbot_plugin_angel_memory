"""P0 fail-closed guards for the legacy vector-only memory path.

The SQL-backed path already enforces exact mutation ownership.  The historical
vector-only fallback did not carry the current scope into ``merge_memories``
and treated missing stored scope metadata as public.  This module closes that
gap without changing the public tool contract.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

QUARANTINE_SCOPE = "__quarantine__"

_INSTALLED = False
_ACTIVE_MUTATION_SCOPE: ContextVar[str] = ContextVar(
    "angel_memory_vector_mutation_scope",
    default="",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_stored_scope(value: Any) -> str:
    """Missing legacy metadata is unknown, never implicitly public."""

    return _clean(value) or QUARANTINE_SCOPE


def scope_is_readable(record_scope: Any, current_scope: Any) -> bool:
    """Return the one-way read relation while denying unknown/quarantine rows."""

    record = normalize_stored_scope(record_scope)
    current = _clean(current_scope)
    if not current or current == QUARANTINE_SCOPE:
        return False
    if record == QUARANTINE_SCOPE:
        return False
    if current == "public":
        return record == "public"
    return record in {current, "public"}


def scope_is_owned(record_scope: Any, current_scope: Any) -> bool:
    """Mutations require exact scope ownership; inherited public is read-only."""

    record = normalize_stored_scope(record_scope)
    current = _clean(current_scope)
    return bool(
        current
        and current != QUARANTINE_SCOPE
        and record != QUARANTINE_SCOPE
        and record == current
    )


def _read_vector_record_scope(collection: Any, memory_id: Any) -> str:
    """Read one source row's scope from the legacy vector collection."""

    source_id = _clean(memory_id)
    if not source_id:
        return QUARANTINE_SCOPE

    try:
        result = collection.get(ids=[source_id], include=["metadatas"])
    except TypeError:
        # Some legacy Chroma wrappers/mocks do not accept ``include``.
        result = collection.get(ids=[source_id])
    except Exception:
        return QUARANTINE_SCOPE

    if not isinstance(result, Mapping):
        return QUARANTINE_SCOPE
    metadatas = result.get("metadatas") or []
    if not metadatas or not isinstance(metadatas[0], Mapping):
        return QUARANTINE_SCOPE
    return normalize_stored_scope(metadatas[0].get("memory_scope"))


def _filter_owned_ids(
    collection: Any,
    memory_ids: Optional[Iterable[Any]],
    current_scope: str,
) -> List[str]:
    owned: List[str] = []
    seen = set()
    for raw_id in memory_ids or []:
        memory_id = _clean(raw_id)
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        if scope_is_owned(
            _read_vector_record_scope(collection, memory_id),
            current_scope,
        ):
            owned.append(memory_id)
    return owned


def filter_vector_feedback_payload(
    collection: Any,
    current_scope: Any,
    useful_memory_ids: Optional[Iterable[Any]],
    recalled_memory_ids: Optional[Iterable[Any]],
    memory_actions: Optional[Iterable[Mapping[str, Any]]],
) -> Tuple[List[str], List[str], List[Dict[str, Any]], int]:
    """Drop all vector-mode mutations that are not owned by ``current_scope``."""

    scope = _clean(current_scope)
    if not scope or scope == QUARANTINE_SCOPE:
        raise ValueError("memory_scope 为空或为隔离域，拒绝反馈写入")

    useful = _filter_owned_ids(collection, useful_memory_ids, scope)
    recalled = _filter_owned_ids(collection, recalled_memory_ids, scope)

    filtered_actions: List[Dict[str, Any]] = []
    rejected = 0
    for raw_action in memory_actions or []:
        if not isinstance(raw_action, Mapping):
            continue
        action_data = dict(raw_action)
        action = _clean(action_data.get("action")).lower()
        if action not in {"merge", "updata"}:
            filtered_actions.append(action_data)
            continue

        raw_sources = action_data.get("source_memory_ids")
        if not isinstance(raw_sources, list) or not raw_sources:
            rejected += 1
            continue
        sources = list(
            dict.fromkeys(
                _clean(memory_id)
                for memory_id in raw_sources
                if _clean(memory_id)
            )
        )
        if not sources:
            rejected += 1
            continue
        owned_sources = _filter_owned_ids(collection, sources, scope)
        if owned_sources != sources:
            rejected += 1
            continue
        action_data["source_memory_ids"] = owned_sources
        filtered_actions.append(action_data)

    return useful, recalled, filtered_actions, rejected


def install_vector_scope_guards() -> None:
    """Install fail-closed ownership checks on the legacy vector-only path."""

    global _INSTALLED
    if _INSTALLED:
        return

    from ..llm_memory.models.data_models import ValidationError
    from ..llm_memory.service.memory_manager import MemoryManager

    original_build_memory = MemoryManager._build_memory_from_metadata
    original_process_feedback = MemoryManager.process_feedback
    original_merge_memories = MemoryManager.merge_memories

    def secure_is_scope_allowed(memory_scope: str, target_scope: str) -> bool:
        return scope_is_readable(memory_scope, target_scope)

    def secure_build_memory_from_metadata(
        self: Any,
        memory_id: str,
        metadata: Dict[str, Any],
    ) -> Any:
        safe_metadata = dict(metadata or {})
        safe_metadata["memory_scope"] = normalize_stored_scope(
            safe_metadata.get("memory_scope")
        )
        return original_build_memory(self, memory_id, safe_metadata)

    async def secure_process_feedback(
        self: Any,
        useful_memory_ids: Optional[List[str]] = None,
        recalled_memory_ids: Optional[List[str]] = None,
        memory_actions: Optional[List[dict]] = None,
        memory_handlers: Optional[Dict[str, object]] = None,
        memory_scope: str = "",
    ) -> List[Any]:
        resolved_scope = _clean(memory_scope)
        if not resolved_scope or resolved_scope == QUARANTINE_SCOPE:
            raise ValidationError("memory_scope 未明确解析，拒绝反馈写入")

        safe_useful = useful_memory_ids or []
        safe_recalled = recalled_memory_ids or []
        safe_actions = memory_actions or []

        if getattr(self, "memory_sql_manager", None) is None:
            safe_useful, safe_recalled, safe_actions, rejected = (
                filter_vector_feedback_payload(
                    self.collection,
                    resolved_scope,
                    useful_memory_ids,
                    recalled_memory_ids,
                    memory_actions,
                )
            )
            if rejected:
                self.logger.warning(
                    "[memory_scope安全] 旧向量模式已拒绝 %s 个跨域 update/merge 动作 "
                    "(scope=%s)",
                    rejected,
                    resolved_scope,
                )

        token = _ACTIVE_MUTATION_SCOPE.set(resolved_scope)
        try:
            return await original_process_feedback(
                self,
                useful_memory_ids=safe_useful,
                recalled_memory_ids=safe_recalled,
                memory_actions=safe_actions,
                memory_handlers=memory_handlers,
                memory_scope=resolved_scope,
            )
        finally:
            _ACTIVE_MUTATION_SCOPE.reset(token)

    async def secure_merge_memories(
        self: Any,
        memories_to_merge_ids: List[str],
        new_memory_type: str,
        new_judgment: str,
        new_reasoning: str,
        new_tags: List[str],
    ) -> Any:
        active_scope = _clean(_ACTIVE_MUTATION_SCOPE.get())
        if not active_scope or active_scope == QUARANTINE_SCOPE:
            raise ValidationError(
                "缺少可信的当前 memory_scope，禁止直接执行向量记忆合并"
            )
        source_ids = list(
            dict.fromkeys(
                _clean(memory_id)
                for memory_id in (memories_to_merge_ids or [])
                if _clean(memory_id)
            )
        )
        if not source_ids:
            raise ValidationError("至少需要1个有效记忆ID才能进行合并")
        if _filter_owned_ids(self.collection, source_ids, active_scope) != source_ids:
            raise ValidationError(
                "源记忆不属于当前 memory_scope，禁止向量模式更新/合并"
            )
        return await original_merge_memories(
            self,
            memories_to_merge_ids=source_ids,
            new_memory_type=new_memory_type,
            new_judgment=new_judgment,
            new_reasoning=new_reasoning,
            new_tags=new_tags,
        )

    MemoryManager._is_scope_allowed = staticmethod(secure_is_scope_allowed)
    MemoryManager._build_memory_from_metadata = secure_build_memory_from_metadata
    MemoryManager.process_feedback = secure_process_feedback
    MemoryManager.merge_memories = secure_merge_memories
    _INSTALLED = True
