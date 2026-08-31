from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "p1_scoped_notes_hardening.py"
if not MODULE_PATH.exists():
    MODULE_PATH = Path("/tmp/p1_scoped_notes_hardening.py")

spec = importlib.util.spec_from_file_location(
    "angel_memory_p1_scoped_notes_hardening",
    MODULE_PATH,
)
assert spec and spec.loader
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class FakeNoteScopeError(ValueError):
    pass


class FakeScopedModule:
    NoteScopeError = FakeNoteScopeError
    _CURRENT_NOTE_SCOPE = contextvars.ContextVar("test_note_scope", default=None)
    _format_note_read_result = staticmethod(lambda **kwargs: "unsafe")

    @staticmethod
    def normalize_scope(value):
        value = str(value or "").strip()
        if not value or value == "__quarantine__":
            raise FakeNoteScopeError("invalid scope")
        return value


class FakeLogger:
    def __init__(self):
        self.messages = []

    def error(self, message, *args):
        self.messages.append(message % args if args else message)


class FakePluginContext:
    async def resolve_memory_scope_from_event(self, event):
        if isinstance(event, Exception):
            raise event
        return event.scope


class Event:
    def __init__(self, scope):
        self.scope = scope


class HardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Each test gets a fresh class because the installer records a class
        # marker and is intentionally idempotent.
        self.legacy_calls = 0
        test_case = self

        async def legacy_retrieve(
            service,
            event,
            query,
            precompute_vectors=False,
        ):
            del service, event, query, precompute_vectors
            test_case.legacy_calls += 1
            await asyncio.sleep(0)
            return {
                "scope_seen": FakeScopedModule._CURRENT_NOTE_SCOPE.get(),
            }

        def build_scoped_wrapper():
            original_retrieve = legacy_retrieve

            async def scoped_retrieve(
                self,
                event,
                query,
                precompute_vectors=False,
            ):
                return await original_retrieve(
                    self,
                    event,
                    query,
                    precompute_vectors,
                )

            return scoped_retrieve

        class RetrievalService:
            retrieve_memories_and_notes = build_scoped_wrapper()

            def __init__(self):
                self.deepmind = types.SimpleNamespace(
                    plugin_context=FakePluginContext(),
                    logger=FakeLogger(),
                )

        self.RetrievalService = RetrievalService
        h.install_scoped_notes_hardening(
            FakeScopedModule,
            self.RetrievalService,
        )

    async def test_scope_failure_returns_empty_shape_without_calling_legacy(self):
        service = self.RetrievalService()
        result = await service.retrieve_memories_and_notes(
            RuntimeError("missing persona"),
            "query",
        )
        self.assertEqual(result, h.empty_retrieval_result())
        self.assertEqual(self.legacy_calls, 0)
        self.assertTrue(service.deepmind.logger.messages)
        self.assertIsNone(FakeScopedModule._CURRENT_NOTE_SCOPE.get())

    async def test_valid_scope_reaches_legacy_and_context_is_reset(self):
        service = self.RetrievalService()
        result = await service.retrieve_memories_and_notes(
            Event("xiaotai"),
            "query",
        )
        self.assertEqual(result["scope_seen"], "xiaotai")
        self.assertEqual(self.legacy_calls, 1)
        self.assertIsNone(FakeScopedModule._CURRENT_NOTE_SCOPE.get())

    async def test_hardened_scope_context_remains_task_local(self):
        first = self.RetrievalService()
        second = self.RetrievalService()
        first_result, second_result = await asyncio.gather(
            first.retrieve_memories_and_notes(Event("xiaotai"), "q"),
            second.retrieve_memories_and_notes(Event("other"), "q"),
        )
        self.assertEqual(first_result["scope_seen"], "xiaotai")
        self.assertEqual(second_result["scope_seen"], "other")
        self.assertIsNone(FakeScopedModule._CURRENT_NOTE_SCOPE.get())

    async def test_installer_is_idempotent(self):
        first = self.RetrievalService.retrieve_memories_and_notes
        h.install_scoped_notes_hardening(
            FakeScopedModule,
            self.RetrievalService,
        )
        self.assertIs(first, self.RetrievalService.retrieve_memories_and_notes)

    def test_single_long_line_cannot_bypass_character_cap(self):
        result = h.format_note_read_result_bounded(
            note_short_id=7,
            row={
                "source_file_path": ".angel/scoped/s-cHVibGlj/note/long.md",
                "memory_scope": "public",
            },
            text="x" * 100_000,
            offset=1,
            limit=None,
            max_chars=30_000,
        )
        body = result.split("\n\n", 1)[1]
        self.assertEqual(len(body), 30_000)
        self.assertIn("line_truncated_by_char_limit: true", result)
        self.assertIn("has_more: true", result)

    def test_multiline_output_also_stays_within_character_cap(self):
        result = h.format_note_read_result_bounded(
            note_short_id=8,
            row={"source_file_path": "safe.md", "memory_scope": "public"},
            text="12345\n67890\nabcde",
            offset=1,
            limit=None,
            max_chars=11,
        )
        body = result.split("\n\n", 1)[1]
        self.assertLessEqual(len(body), 11)
        self.assertEqual(body, "12345\n67890")
        self.assertIn("has_more: true", result)
        self.assertIn("offset=3", result)

    def test_rejects_install_when_expected_scoped_wrapper_shape_changed(self):
        class UnexpectedRetrieval:
            async def retrieve_memories_and_notes(self, event, query):
                del self, event, query
                return {}

        with self.assertRaises(RuntimeError):
            h.install_scoped_notes_hardening(
                FakeScopedModule,
                UnexpectedRetrieval,
            )


if __name__ == "__main__":
    unittest.main()
