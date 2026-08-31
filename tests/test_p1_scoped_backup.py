from __future__ import annotations

import importlib.util
import logging
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # p1_scoped_backup imports security_guard relatively, so test the file in
    # its real package namespace without importing the heavy package __init__.
    if name.endswith("p1_scoped_backup"):
        import sys
        import types

        package = "astrbot_plugin_angel_memory"
        core_package = f"{package}.core"
        for module_name, module_path in (
            (package, ROOT),
            (core_package, ROOT / "core"),
        ):
            if module_name not in sys.modules:
                stub = types.ModuleType(module_name)
                stub.__path__ = [str(module_path)]
                sys.modules[module_name] = stub
    spec.loader.exec_module(module)
    return module


backup = _load_module(
    "astrbot_plugin_angel_memory.core.p1_scoped_backup",
    "core/p1_scoped_backup.py",
)


class _Manager:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.logger = logging.getLogger("p1-scoped-backup-test")
        self.fts_upserts = []
        self.fts_deletes = []
        self.conn.executescript(
            """
            CREATE TABLE memory_records (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                judgment TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                strength INTEGER NOT NULL,
                is_active INTEGER NOT NULL,
                useful_count INTEGER NOT NULL,
                useful_score REAL NOT NULL,
                last_recalled_at REAL NOT NULL,
                memory_scope TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE memory_tag_rel (
                memory_id TEXT NOT NULL,
                tag_id INTEGER NOT NULL
            );
            """
        )

    def _connect(self):
        return self.conn

    @staticmethod
    def _normalize_tags(tags):
        return list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))

    def _replace_memory_tags(self, conn, memory_id, tags):
        del conn, memory_id, tags

    def _sync_memory_fts_batch_sync(self, upsert_ids=None, delete_ids=None):
        self.fts_upserts.extend(upsert_ids or [])
        self.fts_deletes.extend(delete_ids or [])


def _parse_tags(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return list(value or [])


class ScopedBackupTests(unittest.TestCase):
    def test_input_dedupe_uses_scope_and_judgment(self):
        rows = backup.dedupe_backup_candidates(
            [
                {"memory_scope": "public", "judgment": "同一句", "created_at": 1},
                {"memory_scope": "xiaotai", "judgment": "同一句", "created_at": 2},
                {"memory_scope": "public", "judgment": "同一句", "created_at": 3},
            ]
        )
        self.assertEqual(set(rows), {("public", "同一句"), ("xiaotai", "同一句")})
        self.assertEqual(rows[("public", "同一句")]["created_at"], 3.0)

    def test_missing_scope_is_quarantined(self):
        rows = backup.dedupe_backup_candidates(
            [{"judgment": "未知来源", "created_at": 1}]
        )
        self.assertIn((backup.QUARANTINE_SCOPE, "未知来源"), rows)

    def test_existing_same_text_in_other_scope_is_never_overwritten(self):
        manager = _Manager()
        manager.conn.executemany(
            """
            INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("pub-id", "知识记忆", "同一句", "public-old", 1, 0, 0, 0, 0, "public", 10, 10),
                ("private-id", "知识记忆", "同一句", "private-old", 1, 0, 0, 0, 0, "xiaotai", 20, 20),
            ],
        )
        stats = backup.scoped_backup_upsert_sync(
            manager,
            [
                {
                    "memory_scope": "public",
                    "judgment": "同一句",
                    "reasoning": "public-new",
                    "created_at": 30,
                }
            ],
            _parse_tags,
        )

        rows = manager.conn.execute(
            "SELECT id, memory_scope, reasoning FROM memory_records ORDER BY id"
        ).fetchall()
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(
            [(row["id"], row["memory_scope"], row["reasoning"]) for row in rows],
            [
                ("private-id", "xiaotai", "private-old"),
                ("pub-id", "public", "public-new"),
            ],
        )

    def test_identity_migration_dedupes_only_inside_scope_and_adds_unique_index(self):
        manager = _Manager()
        manager.conn.executemany(
            "INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("pub-new", "知识记忆", "相同", "new", 1, 0, 0, 0, 0, "public", 20, 20),
                ("pub-old", "知识记忆", "相同", "old", 1, 0, 0, 0, 0, "public", 10, 10),
                ("private", "知识记忆", "相同", "private", 1, 0, 0, 0, 0, "xiaotai", 5, 5),
                ("blank", "知识记忆", "未知", "blank", 1, 0, 0, 0, 0, "", 1, 1),
            ],
        )
        stats = backup.migrate_scope_judgment_identity(manager)
        rows = manager.conn.execute(
            "SELECT id, memory_scope FROM memory_records ORDER BY id"
        ).fetchall()
        self.assertEqual(stats, {"quarantined": 1, "deduped": 1})
        self.assertEqual(
            [(row["id"], row["memory_scope"]) for row in rows],
            [
                ("blank", backup.QUARANTINE_SCOPE),
                ("private", "xiaotai"),
                ("pub-new", "public"),
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            manager.conn.execute(
                "INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("dup", "知识记忆", "相同", "dup", 1, 0, 0, 0, 0, "public", 30, 30),
            )

    def test_duplicate_cleanup_is_confined_to_one_scope(self):
        manager = _Manager()
        manager.conn.executemany(
            "INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("pub-new", "知识记忆", "重复", "new", 1, 0, 0, 0, 0, "public", 20, 20),
                ("pub-old", "知识记忆", "重复", "old", 1, 0, 0, 0, 0, "public", 10, 10),
                ("private", "知识记忆", "重复", "private", 1, 0, 0, 0, 0, "xiaotai", 5, 5),
            ],
        )
        backup.scoped_backup_upsert_sync(
            manager,
            [{"memory_scope": "public", "judgment": "重复", "created_at": 15}],
            _parse_tags,
        )
        rows = manager.conn.execute(
            "SELECT id, memory_scope FROM memory_records ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["memory_scope"]) for row in rows],
            [("private", "xiaotai"), ("pub-new", "public")],
        )
        self.assertEqual(manager.fts_deletes, ["pub-old"])


if __name__ == "__main__":
    unittest.main()
