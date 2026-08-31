"""Post-install hardening for the P1 scoped-notes runtime.

The main P1 module intentionally owns the storage and tool integration.  This
small layer closes two runtime edges found during final review:

* an unresolved persona scope must skip memory/note retrieval without breaking
  the user's chat request; and
* the note reader's character budget must also apply to a single very long
  physical line.

It is installed only after :func:`install_scoped_notes`.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, Mapping, Optional

_LOGGER = logging.getLogger(__name__)
_INSTALL_MARKER = "_angel_scoped_notes_hardening_installed"
_ORIGINAL_RETRIEVE_ATTR = "_angel_scoped_notes_legacy_retrieve"


def empty_retrieval_result() -> Dict[str, Any]:
    """Return the complete retrieval shape while disclosing no scoped data."""

    return {
        "long_term_memories": [],
        "candidate_notes": [],
        "note_id_mapping": {},
        "memory_id_mapping": {},
        "secretary_decision": {},
        "core_topic": "",
    }


def format_note_read_result_bounded(
    *,
    note_short_id: int,
    row: Mapping[str, Any],
    text: str,
    offset: int,
    limit: Optional[int],
    max_chars: int,
) -> str:
    """Format a note read while enforcing a hard content-character ceiling.

    The previous formatter checked the budget only after at least one line had
    been selected.  A minified or generated note containing one multi-megabyte
    line could therefore bypass ``MAX_CHARS``.  This implementation counts
    every returned content character, including line separators, and truncates
    an overlong physical line rather than returning it whole.
    """

    safe_max_chars = max(1, int(max_chars or 1))
    lines = str(text).splitlines()
    total_lines = len(lines)
    start = max(1, int(offset))
    rel_path = str(row.get("source_file_path") or "")
    memory_scope = str(row.get("memory_scope") or "")

    if start > total_lines:
        return (
            "[angel_note_read]\n"
            f"note_short_id: {int(note_short_id)}\n"
            f"source_file_path: {rel_path}\n"
            f"memory_scope: {memory_scope}\n"
            f"total_lines: {total_lines}\n"
            f"offset: {start}\n"
            "内容已到末尾，无更多行。"
        )

    line_limit = max(1, int(limit)) if limit is not None else max(1, total_lines)
    selected: list[str] = []
    content_chars = 0
    index = start - 1
    truncated_line = False

    while index < total_lines and len(selected) < line_limit:
        line = lines[index]
        separator_chars = 1 if selected else 0
        remaining = safe_max_chars - content_chars - separator_chars
        if remaining < 0:
            break

        if len(line) > remaining:
            # ``remaining`` can be zero when earlier complete lines exactly
            # consumed the budget.  In that case, do not add an empty phantom
            # line; simply report that more content exists.
            if remaining > 0:
                selected.append(line[:remaining])
                content_chars += separator_chars + remaining
                index += 1
                truncated_line = True
            break

        selected.append(line)
        content_chars += separator_chars + len(line)
        index += 1

    actual_end = start + len(selected) - 1 if selected else start - 1
    has_more = truncated_line or index < total_lines
    result = (
        "[angel_note_read]\n"
        f"note_short_id: {int(note_short_id)}\n"
        f"source_file_path: {rel_path}\n"
        f"memory_scope: {memory_scope}\n"
        f"total_lines: {total_lines}\n"
        f"returned: L{start}-{actual_end}\n"
        f"has_more: {'true' if has_more else 'false'}"
    )
    if truncated_line:
        result += (
            "\nline_truncated_by_char_limit: true"
            "\n超长单行已按安全字符上限截断；请先拆分该行再读取完整正文。"
        )
    elif has_more:
        result += f" (继续读取请用 offset={actual_end + 1})"

    return result + "\n\n" + "\n".join(selected)


def _find_legacy_retrieve(installed_retrieve: Any) -> Any:
    """Recover the pre-P1 retrieval callable captured by the scoped wrapper."""

    if not inspect.isfunction(installed_retrieve):
        raise RuntimeError("P1 scoped retrieval 不是可审计的 Python 函数")
    closure = inspect.getclosurevars(installed_retrieve)
    legacy = closure.nonlocals.get("original_retrieve")
    if not callable(legacy):
        raise RuntimeError(
            "无法定位 P1 scoped retrieval 的原始调用链；拒绝静默安装 hardening"
        )
    return legacy


def install_scoped_notes_hardening(
    scoped_module: Any = None,
    retrieval_cls: Any = None,
) -> None:
    """Install bounded note reads and graceful fail-closed retrieval.

    ``scoped_module`` and ``retrieval_cls`` are injectable for regression tests;
    production callers should omit them.
    """

    if scoped_module is None:
        from . import p1_scoped_notes as scoped_module
    if retrieval_cls is None:
        from .services.retrieval_service import DeepMindRetrievalService

        retrieval_cls = DeepMindRetrievalService

    if bool(getattr(retrieval_cls, _INSTALL_MARKER, False)):
        return

    installed_retrieve = getattr(
        retrieval_cls,
        "retrieve_memories_and_notes",
        None,
    )
    legacy_retrieve = _find_legacy_retrieve(installed_retrieve)

    async def hardened_retrieve(
        self: Any,
        event: Any,
        query: str,
        precompute_vectors: bool = False,
    ) -> Dict[str, Any]:
        deepmind = getattr(self, "deepmind", None)
        plugin_context = getattr(deepmind, "plugin_context", None)
        logger = getattr(deepmind, "logger", _LOGGER)
        if plugin_context is None:
            logger.error("scoped retrieval 缺少 plugin_context，已安全跳过")
            return empty_retrieval_result()

        try:
            scope = await plugin_context.resolve_memory_scope_from_event(event)
            scope = scoped_module.normalize_scope(scope)
        except Exception as exc:
            logger.error(
                "无法验证当前人格记忆域，已跳过全部记忆与笔记检索: %s",
                exc,
            )
            return empty_retrieval_result()

        token = scoped_module._CURRENT_NOTE_SCOPE.set(scope)
        try:
            return await legacy_retrieve(
                self,
                event,
                query,
                precompute_vectors,
            )
        except scoped_module.NoteScopeError as exc:
            logger.error("scoped retrieval 在执行期间失去可信 scope: %s", exc)
            return empty_retrieval_result()
        finally:
            scoped_module._CURRENT_NOTE_SCOPE.reset(token)

    setattr(
        retrieval_cls,
        _ORIGINAL_RETRIEVE_ATTR,
        installed_retrieve,
    )
    retrieval_cls.retrieve_memories_and_notes = hardened_retrieve
    setattr(retrieval_cls, _INSTALL_MARKER, True)

    # The scoped note-read tool resolves this global at call time, so replacing
    # it here hardens existing tool instances as well as future ones.
    scoped_module._format_note_read_result = format_note_read_result_bounded
