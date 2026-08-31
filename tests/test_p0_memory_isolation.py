from __future__ import annotations

import importlib.util
import re
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_module("angel_memory_security_guard", "core/security_guard.py")
migration_module = _load_module(
    "angel_memory_scope_migration",
    "core/migrations/memory_scope_migration.py",
)


class _SqlManager:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE memory_records (id TEXT PRIMARY KEY, memory_scope TEXT NOT NULL)"
        )
        self.conn.executemany(
            "INSERT INTO memory_records (id, memory_scope) VALUES (?, ?)",
            [
                ("pub", "public"),
                ("xiaotai", "xiaotai"),
                ("other", "other-private"),
                ("quarantine", guard.QUARANTINE_SCOPE),
            ],
        )

    def _connect(self):
        return self.conn


class _Logger:
    def __init__(self):
        self.messages = []

    def _record(self, level, *args):
        self.messages.append((level, args))

    def info(self, *args, **kwargs):
        self._record("info", *args)

    def warning(self, *args, **kwargs):
        self._record("warning", *args)

    def error(self, *args, **kwargs):
        self._record("error", *args)


class _Collection:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def get(self, *, limit, offset, include):
        del include
        batch = self.rows[offset : offset + limit]
        return {
            "ids": [row[0] for row in batch],
            "metadatas": [row[1] for row in batch],
        }

    def update(self, *, ids, metadatas):
        self.updates.append((list(ids), list(metadatas)))


class MemoryIsolationGuardTests(unittest.TestCase):
    def test_scope_resolution_is_fail_closed_and_persona_first(self):
        pattern = re.compile(r"^[a-zA-Z0-9\u4e00-\u9fff_-]+$")
        scope_map = {
            "小太": "xiaotai",
            "private:friend:42": "conversation-42",
        }

        self.assertEqual(
            guard.resolve_scope_from_mapping(
                "private:friend:42",
                "小太",
                scope_map,
                scope_pattern=pattern,
            ),
            ("xiaotai", "persona", "小太"),
        )
        self.assertEqual(
            guard.resolve_scope_from_mapping(
                "private:friend:42",
                "",
                scope_map,
                scope_pattern=pattern,
            ),
            ("conversation-42", "conversation", "private:friend:42"),
        )
        with self.assertRaises(guard.MemoryScopeResolutionError):
            guard.resolve_scope_from_mapping(
                "unmapped",
                "unknown-persona",
                scope_map,
                scope_pattern=pattern,
            )

    def test_explicit_default_is_allowed_but_never_implicit(self):
        self.assertEqual(
            guard.resolve_scope_from_mapping(
                "unmapped",
                "",
                {},
                default_scope="public",
            ),
            ("public", "explicit_default", "public"),
        )
        self.assertEqual(
            guard.resolve_scope_from_mapping(
                "unmapped",
                "",
                {"__default__": "public"},
            ),
            ("public", "explicit_default", "public"),
        )
        with self.assertRaises(guard.MemoryScopeResolutionError):
            guard.resolve_scope_from_mapping("unmapped", "", {}, default_scope="")

    def test_quarantine_scope_is_reserved(self):
        with self.assertRaises(guard.MemoryScopeResolutionError):
            guard.resolve_scope_from_mapping(
                "conversation",
                "小太",
                {"小太": guard.QUARANTINE_SCOPE},
            )

    def test_read_inheritance_is_one_way_and_mutation_is_exact(self):
        self.assertTrue(guard.scope_is_readable("public", "xiaotai"))
        self.assertFalse(guard.scope_is_readable("xiaotai", "public"))
        self.assertFalse(guard.scope_is_readable("other-private", "xiaotai"))
        self.assertFalse(guard.scope_is_owned("public", "xiaotai"))
        self.assertTrue(guard.scope_is_owned("xiaotai", "xiaotai"))

    def test_rerank_candidates_are_filtered_before_document_lookup(self):
        manager = _SqlManager()
        hits = [
            {"id": "pub", "score": 1.0},
            {"id": "xiaotai", "score": 0.9},
            {"id": "other", "score": 0.8},
            {"id": "quarantine", "score": 0.7},
        ]

        public_hits = guard._filter_hits_by_scope(manager, hits, "public")
        self.assertEqual([item["id"] for item in public_hits], ["pub"])

        private_hits = guard._filter_hits_by_scope(manager, hits, "xiaotai")
        self.assertEqual(
            [item["id"] for item in private_hits],
            ["pub", "xiaotai"],
        )

    def test_mutation_filter_never_grants_inherited_public_memory(self):
        manager = _SqlManager()
        self.assertEqual(
            guard._owned_ids(manager, ["pub", "xiaotai"], "xiaotai"),
            ["xiaotai"],
        )
        self.assertEqual(
            guard._owned_ids(manager, ["pub", "xiaotai"], "public"),
            ["pub"],
        )


class MemoryScopeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_scope_migration_quarantines_missing_metadata(self):
        logger = _Logger()
        collection = _Collection(
            [
                ("missing", {}),
                ("empty", {"memory_scope": ""}),
                ("public", {"memory_scope": "public"}),
                ("private", {"memory_scope": "xiaotai"}),
            ]
        )
        migration = migration_module.MemoryScopeMigration(logger)

        await migration.migrate_missing_memory_scope(
            collection,
            batch_size=2,
            sleep_seconds=0,
        )

        patched = {
            memory_id: metadata["memory_scope"]
            for ids, metadatas in collection.updates
            for memory_id, metadata in zip(ids, metadatas)
        }
        self.assertEqual(
            patched,
            {
                "missing": migration_module.QUARANTINE_SCOPE,
                "empty": migration_module.QUARANTINE_SCOPE,
            },
        )


if __name__ == "__main__":
    unittest.main()
