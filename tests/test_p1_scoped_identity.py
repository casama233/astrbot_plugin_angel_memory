from __future__ import annotations

import importlib.util
import logging
import sqlite3
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = _load_module(
    "angel_memory_p1_scoped_identity",
    "core/p1_scoped_identity.py",
)


class _Manager:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.logger = logging.getLogger("p1-scoped-identity-test")
        self.conn.execute(
            """
            CREATE TABLE memory_records (
                id TEXT PRIMARY KEY,
                judgment TEXT NOT NULL,
                memory_scope TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX uq_scope_judgment "
            "ON memory_records(memory_scope, judgment)"
        )

    def _connect(self):
        return self.conn

    @staticmethod
    def _normalize_scope(value):
        scope = str(value or "").strip()
        if not scope:
            raise ValueError("empty scope")
        return scope

    def _get_memories_by_ids_sync(self, memory_ids):
        if not memory_ids:
            return []
        row = self.conn.execute(
            "SELECT id, judgment, memory_scope, created_at "
            "FROM memory_records WHERE id = ?",
            (memory_ids[0],),
        ).fetchone()
        if row is None:
            return []
        return [
            types.SimpleNamespace(
                id=str(row["id"]),
                judgment=str(row["judgment"]),
                memory_scope=str(row["memory_scope"]),
                created_at=float(row["created_at"]),
            )
        ]

    def insert(self, memory_id, scope, judgment, created_at=1.0):
        self.conn.execute(
            "INSERT INTO memory_records(id, judgment, memory_scope, created_at) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, judgment, scope, created_at),
        )
        self.conn.commit()


class ScopedIdentityTests(unittest.TestCase):
    def test_create_is_idempotent_inside_exact_scope(self):
        manager = _Manager()
        manager.insert("existing", "public", "同一句")
        calls = []

        def original(*args):
            calls.append(args)
            raise AssertionError("duplicate create must not call original writer")

        result = identity.idempotent_remember_sync(
            manager,
            original,
            "knowledge",
            "同一句",
            "new reasoning",
            ["tag"],
            False,
            10,
            "public",
        )
        self.assertEqual(result.id, "existing")
        self.assertEqual(calls, [])

    def test_same_judgment_in_other_scope_remains_independent(self):
        manager = _Manager()
        manager.insert("public-id", "public", "同一句")
        calls = []

        def original(
            mgr,
            memory_type,
            judgment,
            reasoning,
            tags,
            is_active,
            strength,
            memory_scope,
        ):
            del memory_type, reasoning, tags, is_active, strength
            calls.append(memory_scope)
            mgr.insert("private-id", memory_scope, judgment, 2.0)
            return mgr._get_memories_by_ids_sync(["private-id"])[0]

        result = identity.idempotent_remember_sync(
            manager,
            original,
            "knowledge",
            "同一句",
            "private",
            ["tag"],
            False,
            10,
            "xiaotai",
        )
        self.assertEqual(result.id, "private-id")
        self.assertEqual(calls, ["xiaotai"])
        scopes = manager.conn.execute(
            "SELECT memory_scope FROM memory_records ORDER BY memory_scope"
        ).fetchall()
        self.assertEqual([row[0] for row in scopes], ["public", "xiaotai"])

    def test_concurrent_unique_race_converges_to_canonical_row(self):
        manager = _Manager()

        def racing_original(
            mgr,
            memory_type,
            judgment,
            reasoning,
            tags,
            is_active,
            strength,
            memory_scope,
        ):
            del memory_type, reasoning, tags, is_active, strength
            mgr.insert("winner", memory_scope, judgment, 3.0)
            raise sqlite3.IntegrityError("simulated losing writer")

        result = identity.idempotent_remember_sync(
            manager,
            racing_original,
            "knowledge",
            "并发记忆",
            "reasoning",
            ["tag"],
            False,
            10,
            "xiaotai",
        )
        self.assertEqual(result.id, "winner")

    def test_mirror_collision_keeps_existing_canonical_id(self):
        manager = _Manager()
        manager.insert("canonical", "xiaotai", "同一句")
        calls = []
        incoming = types.SimpleNamespace(
            id="foreign-id",
            judgment="同一句",
            memory_scope="xiaotai",
        )

        def original(*args):
            calls.append(args)
            raise AssertionError("identity collision must not write another ID")

        result = identity.idempotent_upsert_memory_sync(
            manager,
            original,
            incoming,
        )
        self.assertEqual(result.id, "canonical")
        self.assertEqual(calls, [])

    def test_normal_writes_cannot_target_quarantine(self):
        manager = _Manager()
        with self.assertRaises(ValueError):
            identity.idempotent_remember_sync(
                manager,
                lambda *args: None,
                "knowledge",
                "x",
                "",
                [],
                False,
                1,
                identity.QUARANTINE_SCOPE,
            )


if __name__ == "__main__":
    unittest.main()
