from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "p1_scoped_notes.py"
if not MODULE_PATH.exists():
    # Local pre-upload execution.
    MODULE_PATH = Path("/mnt/data/p1_scoped_notes.py")

spec = importlib.util.spec_from_file_location(
    "angel_memory_p1_scoped_notes",
    MODULE_PATH,
)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def one_chunk(text: str, index: int = 1):
    lines = text.splitlines() or [text]
    return [
        {
            "chunk_index": index,
            "line_start": 1,
            "line_end": len(lines),
            "content": text,
        }
    ]


def scoped_path(scope: str, filename: str) -> str:
    return f"{m.scoped_relative_directory(scope)}/{filename}"


class ScopedNoteRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = m.ScopedNoteRepository(Path(self.tmp.name) / "notes.sqlite3")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def add(
        self,
        scope: str,
        file_id: str,
        filename: str,
        text: str,
        *,
        updated_at: int = 1,
    ):
        path = (
            filename
            if scope == m.QUARANTINE_SCOPE
            else scoped_path(scope, filename)
        )
        return self.repo.upsert_file(
            memory_scope=scope,
            file_id=file_id,
            source_file_path=path,
            heading_h1="title",
            total_lines=len(text.splitlines()),
            updated_at=updated_at,
            content=text,
            chunks=one_chunk(text),
        )

    def test_scope_roundtrip_and_path_inference(self):
        for scope in ("public", "xiaotai", "小太-私密"):
            segment = m.scope_to_segment(scope)
            self.assertEqual(m.segment_to_scope(segment), scope)
            path = f".angel/scoped/{segment}/note/example.md"
            self.assertEqual(m.infer_scope_from_relative_path(path), scope)
            self.assertTrue(m.note_path_matches_scope(path, scope))

        segment = m.scope_to_segment("xiaotai")
        self.assertIsNone(
            m.infer_scope_from_relative_path(
                f".angel/scoped/{segment}/other/example.md"
            )
        )
        self.assertIsNone(
            m.infer_scope_from_relative_path(f".angel/scoped/{segment}/note")
        )

    def test_scope_read_inheritance_is_one_way(self):
        self.assertEqual(m.readable_scopes("public"), ("public",))
        self.assertEqual(m.readable_scopes("xiaotai"), ("xiaotai", "public"))
        self.assertTrue(m.scope_is_readable("public", "xiaotai"))
        self.assertFalse(m.scope_is_readable("xiaotai", "public"))
        self.assertFalse(m.scope_is_readable("other", "xiaotai"))
        self.assertTrue(m.scope_is_owned("xiaotai", "xiaotai"))
        self.assertFalse(m.scope_is_owned("public", "xiaotai"))

    def test_search_filters_before_returning_candidates(self):
        self.add("public", "1", "public.md", "共同知识 猫喜欢阳光")
        self.add("xiaotai", "2", "private.md", "私密癖好 蓝色围巾")
        self.add("other", "3", "other.md", "他人秘密 蓝色围巾")

        public_hits = self.repo.search(
            query="蓝色围巾",
            current_scope="public",
            limit=20,
        )
        self.assertEqual(public_hits, [])

        private_hits = self.repo.search(
            query="蓝色围巾",
            current_scope="xiaotai",
            limit=20,
        )
        self.assertEqual(
            {item["memory_scope"] for item in private_hits},
            {"xiaotai"},
        )
        self.assertNotIn("他人秘密", repr(private_hits))

        shared_hits = self.repo.search(
            query="共同知识",
            current_scope="xiaotai",
            limit=20,
        )
        self.assertEqual(
            {item["memory_scope"] for item in shared_hits},
            {"public"},
        )

    def test_short_id_read_authorization(self):
        public = self.add("public", "1", "public.md", "shared")
        private = self.add("xiaotai", "2", "private.md", "private")
        other = self.add("other", "3", "other.md", "other")

        self.assertIsNotNone(
            self.repo.get_note_for_read(public["note_short_id"], "public")
        )
        self.assertIsNone(
            self.repo.get_note_for_read(private["note_short_id"], "public")
        )
        self.assertIsNotNone(
            self.repo.get_note_for_read(public["note_short_id"], "xiaotai")
        )
        self.assertIsNotNone(
            self.repo.get_note_for_read(private["note_short_id"], "xiaotai")
        )
        self.assertIsNone(
            self.repo.get_note_for_read(other["note_short_id"], "xiaotai")
        )
        self.assertIsNone(
            self.repo.get_note_owned(public["note_short_id"], "xiaotai")
        )

    def test_scope_encoded_paths_are_distinct(self):
        public = self.add("public", "1", "same.md", "public body")
        private = self.add("xiaotai", "2", "same.md", "private body")
        self.assertNotEqual(
            public["note_short_id"],
            private["note_short_id"],
        )
        self.assertEqual(self.repo.stats()["active_files"], 2)

    def test_quarantine_never_stores_chunks_or_becomes_readable(self):
        quarantined = self.add(
            m.QUARANTINE_SCOPE,
            "1",
            "legacy.md",
            "legacy secret body",
        )
        self.assertEqual(
            self.repo.search(
                query="legacy secret body",
                current_scope="public",
                limit=20,
            ),
            [],
        )
        self.assertIsNone(
            self.repo.get_note_for_read(
                quarantined["note_short_id"],
                "xiaotai",
            )
        )
        stats = self.repo.stats()
        self.assertEqual(stats["quarantined_files"], 1)
        self.assertEqual(stats["chunks"], 0)
        row = self.repo._get_conn().execute(
            "SELECT heading_h1 FROM scoped_note_files WHERE note_short_id = ?",
            (quarantined["note_short_id"],),
        ).fetchone()
        self.assertEqual(row["heading_h1"], "")

    def test_missing_then_reindex_preserves_short_id_after_file_id_reset(self):
        first = self.add("public", "7", "stable.md", "first content")
        path = scoped_path("public", "stable.md")
        self.assertEqual(self.repo.mark_missing_by_path(path), 1)
        self.assertIsNone(
            self.repo.get_note_for_read(first["note_short_id"], "public")
        )
        second = self.add("public", "800", "stable.md", "second content")
        self.assertEqual(first["note_short_id"], second["note_short_id"])
        hits = self.repo.search(
            query="second content",
            current_scope="public",
            limit=20,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["file_id"], "800")

    def test_path_traversal_drive_prefix_and_scope_spoofing_rejected(self):
        for path in (
            "../secret.md",
            "/tmp/secret.md",
            "a/../../secret.md",
            "C:/Windows/secret.md",
        ):
            with self.assertRaises(m.NotePathError):
                m.normalize_relative_path(path)
        with self.assertRaises(m.NoteScopeError):
            m.normalize_scope("private/other")
        self.assertIsNone(m.infer_scope_from_relative_path("ordinary.md"))

    def test_storage_layer_rejects_path_scope_mismatch(self):
        with self.assertRaises(m.NotePathError):
            self.repo.upsert_file(
                memory_scope="public",
                file_id="1",
                source_file_path=scoped_path("xiaotai", "spoof.md"),
                heading_h1="",
                total_lines=1,
                updated_at=1,
                content="secret",
                chunks=one_chunk("secret"),
            )
        with self.assertRaises(m.NotePathError):
            self.repo.upsert_file(
                memory_scope="public",
                file_id="2",
                source_file_path="legacy.md",
                heading_h1="",
                total_lines=1,
                updated_at=1,
                content="secret",
                chunks=one_chunk("secret"),
            )

    def test_chunk_ownership_trigger_rejects_cross_scope_insert(self):
        item = self.add("public", "1", "owned.md", "owned")
        conn = self.repo._get_conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO scoped_note_chunks(
                    note_short_id, file_id, memory_scope,
                    source_file_path, chunk_index, line_start,
                    line_end, content, search_text, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["note_short_id"],
                    "1",
                    "xiaotai",
                    scoped_path("xiaotai", "owned.md"),
                    999,
                    1,
                    1,
                    "cross scope",
                    "cross scope",
                    time.time(),
                ),
            )
        conn.rollback()

    def test_parent_row_tampering_is_not_returned(self):
        item = self.add("public", "1", "tamper.md", "tamper sentinel")
        conn = self.repo._get_conn()
        conn.execute(
            "UPDATE scoped_note_files SET file_id = 'different' WHERE note_short_id = ?",
            (item["note_short_id"],),
        )
        conn.commit()
        self.assertEqual(
            self.repo.search(
                query="tamper sentinel",
                current_scope="public",
                limit=20,
            ),
            [],
        )

    def test_fallback_scan_is_not_limited_to_first_hundred_candidates(self):
        self.repo._fts_available = False
        self.add(
            "public",
            "match",
            "old-match.md",
            "needle beyond candidate window",
            updated_at=0,
        )
        for index in range(130):
            self.add(
                "public",
                f"n-{index}",
                f"recent-{index}.md",
                f"unrelated body {index}",
                updated_at=1000 + index,
            )
        hits = self.repo.search(
            query="needle beyond candidate window",
            current_scope="public",
            limit=1,
        )
        self.assertEqual(len(hits), 1)
        self.assertIn("needle", hits[0]["content"])

    def test_unknown_schema_version_fails_closed(self):
        self.repo.close()
        db_path = Path(self.tmp.name) / "future.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE scoped_note_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO scoped_note_meta(key, value) VALUES('schema_version', '999')"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RuntimeError):
            m.ScopedNoteRepository(db_path)

    def test_repository_can_reopen_after_connections_are_closed(self):
        self.add("public", "1", "threaded.md", "threaded sentinel")
        with ThreadPoolExecutor(max_workers=1) as executor:
            hits = executor.submit(
                self.repo.search,
                query="threaded sentinel",
                current_scope="public",
                limit=5,
            ).result()
        self.assertEqual(len(hits), 1)
        self.repo.close()
        self.assertEqual(self.repo.stats()["active_files"], 1)

    def test_missing_scope_fails_closed(self):
        token = m._CURRENT_NOTE_SCOPE.set(None)
        try:
            with self.assertRaises(m.NoteScopeError):
                m.resolve_note_scope()
        finally:
            m._CURRENT_NOTE_SCOPE.reset(token)


class ScopedNoteHelperTests(unittest.TestCase):
    def test_physical_path_resolver_rejects_mismatch_and_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            inside = raw / "inside.md"
            inside.write_text("inside", encoding="utf-8")
            outside = Path(tmp) / "outside.md"
            outside.write_text("outside", encoding="utf-8")

            class PathManager:
                def get_raw_dir(self):
                    return raw

            class PluginContext:
                def get_path_manager(self):
                    return PathManager()

            target, relative = m._resolve_file_under_raw(
                PluginContext(),
                inside,
                "inside.md",
            )
            self.assertEqual(target, inside.resolve())
            self.assertEqual(relative, "inside.md")
            with self.assertRaises(m.NotePathError):
                m._resolve_file_under_raw(
                    PluginContext(),
                    inside,
                    "different.md",
                )
            with self.assertRaises(m.NotePathError):
                m._resolve_file_under_raw(PluginContext(), outside)

    def test_note_read_pagination_and_character_cap(self):
        row = {
            "source_file_path": scoped_path("public", "read.md"),
            "memory_scope": "public",
        }
        result = m._format_note_read_result(
            note_short_id=9,
            row=row,
            text="line1\nline2\nline3",
            offset=2,
            limit=1,
            max_chars=100,
        )
        self.assertIn("returned: L2-2", result)
        self.assertIn("has_more: true", result)
        self.assertTrue(result.endswith("line2"))
        self.assertNotIn("line3", result)

    def test_restore_note_config_supports_mapping_and_object(self):
        class DeepMind:
            pass

        mapping = DeepMind()
        mapping.config = {
            "note_assistant": {"enable_recall": False, "top_k": "4"}
        }
        m._restore_note_config(mapping)
        self.assertFalse(mapping.note_recall_enabled)
        self.assertEqual(mapping.note_candidate_top_k, 28)

        class Config:
            note_assistant = {"enable_recall": True, "top_k": 2}

        obj = DeepMind()
        obj.config = Config()
        m._restore_note_config(obj)
        self.assertTrue(obj.note_recall_enabled)
        self.assertEqual(obj.note_inject_top_k, 2)


class ScopedNoteContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_scope_is_task_local(self):
        async def worker(scope: str):
            token = m._CURRENT_NOTE_SCOPE.set(scope)
            try:
                await asyncio.sleep(0)
                return m.resolve_note_scope()
            finally:
                m._CURRENT_NOTE_SCOPE.reset(token)

        first, second = await asyncio.gather(
            worker("xiaotai"),
            worker("other"),
        )
        self.assertEqual(first, "xiaotai")
        self.assertEqual(second, "other")
        with self.assertRaises(m.NoteScopeError):
            m.resolve_note_scope()


if __name__ == "__main__":
    unittest.main()
