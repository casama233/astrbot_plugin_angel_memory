from __future__ import annotations

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
    "angel_memory_p0_vector_scope_guard",
    "core/p0_vector_scope_guard.py",
)


class _Collection:
    def __init__(self, scopes):
        self.scopes = dict(scopes)

    def get(self, *, ids, include=None):
        del include
        memory_id = str(ids[0])
        if memory_id not in self.scopes:
            return {"ids": [], "metadatas": []}
        scope = self.scopes[memory_id]
        metadata = {} if scope is None else {"memory_scope": scope}
        return {"ids": [memory_id], "metadatas": [metadata]}


class VectorScopeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.collection = _Collection(
            {
                "pub": "public",
                "xiaotai": "xiaotai",
                "other": "other-private",
                "missing": None,
                "blank": "",
                "quarantine": guard.QUARANTINE_SCOPE,
            }
        )

    def test_missing_stored_scope_is_quarantine_not_public(self):
        self.assertEqual(
            guard.normalize_stored_scope(None),
            guard.QUARANTINE_SCOPE,
        )
        self.assertEqual(
            guard.normalize_stored_scope(""),
            guard.QUARANTINE_SCOPE,
        )
        self.assertFalse(guard.scope_is_readable(None, "public"))
        self.assertFalse(guard.scope_is_readable("", "xiaotai"))

    def test_read_inheritance_is_one_way(self):
        self.assertTrue(guard.scope_is_readable("public", "public"))
        self.assertFalse(guard.scope_is_readable("xiaotai", "public"))
        self.assertTrue(guard.scope_is_readable("public", "xiaotai"))
        self.assertTrue(guard.scope_is_readable("xiaotai", "xiaotai"))
        self.assertFalse(guard.scope_is_readable("other-private", "xiaotai"))

    def test_private_scope_cannot_mutate_inherited_public(self):
        useful, recalled, actions, rejected = guard.filter_vector_feedback_payload(
            self.collection,
            "xiaotai",
            ["pub", "xiaotai", "missing"],
            ["pub", "xiaotai", "other", "blank"],
            [
                {
                    "action": "updata",
                    "source_memory_ids": ["pub"],
                    "memory": {"judgment": "should be rejected"},
                },
                {
                    "action": "merge",
                    "source_memory_ids": ["xiaotai", "pub"],
                    "memory": {"judgment": "should be rejected"},
                },
                {
                    "action": "updata",
                    "source_memory_ids": ["xiaotai"],
                    "memory": {"judgment": "allowed"},
                },
                {
                    "action": "create",
                    "memory": {"judgment": "allowed create"},
                },
            ],
        )

        self.assertEqual(useful, ["xiaotai"])
        self.assertEqual(recalled, ["xiaotai"])
        self.assertEqual(rejected, 2)
        self.assertEqual(
            [item["action"] for item in actions],
            ["updata", "create"],
        )
        self.assertEqual(actions[0]["source_memory_ids"], ["xiaotai"])

    def test_public_scope_cannot_mutate_private(self):
        useful, recalled, actions, rejected = guard.filter_vector_feedback_payload(
            self.collection,
            "public",
            ["pub", "xiaotai"],
            ["xiaotai", "pub"],
            [
                {
                    "action": "merge",
                    "source_memory_ids": ["xiaotai"],
                    "memory": {"judgment": "blocked"},
                },
                {
                    "action": "updata",
                    "source_memory_ids": ["pub"],
                    "memory": {"judgment": "allowed"},
                },
            ],
        )

        self.assertEqual(useful, ["pub"])
        self.assertEqual(recalled, ["pub"])
        self.assertEqual(rejected, 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["source_memory_ids"], ["pub"])

    def test_unknown_or_quarantine_current_scope_is_rejected(self):
        for scope in ("", guard.QUARANTINE_SCOPE):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    guard.filter_vector_feedback_payload(
                        self.collection,
                        scope,
                        [],
                        [],
                        [],
                    )


if __name__ == "__main__":
    unittest.main()
