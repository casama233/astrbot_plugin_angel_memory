"""P0.1 fail-closed containment for the unscoped Notes Web API.

The legacy WebUI notes endpoints do not receive an AstrBot event, persona, or
conversation identity.  They therefore cannot prove which memory scope is
requesting a list, search, or file read.  Until a scoped Web API is introduced,
all content-bearing note endpoints must fail closed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple, Type

NOTE_API_ERROR_CODE = "NOTE_SCOPE_REQUIRED"
NOTE_API_DISABLED_MESSAGE = (
    "安全模式：笔记 Web API 无法验证当前人格或会话记忆域，"
    "浏览、搜索、读取与统计接口已暂时停用。"
)

PROTECTED_NOTE_API_METHODS: Tuple[str, ...] = (
    "browse_notes",
    "recall_note",
    "list_note_files",
    "get_file_content",
    "search_chunks",
    "chunk_stats",
)

_CLASS_GUARD_MARKER = "_angel_note_scope_guard_installed"
_CLASS_ORIGINALS_ATTR = "_angel_unscoped_note_api_originals"
_INSTALLED = False
_LOGGER = logging.getLogger(__name__)


def build_denied_response() -> Tuple[dict, int]:
    """Return a Quart-compatible JSON payload and HTTP status code."""

    return (
        {
            "error": NOTE_API_DISABLED_MESSAGE,
            "code": NOTE_API_ERROR_CODE,
            "scope_required": True,
            "retryable": False,
        },
        403,
    )


async def _deny_unscoped_note_api(
    self: Any,
    *args: Any,
    **kwargs: Any,
) -> Tuple[dict, int]:
    del self, args, kwargs
    _LOGGER.warning(NOTE_API_DISABLED_MESSAGE)
    return build_denied_response()


def install_note_api_containment(
    notes_api_cls: Optional[Type[Any]] = None,
) -> Tuple[str, ...]:
    """Replace every legacy content-bearing Notes API method with a denial.

    ``notes_api_cls`` is injectable so the guard can be regression-tested
    without importing Quart or AstrBot.  Original callables are retained on the
    class for the later scoped-API migration, but are never invoked by this P0
    guard.
    """

    global _INSTALLED

    is_runtime_install = notes_api_cls is None
    if is_runtime_install:
        if _INSTALLED:
            return PROTECTED_NOTE_API_METHODS
        from ..web_api.notes_api import NotesAPI

        notes_api_cls = NotesAPI

    assert notes_api_cls is not None
    if bool(getattr(notes_api_cls, _CLASS_GUARD_MARKER, False)):
        if is_runtime_install:
            _INSTALLED = True
        return PROTECTED_NOTE_API_METHODS

    originals = {}
    for method_name in PROTECTED_NOTE_API_METHODS:
        original = getattr(notes_api_cls, method_name, None)
        if original is None:
            continue
        originals[method_name] = original
        setattr(notes_api_cls, method_name, _deny_unscoped_note_api)

    if not originals:
        raise RuntimeError(
            "NotesAPI 未找到任何已知端点；无法证明旧笔记 API 已被安全封锁"
        )

    setattr(notes_api_cls, _CLASS_ORIGINALS_ATTR, originals)
    setattr(notes_api_cls, _CLASS_GUARD_MARKER, True)
    if is_runtime_install:
        _INSTALLED = True
    return tuple(originals.keys())
