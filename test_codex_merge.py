import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_merge


def write_rollout(path: Path, thread_id: str, base: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": thread_id, "history_mode": "paginated"}
    if base:
        payload["history_base"] = base
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n",
        encoding="utf-8",
    )


class CodexMergeTests(unittest.TestCase):
    def test_help_contains_examples_and_explains_fork(self) -> None:
        help_text = codex_merge.build_parser().format_help()
        self.assertIn("codex-merge primary secondary 01a0ad83-f4e --resume", help_text)
        self.assertNotIn("  list", help_text)
        self.assertNotIn("completion", help_text)
        self.assertIn("一次性快照", help_text)

        output = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(output):
            codex_merge.main(["fork", "--help"])
        fork_help = output.getvalue()
        self.assertIn("源 Session 不会被修改", fork_help)
        self.assertIn("唯一的 Session ID 前缀", fork_help)

    def test_resolve_home_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "tester"
            for dirname in (".codex", ".codex_share_primary", ".codex_secondary"):
                home = root / dirname
                home.mkdir(parents=True)
                (home / "config.toml").touch()
            homes = codex_merge.discover_homes(root, "")
            with mock.patch.object(codex_merge, "discover_homes", return_value=homes):
                self.assertEqual(codex_merge.resolve_home("t"), root / ".codex")
                self.assertEqual(codex_merge.resolve_home("p"), root / ".codex_share_primary")
                self.assertEqual(codex_merge.resolve_home("s"), root / ".codex_secondary")
            self.assertEqual(
                codex_merge.launcher_for_home(root / ".codex_secondary"),
                f"CODEX_HOME={root / '.codex_secondary'} codex",
            )

    def test_direct_migration_accepts_full_names_and_short_names(self) -> None:
        session_id = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "primary"
            target = root / "secondary"
            homes = {"primary": source, "p": source, "secondary": target, "s": target}
            with mock.patch.object(codex_merge, "discover_homes", return_value=homes), \
                 mock.patch.object(codex_merge, "share_session", return_value=("", None, 0)) as share:
                for source_name, target_name in (("primary", "secondary"), ("p", "s")):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(
                            codex_merge.main([source_name, target_name, session_id, "--dry-run"]), 0
                        )
                    self.assertEqual(share.call_args.args[:3], (source, target, session_id))
                    self.assertTrue(share.call_args.kwargs["dry_run"])

    def test_discovers_homes_and_current_without_reading_auth(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "operator"
            root.mkdir()
            default = root / ".codex"
            default.mkdir()
            (default / "auth.json").write_text("not valid JSON")
            other = root / ".codex_team"
            other.mkdir()
            (other / "sessions").mkdir()
            hyphenated = root / ".codex-work"
            hyphenated.mkdir()
            (hyphenated / "config.toml").touch()
            unrelated = root / ".codex_unrelated"
            unrelated.mkdir()
            external = root / "other home"
            external.mkdir()
            (external / "config.toml").touch()
            homes = codex_merge.discover_homes(root, str(external))
            self.assertEqual(homes["default"], default)
            self.assertEqual(homes["team"], other)
            self.assertEqual(homes["t"], other)
            self.assertEqual(homes["work"], hyphenated)
            self.assertEqual(homes["current"], external)
            self.assertNotIn("unrelated", homes)
            self.assertEqual(codex_merge.launcher_for_home(external),
                             f"CODEX_HOME='{external}' codex")

    def test_app_server_handles_batched_messages(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fake = root / "fake-codex"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    request = json.loads(line)\n"
                "    if 'id' not in request: continue\n"
                "    if request['method'] == 'initialize':\n"
                "        sys.stdout.write(json.dumps({'method': 'notice'}) + '\\n')\n"
                "    sys.stdout.write(json.dumps({'id': request['id'], 'result': {}}) + '\\n')\n"
                "    sys.stdout.flush()\n"
            )
            fake.chmod(0o755)
            with codex_merge.AppServer(root, str(fake), timeout=2) as server:
                self.assertEqual(server.call("thread/list", {"limit": 1}), {})

    def test_resolve_lineage(self) -> None:
        parent = "11111111-1111-4111-8111-111111111111"
        child = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            parent_path = home / "sessions/2026/01/01" / f"rollout-x-{parent}.jsonl"
            child_path = home / "sessions/2026/01/02" / f"rollout-x-{child}.jsonl"
            write_rollout(parent_path, parent)
            write_rollout(
                child_path,
                child,
                {"thread_id": parent, "end_byte_offset": parent_path.stat().st_size},
            )
            lineage = codex_merge.resolve_lineage(home, child)
            self.assertEqual(lineage.thread_ids, (child, parent))
            self.assertEqual(
                lineage.required_prefix_bytes[parent_path.resolve()], parent_path.stat().st_size
            )

    def test_rejects_invalid_history_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "rollout-test.jsonl"
            write_rollout(path, "11111111-1111-4111-8111-111111111111",
                          {"thread_id": "22222222-2222-4222-8222-222222222222",
                           "end_byte_offset": -1})
            with self.assertRaisesRegex(codex_merge.ShareError, "history boundary"):
                codex_merge.parse_rollout(path)

    def test_merge_history_rows(self) -> None:
        schema = """
        CREATE TABLE thread_turns (
          thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL,
          status TEXT NOT NULL, error_json TEXT, started_at INTEGER, completed_at INTEGER,
          duration_ms INTEGER, first_user_item_id TEXT, final_agent_item_id TEXT,
          rollout_byte_offset INTEGER, rollout_end_ordinal INTEGER,
          rollout_end_byte_offset INTEGER, PRIMARY KEY(thread_id, turn_id));
        CREATE TABLE thread_items (
          thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
          rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
          item_json TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '',
          updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(thread_id, turn_id, item_id));
        CREATE TABLE thread_history_projection_state (
          thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER NOT NULL,
          next_rollout_ordinal INTEGER NOT NULL);
        CREATE TABLE thread_realtime_items (
          thread_id TEXT NOT NULL, item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL,
          created_at_ms INTEGER NOT NULL, item_type TEXT NOT NULL, item_json TEXT NOT NULL,
          PRIMARY KEY(thread_id, item_id));
        """
        thread_id = "33333333-3333-4333-8333-333333333333"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source?#.sqlite"
            target = root / "target?#.sqlite"
            for database in (source, target):
                connection = sqlite3.connect(database)
                connection.executescript(schema)
                connection.close()
            connection = sqlite3.connect(source)
            connection.execute(
                "INSERT INTO thread_history_projection_state VALUES (?, 10, 2)",
                (thread_id,),
            )
            connection.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status) VALUES (?, 'turn', 1, 'completed')",
                (thread_id,),
            )
            connection.commit()
            connection.close()
            counts = codex_merge.merge_history_rows(source, target, [thread_id])
            self.assertEqual(counts["thread_turns"], 1)
            connection = sqlite3.connect(target)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM thread_turns WHERE thread_id=?", (thread_id,)
                ).fetchone()[0],
                1,
            )
            connection.close()

    def test_clone_history_schema_copies_no_session_rows(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.sqlite"
            target = root / "target.sqlite"
            connection = sqlite3.connect(source)
            connection.executescript(
                """
                CREATE TABLE _sqlx_migrations (
                  version INTEGER PRIMARY KEY, description TEXT NOT NULL);
                CREATE TABLE thread_turns (
                  thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                  PRIMARY KEY(thread_id, turn_id));
                CREATE TABLE thread_items (
                  thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                  item_id TEXT NOT NULL, PRIMARY KEY(thread_id, turn_id, item_id));
                CREATE INDEX idx_turns_thread ON thread_turns(thread_id);
                INSERT INTO _sqlx_migrations VALUES (1, 'initial');
                INSERT INTO thread_turns VALUES ('secret-thread', 'turn-1');
                """
            )
            connection.close()

            codex_merge.clone_history_schema(source, target)

            connection = sqlite3.connect(target)
            self.assertEqual(
                connection.execute("SELECT * FROM _sqlx_migrations").fetchall(),
                [(1, "initial")],
            )
            self.assertEqual(connection.execute("SELECT * FROM thread_turns").fetchall(), [])
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='index' AND name='idx_turns_thread'"
                ).fetchone()
            )
            connection.close()

    def test_rollout_conflict_detects_divergent_or_short_target(self) -> None:
        parent = "11111111-1111-4111-8111-111111111111"
        child = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source"
            target = root / "target"
            parent_path = source / "sessions/2026/01/01" / f"rollout-x-{parent}.jsonl"
            child_path = source / "sessions/2026/01/02" / f"rollout-x-{child}.jsonl"
            write_rollout(parent_path, parent)
            parent_path.write_text(parent_path.read_text() + '{"value":"source"}\n')
            write_rollout(
                child_path,
                child,
                {"thread_id": parent, "end_byte_offset": parent_path.stat().st_size},
            )
            lineage = codex_merge.resolve_lineage(source, child)

            target_parent = target / parent_path.relative_to(source)
            target_parent.parent.mkdir(parents=True)
            target_parent.write_bytes(parent_path.read_bytes()[:-1])
            self.assertTrue(codex_merge.rollout_conflicts(source, target, lineage))

            target_parent.write_bytes(parent_path.read_bytes())
            self.assertFalse(codex_merge.rollout_conflicts(source, target, lineage))

            target_parent.write_text(target_parent.read_text().replace("source", "target"))
            self.assertTrue(codex_merge.rollout_conflicts(source, target, lineage))

    def test_remap_lineage_preserves_boundaries_and_prompt_text(self) -> None:
        parent = "11111111-1111-4111-8111-111111111111"
        child = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            parent_path = home / "sessions/2026/01/01" / f"rollout-x-{parent}.jsonl"
            child_path = home / "sessions/2026/01/02" / f"rollout-x-{child}.jsonl"
            write_rollout(parent_path, parent)
            parent_path.write_text(
                parent_path.read_text()
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "thread_id": parent,
                            "message": f"keep embedded id={parent}",
                        },
                    }
                )
                + "\n"
            )
            boundary = parent_path.stat().st_size
            write_rollout(
                child_path,
                child,
                {"thread_id": parent, "end_byte_offset": boundary},
            )

            connection = sqlite3.connect(codex_merge.history_db(home))
            connection.executescript(
                """
                CREATE TABLE thread_history_projection_state (
                  thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER,
                  next_rollout_ordinal INTEGER);
                """
            )
            connection.executemany(
                "INSERT INTO thread_history_projection_state VALUES (?, 1, 1)",
                [(parent,), (child,)],
            )
            connection.commit()
            connection.close()

            state = home / "state_5.sqlite"
            connection = sqlite3.connect(state)
            connection.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT);
                CREATE TABLE thread_dynamic_tools (
                  thread_id TEXT, position INTEGER, PRIMARY KEY(thread_id, position));
                """
            )
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, 'cli')",
                [(parent, str(parent_path)), (child, str(child_path))],
            )
            connection.execute("INSERT INTO thread_dynamic_tools VALUES (?, 0)", (child,))
            connection.commit()
            connection.close()

            original_sizes = {parent: parent_path.stat().st_size, child: child_path.stat().st_size}
            lineage = codex_merge.resolve_lineage(home, child)
            remapped, id_map = codex_merge.remap_lineage(home, lineage)

            self.assertEqual(remapped.thread_ids, (id_map[child], id_map[parent]))
            self.assertEqual(
                remapped.required_prefix_bytes[remapped.rollouts[1].path], boundary
            )
            for rollout in remapped.rollouts:
                old_id = child if rollout.thread_id == id_map[child] else parent
                self.assertEqual(rollout.path.stat().st_size, original_sizes[old_id])
            parent_text = remapped.rollouts[1].path.read_text()
            self.assertIn(f'"thread_id": "{id_map[parent]}"', parent_text)
            self.assertIn(f"keep embedded id={parent}", parent_text)

            connection = sqlite3.connect(state)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM threads WHERE id IN (?, ?)", tuple(id_map.values())
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT thread_id FROM thread_dynamic_tools").fetchone()[0],
                id_map[child],
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
