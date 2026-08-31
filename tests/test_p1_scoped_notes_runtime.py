from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "p1_scoped_notes.py"
if not MODULE_PATH.exists():
    MODULE_PATH = Path("/mnt/data/p1_scoped_notes.py")

PREFIX = "p1_runtime_fixture"


def package(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def module(name: str, **values) -> types.ModuleType:
    value = types.ModuleType(name)
    for key, item in values.items():
        setattr(value, key, item)
    sys.modules[name] = value
    return value


class FakeDeepMind:
    def __init__(self, config=None, plugin_context=None, *args, **kwargs):
        del args, kwargs
        self.config = config or {
            "note_assistant": {"enable_recall": True, "top_k": 4}
        }
        self.plugin_context = plugin_context
        # Simulate the preceding P0 wrapper.
        self.note_recall_enabled = False
        self.note_inject_top_k = 0
        self.note_candidate_top_k = 0


class FakeFileMonitorService:
    def start_monitoring(self):
        return "legacy-monitor"


class FakeRetrievalService:
    def __init__(self, deepmind):
        self.deepmind = deepmind

    async def retrieve_memories_and_notes(
        self,
        event,
        query,
        precompute_vectors=False,
    ):
        del event, query, precompute_vectors
        runtime_module = sys.modules[f"{PREFIX}.core.p1_scoped_notes"]
        await asyncio.sleep(0)
        return {"scope_seen": runtime_module.resolve_note_scope()}


class FakeMemoryFormatter:
    @staticmethod
    def format_session_memories(*args, **kwargs):
        del args, kwargs
        return "memories"


class FakeNoteService:
    def search_notes(self, *args, **kwargs):
        del args, kwargs
        return []

    def search_notes_by_top_k(self, *args, **kwargs):
        del args, kwargs
        return []

    def parse_and_store_file_sync(self, *args, **kwargs):
        del args, kwargs
        return 0, {}

    def remove_file_data(self, *args, **kwargs):
        del args, kwargs
        return True

    def remove_file_data_by_file_id(self, *args, **kwargs):
        del args, kwargs
        return True

    def close(self):
        return None


class FakeNoteCreateTool:
    async def run(self, *args, **kwargs):
        del args, kwargs
        return "p0-disabled"


class FakeNoteRecallTool:
    MAX_CHARS = 30000

    async def run(self, *args, **kwargs):
        del args, kwargs
        return "p0-disabled"


class FakeCoreMemoryRecallTool:
    async def run(self, *args, **kwargs):
        del args, kwargs
        return "memory-only"


class FakePluginContext:
    async def resolve_memory_scope_from_event(self, event):
        return event.scope


class Event:
    def __init__(self, scope: str):
        self.scope = scope


def load_runtime_module():
    for key in list(sys.modules):
        if key == PREFIX or key.startswith(PREFIX + "."):
            del sys.modules[key]

    package(PREFIX)
    package(f"{PREFIX}.core")
    package(f"{PREFIX}.core.services")
    package(f"{PREFIX}.core.utils")
    package(f"{PREFIX}.llm_memory")
    package(f"{PREFIX}.llm_memory.parser")
    package(f"{PREFIX}.llm_memory.service")
    package(f"{PREFIX}.tools")

    module(f"{PREFIX}.core.deepmind", DeepMind=FakeDeepMind)
    module(
        f"{PREFIX}.core.file_monitor",
        FileMonitorService=FakeFileMonitorService,
    )
    module(
        f"{PREFIX}.core.services.retrieval_service",
        DeepMindRetrievalService=FakeRetrievalService,
    )
    module(
        f"{PREFIX}.core.utils.memory_formatter",
        MemoryFormatter=FakeMemoryFormatter,
    )
    module(
        f"{PREFIX}.llm_memory.parser.note_chunker",
        chunk_file=lambda content, path: [
            {
                "chunk_index": 1,
                "line_start": 0,
                "line_end": 1,
                "content": f"{path}\n{content}",
            }
        ],
    )
    module(
        f"{PREFIX}.llm_memory.service.note_service",
        NoteService=FakeNoteService,
    )
    module(
        f"{PREFIX}.tools.angel_note_create",
        NoteCreateTool=FakeNoteCreateTool,
    )
    module(
        f"{PREFIX}.tools.angel_note_read",
        NoteRecallTool=FakeNoteRecallTool,
    )
    module(
        f"{PREFIX}.tools.angel_recall",
        CoreMemoryRecallTool=FakeCoreMemoryRecallTool,
    )

    name = f"{PREFIX}.core.p1_scoped_notes"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


class ScopedNotesRuntimeInstallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = load_runtime_module()
        self.runtime.install_scoped_notes()

    async def test_installer_replaces_all_p0_runtime_entrypoints(self):
        self.assertEqual(
            FakeNoteService.search_notes.__name__,
            "scoped_search_notes",
        )
        self.assertEqual(
            FakeNoteService.search_notes_by_top_k.__name__,
            "scoped_search_notes_by_top_k",
        )
        self.assertEqual(
            FakeNoteCreateTool.run.__name__,
            "scoped_note_create",
        )
        self.assertEqual(
            FakeNoteRecallTool.run.__name__,
            "scoped_note_read",
        )
        self.assertEqual(
            FakeCoreMemoryRecallTool.run.__name__,
            "scoped_core_recall",
        )
        self.assertEqual(
            FakeRetrievalService.retrieve_memories_and_notes.__name__,
            "scoped_retrieve",
        )

    async def test_installer_is_idempotent(self):
        first = FakeNoteService.search_notes
        self.runtime.install_scoped_notes()
        self.assertIs(first, FakeNoteService.search_notes)

    async def test_deepmind_p0_disable_is_restored_only_from_config(self):
        instance = FakeDeepMind(
            config={
                "note_assistant": {
                    "enable_recall": True,
                    "top_k": 5,
                }
            }
        )
        self.assertTrue(instance.note_recall_enabled)
        self.assertEqual(instance.note_inject_top_k, 5)
        self.assertEqual(instance.note_candidate_top_k, 35)

    async def test_retrieval_scope_context_is_concurrent_task_local(self):
        plugin_context = FakePluginContext()
        first = FakeRetrievalService(
            types.SimpleNamespace(plugin_context=plugin_context)
        )
        second = FakeRetrievalService(
            types.SimpleNamespace(plugin_context=plugin_context)
        )
        first_result, second_result = await asyncio.gather(
            first.retrieve_memories_and_notes(Event("xiaotai"), "q"),
            second.retrieve_memories_and_notes(Event("other"), "q"),
        )
        self.assertEqual(first_result["scope_seen"], "xiaotai")
        self.assertEqual(second_result["scope_seen"], "other")
        with self.assertRaises(self.runtime.NoteScopeError):
            self.runtime.resolve_note_scope()


if __name__ == "__main__":
    unittest.main()
