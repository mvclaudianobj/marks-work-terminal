import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from work_orchestrator.config import parse_project
from work_orchestrator.errors import WorkError
from work_orchestrator.model import PaneObservation
from work_orchestrator.monitor import Monitor
from work_orchestrator.paths import Paths
from work_orchestrator.events import encode_event
from work_orchestrator.store import append_observation, append_spool, observability_path, observability_signal_path, observability_signal_processing_path
from work_orchestrator.tmux import OBSERVE_FORMAT, Tmux
from work_orchestrator.visuals import DEFAULT_ENGINES, default_recommended_toml


class FakeTmux:
    socket = Path("/tmp/test-socket")

    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def ownership_token(self, project):
        return "t" * 40

    def owned_identity(self, project, token):
        return "$1"

    def observe_owned(self, project, token, expected_identity):
        if any(item.session_id != expected_identity for item in self.observations):
            raise WorkError("identity inesperada")
        self.calls.append("observe_owned")
        return tuple(self.observations)

    def capture_owned(self, project, token):
        raise AssertionError("agentic não pode usar snapshot")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def project(self, *, agentic=True, notify_working=False, engine="opencode"):
        engine_line = f'engine = "{engine}"\n' if engine else ""
        payload = (
            f'[project]\nname = "Alpha"\nroot = "{self.root}"\n'
            f'[[windows]]\nname = "dev"\n{engine_line}focus = true\n'
            '[[windows.panes]]\nfocus = true\n'
            f'[monitor]\nautosave = false\nagentic = {str(agentic).lower()}\nnotify_working = {str(notify_working).lower()}\nidle_warning_seconds = 300\nidle_attention_seconds = 600\n'
        ).encode()
        path = self.paths.config / "alpha.toml"
        path.write_bytes(payload)
        path.chmod(0o600)
        return parse_project(self.paths, "alpha", payload)

    def observation(self, command_class="shell", activity=None, dead=False, status=None):
        return PaneObservation("$1", "@1", "dev", 0, "%1", 0, activity, command_class, dead, status)

    def test_observe_parser_is_metadata_only_and_unknown_activity_stays_none(self):
        project = self.project()
        tmux = Tmux(self.paths)
        output = "\t".join(("$1", "alpha", "t" * 40, "@1", "0", "dev", "%1", "0", "", "/usr/bin/opencode", "0", "")) + "\n"
        rows = tmux._parse_observations(project, "t" * 40, "$1", output)
        self.assertEqual(rows[0].activity_timestamp, None)
        self.assertEqual(rows[0].command_class, "engine")
        self.assertFalse(hasattr(rows[0], "current_command"))
        self.assertNotIn("capture-pane", OBSERVE_FORMAT)
        self.assertNotIn("pane_title", OBSERVE_FORMAT)
        self.assertNotIn("pane_current_path", OBSERVE_FORMAT)
        self.assertNotIn("pane_pid", OBSERVE_FORMAT)

    def test_observe_owned_uses_only_list_panes_metadata(self):
        project = self.project()
        tmux = Tmux(self.paths)
        output = "\t".join(("$1", "alpha", "t" * 40, "@1", "0", "dev", "%1", "0", "1", "bash", "0", "")) + "\n"
        with patch.object(tmux, "run", return_value=SimpleNamespace(stdout=output)) as run:
            tmux.observe_owned(project, "t" * 40, "$1")
        argv = run.call_args.args
        rendered = " ".join(argv)
        self.assertEqual(argv[0], "list-panes")
        for forbidden in ("capture-pane", "show-environment", "pane_title", "pane_current_path", "pane_pid", "#{pid}"):
            self.assertNotIn(forbidden, rendered)

    def test_parser_rejects_ownership_mismatch_and_raw_command_never_persists(self):
        project = self.project()
        tmux = Tmux(self.paths)
        bad = "\t".join(("$1", "alpha", "x" * 40, "@1", "0", "dev", "%1", "0", "1", "secret-command", "0", "")) + "\n"
        with self.assertRaises(WorkError):
            tmux._parse_observations(project, "t" * 40, "$1", bad)
        fake = FakeTmux([self.observation("other", int(time.time()))])
        Monitor(self.paths, fake).inspect("alpha")
        journal = observability_path(self.paths.state, "alpha").read_text()
        self.assertNotIn("secret-command", journal)
        self.assertNotIn("current_command", journal)

    def test_observe_rejects_session_replacement_after_owned_identity(self):
        project = self.project()
        tmux = Tmux(self.paths)
        output = "\t".join(("$2", "alpha", "t" * 40, "@1", "0", "dev", "%1", "0", "1", "opencode", "0", "")) + "\n"
        with self.assertRaises(WorkError):
            tmux._parse_observations(project, "t" * 40, "$1", output)

    def test_first_observation_is_recorded_without_notification(self):
        self.project(notify_working=True)
        monitor = Monitor(self.paths, FakeTmux([self.observation("engine", int(time.time()))]))
        with patch("work_orchestrator.monitor.notify_or_log") as notify:
            events = monitor.inspect("alpha")
        self.assertEqual(events[0]["state"], "working")
        notify.assert_not_called()

    def test_agentic_preserves_autosave(self):
        self.project()
        monitor = Monitor(self.paths, FakeTmux([self.observation("engine", int(time.time()))]))
        with patch.object(monitor, "_autosave", return_value=True) as autosave:
            monitor.inspect("alpha")
        autosave.assert_called_once()

    def test_new_run_does_not_inherit_previous_state_or_notify(self):
        self.project(notify_working=True)
        fake = FakeTmux([self.observation("engine", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        monitor.inspect("alpha")
        fake.owned_identity = lambda project, token: "$2"
        fake.observations = [PaneObservation("$2", "@1", "dev", 0, "%1", 0, int(time.time()), "engine", False, None)]
        with patch("work_orchestrator.monitor.notify_or_log") as notify:
            monitor.inspect("alpha")
        notify.assert_not_called()

    def test_shell_engine_shell_transitions_and_dedup(self):
        self.project(notify_working=True)
        fake = FakeTmux([self.observation("shell", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        monitor.inspect("alpha")
        fake.observations = [self.observation("engine", int(time.time()))]
        with patch("work_orchestrator.monitor.notify_or_log") as notify:
            self.assertEqual(monitor.inspect("alpha")[0]["state"], "working")
            monitor.inspect("alpha")
        self.assertEqual(notify.call_count, 1)
        fake.observations = [self.observation("shell", int(time.time()))]
        with patch("work_orchestrator.monitor.notify_or_log"):
            self.assertEqual(monitor.inspect("alpha")[0]["state"], "completed")

    def test_inactivity_and_dead_states(self):
        project = self.project()
        monitor = Monitor(self.paths, FakeTmux([]))
        now = 1000
        self.assertEqual(monitor._agentic_state(project, None, "engine", 699, False, None, now), "inactive_5m")
        self.assertEqual(monitor._agentic_state(project, None, "engine", 399, False, None, now), "inactive_10m")
        self.assertEqual(monitor._agentic_state(project, None, "engine", None, True, 0, now), "completed")
        self.assertEqual(monitor._agentic_state(project, None, "engine", None, True, 1, now), "failed")

    def test_dead_engine_pane_has_priority_over_live_shell(self):
        monitor = Monitor(self.paths, FakeTmux([]))
        rows = [self.observation("engine", 10, True, 1), PaneObservation("$1", "@1", "dev", 0, "%2", 1, 20, "shell", False, None)]
        self.assertEqual(monitor._aggregate_window(rows), ("engine", 20, True, 1))

    def test_explicit_signal_spool_is_private_consumed_and_redacted(self):
        self.project()
        fake = FakeTmux([self.observation("engine", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        result = monitor.signal("alpha", "dev", "waiting_user")
        self.assertTrue(result["queued"])
        spool = observability_signal_path(self.paths.state, "alpha")
        self.assertEqual(spool.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("message", spool.read_text())
        with patch("work_orchestrator.monitor.notify_or_log"):
            monitor.inspect("alpha")
        self.assertFalse(spool.exists())
        journal = observability_path(self.paths.state, "alpha").read_text()
        self.assertIn('"source":"explicit-signal"', journal)
        self.assertIn('"state":"waiting_user"', journal)

    def test_explicit_transition_can_repeat_after_another_state(self):
        self.project()
        fake = FakeTmux([self.observation("engine", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        with patch("work_orchestrator.monitor.notify_or_log"):
            for state in ("waiting_user", "completed", "waiting_user"):
                monitor.signal("alpha", "dev", state)
                monitor.inspect("alpha")
        journal = observability_path(self.paths.state, "alpha").read_text()
        self.assertEqual(journal.count('"state":"waiting_user"'), 2)

    def test_spool_crash_retries_without_duplicate_notification(self):
        project = self.project()
        fake = FakeTmux([self.observation("engine", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        monitor.signal("alpha", "dev", "waiting_user")
        with patch("work_orchestrator.monitor.notify_or_log", side_effect=WorkError("crash")), self.assertRaises(WorkError):
            monitor._consume_signals(project, "$1", {})
        self.assertTrue(observability_signal_processing_path(self.paths.state, "alpha").exists())
        with patch("work_orchestrator.monitor.notify_or_log") as notify:
            monitor._consume_signals(project, "$1", monitor._agentic_previous(project, "$1"))
        notify.assert_called_once()
        journal = observability_path(self.paths.state, "alpha").read_text()
        self.assertEqual(journal.count('"state":"waiting_user"'), 1)

    def test_notify_failure_remains_pending_until_retry_succeeds(self):
        project = self.project()
        monitor = Monitor(self.paths, FakeTmux([]))
        latest = {"dev": {"project": "alpha", "session": "work-alpha", "pane": "%1", "window": "@1", "window_name": "dev", "run": "$1", "sequence": 1, "kind": "state", "state": "waiting_user", "source": "explicit-signal", "confidence": "explicit", "command_class": "engine", "delivery": "append"}}
        with patch("work_orchestrator.monitor.notify_or_log", side_effect=(False, True)) as notify:
            monitor._deliver(project, "$1", latest, "dev", "opencode")
            self.assertEqual(latest["dev"]["delivery"], "notify")
            self.assertEqual(monitor._agentic_previous(project, "$1")["dev"]["delivery"], "notify")
            monitor._retry_deliveries(project, "$1", latest)
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(latest["dev"]["delivery"], "notified")
        self.assertEqual(monitor._agentic_previous(project, "$1")["dev"]["delivery"], "notified")

    def test_spool_corrupt_and_forbidden_fields_are_skipped(self):
        project = self.project()
        spool = observability_signal_path(self.paths.state, "alpha")
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text('{bad json}\n' + encode_event({"project": "alpha", "session": "work-alpha", "pane": "%1", "window": "@1", "window_name": "dev", "run": "$1", "sequence": 0, "kind": "state", "state": "waiting_user", "source": "explicit-signal", "confidence": "explicit", "command_class": "engine", "message": "forbidden"}).decode())
        spool.chmod(0o600)
        Monitor(self.paths, FakeTmux([]))._consume_signals(project, "$1", {})
        self.assertFalse(observability_path(self.paths.state, "alpha").exists())

    def test_spool_overflow_and_symlink_are_rejected(self):
        spool = observability_signal_path(self.paths.state, "alpha")
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_bytes(b"x" * 32)
        spool.chmod(0o600)
        with self.assertRaises(WorkError):
            append_spool(spool, b"{}\n", max_bytes=33)
        spool.unlink()
        target = spool.parent / "target"
        target.write_text("")
        target.chmod(0o600)
        spool.symlink_to(target)
        with self.assertRaises(OSError):
            append_spool(spool, b"{}\n")

    def test_journal_rotates_and_reads_previous_segment(self):
        project = self.project()
        path = observability_path(self.paths.state, "alpha")
        append_observation(path, b"{}\n", max_bytes=5)
        append_observation(path, b"{}\n", max_bytes=5)
        self.assertTrue(path.with_suffix(".jsonl.1").exists())
        monitor = Monitor(self.paths, FakeTmux([]))
        monitor._append(project, {"project": "alpha", "session": "work-alpha", "pane": "%1", "window": "@1", "window_name": "dev", "run": "$1", "sequence": 0, "kind": "state", "state": "working", "source": "tmux-metadata", "confidence": "heuristic", "command_class": "engine"})

    def test_states_filter_preserves_internal_transition(self):
        self.project()
        path = self.paths.config / "alpha.toml"
        path.write_text(path.read_text().replace("idle_attention_seconds = 600\n", 'idle_attention_seconds = 600\nstates = ["completed"]\n'))
        path.chmod(0o600)
        fake = FakeTmux([self.observation("engine", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        monitor.inspect("alpha")
        self.assertFalse(observability_path(self.paths.state, "alpha").exists())
        self.assertEqual(monitor.status("alpha")["windows"][0]["state"], "working")
        fake.observations = [self.observation("shell", int(time.time()))]
        monitor.inspect("alpha")
        self.assertIn('"state":"completed"', observability_path(self.paths.state, "alpha").read_text())

    def test_append_rejects_non_metadata_fields(self):
        project = self.project()
        event = {"project": "alpha", "session": "work-alpha", "pane": "%1", "run": "$1", "sequence": 0, "kind": "state", "state": "working", "message": "secret"}
        with self.assertRaises(WorkError):
            Monitor(self.paths, FakeTmux([]))._append(project, event)

    def test_multipane_live_engine_beats_dead_engine(self):
        rows = [self.observation("engine", 1, True, 1), PaneObservation("$1", "@1", "dev", 0, "%2", 1, 2, "engine", False, None)]
        self.assertEqual(Monitor._aggregate_window(rows), ("engine", 2, False, None))

    def test_multipane_single_dead_engine_beats_live_shell(self):
        rows = [self.observation("engine", 1, True, 1), PaneObservation("$1", "@1", "dev", 0, "%2", 1, 2, "shell", False, None)]
        self.assertEqual(Monitor._aggregate_window(rows), ("engine", 2, True, 1))

    def test_capture_owned_rejects_recreated_session(self):
        project = self.project()
        tmux = Tmux(self.paths)
        with patch.object(tmux, "run", return_value=SimpleNamespace(stdout="")), patch.object(tmux, "_parse_capture", return_value=("$2", {}, ())):
            with self.assertRaises(WorkError):
                tmux.capture_owned(project, "t" * 40, "$1")

    def test_status_uses_latest_event_for_reused_window_name(self):
        self.project()
        monitor = Monitor(self.paths, FakeTmux([]))
        project = self.project()
        monitor._append(project, {"project": "alpha", "session": "work-alpha", "pane": "%1", "window": "@1", "window_name": "dev", "run": "$1", "sequence": 0, "kind": "state", "state": "idle", "source": "tmux-metadata", "confidence": "heuristic", "command_class": "shell"})
        monitor._append(project, {"project": "alpha", "session": "work-alpha", "pane": "%2", "window": "@2", "window_name": "dev", "run": "$1", "sequence": 0, "kind": "state", "state": "working", "source": "tmux-metadata", "confidence": "heuristic", "command_class": "engine"})
        self.assertEqual(monitor.status("alpha")["windows"][0]["state"], "working")

    def test_notifier_is_static_and_redacted(self):
        project = self.project(notify_working=True)
        monitor = Monitor(self.paths, FakeTmux([]))
        with patch("work_orchestrator.monitor.notify_or_log") as notify:
            monitor._notify_state(project, "phase_started", "dev", "opencode")
        title, body = notify.call_args.args
        self.assertEqual(title, "Alpha — dev — opencode")
        self.assertEqual(body, "iniciou nova fase")
        self.assertNotIn("command", title + body)

    def test_config_defaults_engines_legacy_and_status(self):
        legacy = self.project(agentic=False, engine=None)
        self.assertFalse(legacy.monitor.agentic)
        self.assertFalse(legacy.monitor.notify_working)
        self.assertIsNone(legacy.windows[0].engine)
        recommended = default_recommended_toml("alpha")
        for name, engine in DEFAULT_ENGINES.items():
            self.assertIn(f'name = "{name}"', recommended)
            self.assertIn(f'engine = "{engine}"', recommended)
        self.project()
        fake = FakeTmux([self.observation("shell", int(time.time()))])
        monitor = Monitor(self.paths, fake)
        monitor.inspect("alpha")
        status = monitor.status("alpha")
        self.assertTrue(status["agentic"])
        self.assertEqual(status["windows"][0]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
