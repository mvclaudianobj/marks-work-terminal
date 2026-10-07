import json
import os
import stat
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from work_orchestrator.errors import WorkError
from work_orchestrator.runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.project = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.project.name, self.state.name)
        self.agents = [self.runtime.register_agent(engine) for engine in ("markscode", "claude", "opencode", "codex")]

    def tearDown(self):
        self.project.cleanup()
        self.state.cleanup()

    def test_project_id_is_stable(self):
        self.assertEqual(self.runtime.project_id, Runtime(self.project.name, self.state.name).project_id)

    def test_project_id_is_uuid(self):
        uuid.UUID(self.runtime.project_id)

    def test_database_permissions(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.runtime.db_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.runtime.root).st_mode), 0o700)

    def test_wal(self):
        with self.runtime.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_four_agents_registered(self):
        self.assertEqual(len(self.runtime.agents()), 4)
        self.assertEqual({agent["engine"] for agent in self.runtime.agents()}, {"markscode", "claude", "opencode", "codex"})

    def test_agent_session_ids_are_unique(self):
        sessions = {agent["session_id"] for agent in self.runtime.agents()}
        self.assertEqual(len(sessions), 4)

    def test_agent_start_is_local_state_only(self):
        agent = self.runtime.agent_state(self.agents[0]["agent_id"], "started")
        self.assertEqual(agent["status"], "started")

    def test_agent_heartbeat(self):
        agent = self.runtime.heartbeat(self.agents[0]["agent_id"])
        self.assertIsNotNone(agent["last_heartbeat"])

    def test_invalid_engine(self):
        with self.assertRaises(WorkError):
            self.runtime.register_agent("unknown")

    def test_task_claim_release(self):
        task = self.runtime.create_task("task", "description")
        agent = self.agents[0]["agent_id"]
        self.runtime.claim_task(task, agent)
        with self.assertRaises(WorkError):
            self.runtime.claim_task(task, self.agents[1]["agent_id"])
        self.runtime.release_task(task, agent)

    def test_lease_fencing(self):
        first = self.runtime.acquire_lease("project", "p", self.agents[0]["agent_id"], ttl=10)
        with self.assertRaises(WorkError):
            self.runtime.acquire_lease("project", "p", self.agents[1]["agent_id"], ttl=10)
        self.runtime.release_lease("project", "p", self.agents[0]["agent_id"], first["fencing_token"])
        second = self.runtime.acquire_lease("project", "p", self.agents[1]["agent_id"], ttl=10)
        self.assertEqual(second["fencing_token"], 2)

    def test_expire_reclaim_reclaim_is_monotonic(self):
        agent = self.agents[0]["agent_id"]
        other = self.agents[1]["agent_id"]
        first = self.runtime.acquire_lease("project", "expired", agent, ttl=1)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE leases SET expires_at=? WHERE resource_type=? AND resource_id=?", ("2000-01-01T00:00:00Z", "project", "expired"))
        second = self.runtime.acquire_lease("project", "expired", other, ttl=1)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE leases SET expires_at=? WHERE resource_type=? AND resource_id=?", ("2000-01-01T00:00:00Z", "project", "expired"))
        third = self.runtime.acquire_lease("project", "expired", agent, ttl=1)
        self.assertEqual((first["fencing_token"], second["fencing_token"], third["fencing_token"]), (1, 2, 3))
        with self.runtime.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lease_history WHERE resource_type=? AND resource_id=?", ("project", "expired")).fetchone()[0], 3)

    def test_fencing_survives_restart(self):
        first = self.runtime.acquire_lease("project", "restart", self.agents[0]["agent_id"], ttl=1)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE leases SET expires_at=? WHERE resource_type=? AND resource_id=?", ("2000-01-01T00:00:00Z", "project", "restart"))
        restarted = Runtime(self.project.name, self.state.name)
        second = restarted.acquire_lease("project", "restart", self.agents[1]["agent_id"], ttl=1)
        self.assertEqual((first["fencing_token"], second["fencing_token"]), (1, 2))

    def test_rejected_old_fencing_token(self):
        first = self.runtime.acquire_lease("project", "reject", self.agents[0]["agent_id"], ttl=1)
        self.runtime.release_lease("project", "reject", self.agents[0]["agent_id"], first["fencing_token"])
        second = self.runtime.acquire_lease("project", "reject", self.agents[1]["agent_id"], ttl=1)
        with self.assertRaises(WorkError):
            self.runtime.heartbeat_lease("project", "reject", self.agents[0]["agent_id"], first["fencing_token"])
        with self.assertRaises(WorkError):
            self.runtime.release_lease("project", "reject", self.agents[1]["agent_id"], first["fencing_token"])
        self.assertEqual(second["fencing_token"], 2)

    def test_concurrent_reclaim_has_unique_tokens(self):
        first = self.runtime.acquire_lease("project", "concurrent", self.agents[0]["agent_id"], ttl=1)
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE leases SET expires_at=? WHERE resource_type=? AND resource_id=?", ("2000-01-01T00:00:00Z", "project", "concurrent"))
        results = []
        failures = []
        barrier = threading.Barrier(3)

        def reclaim(agent_id):
            try:
                barrier.wait()
                results.append(self.runtime.acquire_lease("project", "concurrent", agent_id, ttl=1)["fencing_token"])
            except (WorkError, threading.BrokenBarrierError) as exc:
                failures.append(exc)

        threads = [threading.Thread(target=reclaim, args=(agent["agent_id"],)) for agent in self.agents[1:3]]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [2])
        self.assertEqual(len(failures), 1)
        self.assertEqual(first["fencing_token"], 1)

    def test_invalid_or_overflow_fencing_fails_safe(self):
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO lease_fencing_counters VALUES(?,?,?)", ("project", "invalid", 9223372036854775807))
        with self.assertRaises(WorkError):
            self.runtime.acquire_lease("project", "invalid", self.agents[0]["agent_id"])
        with self.assertRaises(WorkError):
            self.runtime.heartbeat_lease("project", "missing", self.agents[0]["agent_id"], True)

    def test_lease_heartbeat_keeps_token(self):
        agent = self.agents[0]["agent_id"]
        lease = self.runtime.acquire_lease("agent", agent, agent)
        renewed = self.runtime.heartbeat_lease("agent", agent, agent, lease["fencing_token"])
        self.assertEqual(renewed["fencing_token"], lease["fencing_token"])

    def test_stale_recovery(self):
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO leases VALUES(?,?,?,?,?,?)", ("task", "old", self.agents[0]["agent_id"], "2000-01-01T00:00:00Z", 1, "2000-01-01T00:00:00Z"))
        self.assertEqual(self.runtime.recover_stale_leases(), 1)

    def test_claim_conflict(self):
        path = "src.py"
        self.runtime.acquire_claim("main", path, self.agents[0]["agent_id"])
        with self.assertRaises(WorkError):
            self.runtime.acquire_claim("main", path, self.agents[1]["agent_id"])

    def test_shared_readonly_claims(self):
        self.runtime.acquire_claim("main", "src.py", self.agents[0]["agent_id"], True)
        claim = self.runtime.acquire_claim("main", "src.py", self.agents[1]["agent_id"], True)
        self.assertTrue(claim)

    def test_claim_path_traversal(self):
        with self.assertRaises(WorkError):
            self.runtime.acquire_claim("main", "../outside", self.agents[0]["agent_id"])

    def test_claim_release_owner(self):
        claim = self.runtime.acquire_claim("main", "src.py", self.agents[0]["agent_id"])
        with self.assertRaises(WorkError):
            self.runtime.release_claim(claim, self.agents[1]["agent_id"])
        self.runtime.release_claim(claim, self.agents[0]["agent_id"])

    def test_handoff_redacts_sensitive_fields(self):
        handoff = self.runtime.write_handoff(self.agents[0]["agent_id"], {"summary": "ok", "prompt": "full prompt", "token": "abc", "scrollback": "output"})
        body = self.runtime.read_handoffs()[0]["body"]
        self.assertEqual(handoff, self.runtime.read_handoffs()[0]["handoff_id"])
        self.assertNotIn("full prompt", body)
        self.assertNotIn("abc", body)

    def test_handoff_markdown_is_bounded(self):
        self.runtime.write_handoff(self.agents[0]["agent_id"], "x" * 20000, "markdown")
        self.assertLessEqual(len(self.runtime.read_handoffs()[0]["body"]), 12000)

    def test_event_dedup(self):
        event_id = str(uuid.uuid4())
        self.runtime.append_event(event_id, "test", {"token": "secret", "value": 1})
        self.runtime.append_event(event_id, "changed", {"value": 2})
        events = self.runtime.timeline()
        self.assertEqual(len(events), 1)
        self.assertNotIn("secret", events[0]["metadata"])

    def test_event_timeline_limit(self):
        for index in range(5):
            self.runtime.append_event(str(index), "test", {"index": index})
        self.assertEqual(len(self.runtime.timeline(2)), 2)

    def test_context_contains_paths_only(self):
        Path(self.project.name, "README.md").write_text("readme", encoding="utf-8")
        bundle = self.runtime.compile_context()
        self.assertIn("README.md", bundle["files"])
        self.assertNotIn("readme", json.dumps(bundle))

    def test_context_limit(self):
        for index in range(30):
            self.runtime.create_task("task" * 20, "description" * 20)
        bundle = self.runtime.compile_context(max_chars=500)
        self.assertLessEqual(len(json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))), 500)

    def test_non_git_detection(self):
        self.assertFalse(self.runtime.status()["git"])

    def test_git_detection_read_only(self):
        git_project = tempfile.TemporaryDirectory()
        try:
            Path(git_project.name, ".git").mkdir()
            runtime = Runtime(git_project.name, self.state.name)
            self.assertTrue(runtime.status()["git"])
        finally:
            git_project.cleanup()

    def test_no_engine_process(self):
        before = set(Path("/proc").iterdir())
        self.runtime.agent_state(self.agents[0]["agent_id"], "started")
        self.assertEqual(before, set(Path("/proc").iterdir()))

    def test_status_counts(self):
        status = self.runtime.status()
        self.assertEqual(status["counts"]["agents"], 4)
        self.assertIn("events", status["counts"])

    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.runtime.transaction() as connection:
                connection.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)", ("x", self.runtime.project_id, "x", "x", 0, "open", None, "now", "now"))
                raise RuntimeError
        with self.runtime.connect() as connection:
            self.assertIsNone(connection.execute("SELECT task_id FROM tasks WHERE task_id='x'").fetchone())

    def test_concurrent_agent_registration(self):
        results = []
        errors = []
        def register(index):
            try:
                results.append(Runtime(self.project.name, self.state.name).register_agent(("markscode", "claude", "opencode", "codex")[index]))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=register, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 4)

    def test_concurrent_event_append(self):
        errors = []
        def append(index):
            try:
                Runtime(self.project.name, self.state.name).append_event(str(index), "parallel", {"index": index})
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.runtime.timeline(20)), 10)

    def test_project_root_is_canonical(self):
        self.assertEqual(self.runtime.status()["worktree"], str(Path(self.project.name).resolve()))

    def test_commands_are_not_persisted(self):
        event = self.runtime.append_event("command", "metadata", {"commands": ["rm -rf /"], "safe": True})
        self.assertNotIn("rm -rf", event["metadata"])

    def test_runtime_schema_tables(self):
        with self.runtime.connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"projects", "agents", "sessions", "tasks", "leases", "claims", "handoffs", "decisions", "events", "artifacts", "snapshots"} <= tables)

    def test_agent_stop_updates_session(self):
        agent = self.runtime.agent_state(self.agents[0]["agent_id"], "started")
        self.runtime.agent_state(agent["agent_id"], "stopped")
        with self.runtime.connect() as connection:
            self.assertIsNotNone(connection.execute("SELECT stopped_at FROM sessions WHERE agent_id=?", (agent["agent_id"],)).fetchone()[0])

    def test_lease_invalid_resource_order_is_rejected(self):
        with self.assertRaises(WorkError):
            self.runtime.acquire_lease("unknown", "resource", self.agents[0]["agent_id"])

    def test_markdown_handoff_does_not_store_prompt_key(self):
        self.runtime.write_handoff(self.agents[0]["agent_id"], "prompt=hidden token=secret", "markdown")
        body = self.runtime.read_handoffs()[0]["body"]
        self.assertNotIn("hidden", body)
        self.assertNotIn("secret", body)


if __name__ == "__main__":
    unittest.main()
