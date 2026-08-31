from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_module(
    "angel_memory_p0_note_api_guard",
    "core/p0_note_api_guard.py",
)


class _FakeNotesAPI:
    async def browse_notes(self):
        return {"leaked": "browse"}, 200

    async def recall_note(self):
        return {"leaked": "read"}, 200

    async def list_note_files(self):
        return {"files": ["private.md"]}, 200

    async def get_file_content(self):
        return {"content": "private"}, 200

    async def search_chunks(self):
        return {"items": [{"content": "private"}]}, 200

    async def chunk_stats(self):
        return {"chunk_count": 42}, 200

    async def unrelated_health(self):
        return {"ok": True}, 200


class NoteApiContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_content_bearing_endpoints_fail_closed(self):
        patched = guard.install_note_api_containment(_FakeNotesAPI)
        self.assertEqual(set(patched), set(guard.PROTECTED_NOTE_API_METHODS))

        api = _FakeNotesAPI()
        for method_name in guard.PROTECTED_NOTE_API_METHODS:
            payload, status = await getattr(api, method_name)()
            self.assertEqual(status, 403, method_name)
            self.assertEqual(payload["code"], guard.NOTE_API_ERROR_CODE)
            self.assertTrue(payload["scope_required"])
            self.assertFalse(payload["retryable"])
            serialized = repr(payload).lower()
            self.assertNotIn("private.md", serialized)
            self.assertNotIn("chunk_count", serialized)

    async def test_unrelated_endpoint_is_not_modified(self):
        guard.install_note_api_containment(_FakeNotesAPI)
        payload, status = await _FakeNotesAPI().unrelated_health()
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})

    def test_original_callables_are_retained_for_scoped_migration(self):
        guard.install_note_api_containment(_FakeNotesAPI)
        originals = getattr(
            _FakeNotesAPI,
            "_angel_unscoped_note_api_originals",
        )
        self.assertEqual(set(originals), set(guard.PROTECTED_NOTE_API_METHODS))
        self.assertTrue(all(callable(value) for value in originals.values()))

    def test_install_is_idempotent(self):
        first = guard.install_note_api_containment(_FakeNotesAPI)
        first_callable = _FakeNotesAPI.browse_notes
        second = guard.install_note_api_containment(_FakeNotesAPI)
        self.assertEqual(first, second)
        self.assertIs(first_callable, _FakeNotesAPI.browse_notes)

    def test_refuses_silent_success_when_api_shape_is_unknown(self):
        class EmptyAPI:
            pass

        with self.assertRaises(RuntimeError):
            guard.install_note_api_containment(EmptyAPI)


if __name__ == "__main__":
    unittest.main()
