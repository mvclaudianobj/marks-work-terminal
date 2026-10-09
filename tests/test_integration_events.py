import io
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.cli import main, parser
from work_orchestrator.errors import WorkError
from work_orchestrator.integration import TOP_LEVEL_FIELDS, append_integration_event, build_integration_event, integration_events_path, read_integration_events, validate_integration_event
from work_orchestrator.model import PaneObservation
from work_orchestrator.monitor import Monitor
from work_orchestrator.paths import Paths


class FakeTmux:
    socket = Path("/tmp/test-socket")

    def __init__(self, run="$1"):
        self.run = run
        self.observations = [PaneObservation(run, "@1", "dev", 0, "%1", 0, int(time.time()), "engine", False, None)]

    def ownership_token(self, project):
        return "t" * 40

    def owned_identity(self, project, token):
        return self.run

    def observe_owned(self, project, token, expected_identity):
        return tuple(self.observations)


class IntegrationEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def project(self, *, agentic=True):
        payload = (
            f'[project]\nname = "Alpha"\nroot = "{self.root}"\n'
            '[[windows]]\nname = "dev"\nengine = "opencode"\nfocus = true\n'
            '[[windows.panes]]\nfocus = true\n'
            f'[monitor]\nautosave = false\nagentic = {str(agentic).lower()}\n'
        ).encode()
        path = self.paths.config / "alpha.toml"
        path.write_bytes(payload)
        path.chmod(0o600)

    def events(self):
        return read_integration_events(self.paths.state, self.paths.runtime, "alpha", limit=10000)

    def test_schema_is_strict_metadata_only_and_event_id_is_deterministic(self):
        subject = {"window": "@1", "window_name": "dev", "pane": "%1"}
        data = {"engine": "opencode", "command_class": "engine", "activity_timestamp": 123, "pane_dead": False, "pane_dead_status": None}
        first = build_integration_event("work.window.observed", "alpha", "work-alpha", "$1", 7, subject, data, timestamp=99)
        second = build_integration_event("work.window.observed", "alpha", "work-alpha", "$1", 7, subject, data, timestamp=99)
        self.assertEqual(first, second)
        self.assertEqual(set(first), TOP_LEVEL_FIELDS)
        self.assertEqual(first["schema"], "work.events.v1")
        self.assertEqual(first["producer"], "work-monitor")
        serialized = json.dumps(first)
        for forbidden in ("command", "title", "cwd", "pid", "argv", "env", "prompt", "scrollback", "content", "message"):
            self.assertNotIn(f'"{forbidden}"', serialized)
        invalid = dict(first)
        invalid["message"] = "secret"
        with self.assertRaises(WorkError):
            validate_integration_event(invalid)

    def test_private_rotating_journal_and_symlink_rejection(self):
        subject = {"window": "@1", "window_name": "dev", "pane": "%1"}
        data = {"command_class": "engine", "activity_timestamp": None, "pane_dead": False, "pane_dead_status": None}
        with patch("work_orchestrator.integration.MAX_JOURNAL_BYTES", 900):
            for index in range(6):
                append_integration_event(self.paths.state, self.paths.runtime, "alpha", "work.window.observed", "work-alpha", "$1", subject, data, timestamp=index)
            events = read_integration_events(self.paths.state, self.paths.runtime, "alpha", limit=10000)
        path = integration_events_path(self.paths.state, "alpha")
        self.assertTrue(path.with_suffix(".jsonl.1").exists())
        self.assertTrue(events)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.unlink()
        path.symlink_to(self.root / "target")
        with self.assertRaises(WorkError):
            append_integration_event(self.paths.state, self.paths.runtime, "alpha", "work.window.observed", "work-alpha", "$1", subject, data)
        path.unlink()
        path.write_text("", encoding="utf-8")
        path.chmod(0o600)
        os.link(path, self.root / "hardlink")
        with self.assertRaises(WorkError):
            append_integration_event(self.paths.state, self.paths.runtime, "alpha", "work.window.observed", "work-alpha", "$1", subject, data)

    def test_sequence_recovers_from_journal_after_sequence_file_loss(self):
        first = append_integration_event(self.paths.state, self.paths.runtime, "alpha", "work.monitor.started", "work-alpha", "$1", {}, {"reason": "first_observation"}, timestamp=1)
        (self.paths.state / "integration-events" / "alpha.sequence").unlink()
        second = append_integration_event(self.paths.state, self.paths.runtime, "alpha", "work.monitor.stopped", "work-alpha", "$1", {}, {"reason": "normal"}, timestamp=2)
        self.assertEqual((first["sequence"], second["sequence"]), (0, 1))

    def test_agentic_lifecycle_run_isolation_observation_and_signal(self):
        self.project()
        fake = FakeTmux()
        monitor = Monitor(self.paths, fake)
        monitor.inspect("alpha")
        monitor.signal("alpha", "dev", "waiting_user")
        monitor.inspect("alpha")
        fake.run = "$2"
        fake.observations = [PaneObservation("$2", "@1", "dev", 0, "%1", 0, 124, "shell", False, None)]
        Monitor(self.paths, fake).inspect("alpha")
        events = self.events()
        types = [event["type"] for event in events]
        self.assertIn("work.monitor.started", types)
        self.assertIn("work.window.observed", types)
        self.assertIn("work.window.signal", types)
        changed = next(event for event in events if event["type"] == "work.run.changed")
        self.assertEqual((changed["run"], changed["data"]["previous_run"]), ("$2", "$1"))
        signal = next(event for event in events if event["type"] == "work.window.signal")
        self.assertEqual(signal["data"], {"state": "waiting_user", "confidence": "explicit", "engine": "opencode"})

    def test_sink_failure_is_isolated_from_autosave_and_legacy_flow(self):
        self.project()
        monitor = Monitor(self.paths, FakeTmux())
        with patch.object(monitor, "_autosave", return_value=True) as autosave, patch("work_orchestrator.monitor.append_integration_event", side_effect=WorkError("sink")):
            events = monitor.inspect("alpha")
        autosave.assert_called_once()
        self.assertEqual(events[0]["state"], "working")

    def test_once_monitor_emits_normal_stopped_lifecycle(self):
        self.project()
        Monitor(self.paths, FakeTmux()).start("alpha", once=True)
        types = [event["type"] for event in self.events()]
        self.assertEqual(types[0], "work.monitor.started")
        self.assertEqual(types[-1], "work.monitor.stopped")

    def test_agentic_false_does_not_publish_integration_events(self):
        self.project(agentic=False)
        monitor = Monitor(self.paths, FakeTmux())
        with patch.object(monitor, "_autosave", return_value=False), patch.object(monitor, "_inspect_legacy", return_value=[]):
            self.assertEqual(monitor.inspect("alpha"), [])
        self.assertFalse(integration_events_path(self.paths.state, "alpha").exists())

    def test_cli_parser_and_incremental_json_lines(self):
        self.project()
        for timestamp in (1, 2, 3):
            append_integration_event(self.paths.state, self.paths.runtime, "alpha", "work.monitor.started", "work-alpha", "$1", {}, {"reason": "first_observation"}, timestamp=timestamp)
        args = parser().parse_args(["integration", "events", "alpha", "--after", "0", "--limit", "1"])
        self.assertEqual((args.slug, args.after, args.limit), ("alpha", 0, 1))
        output = io.StringIO()
        environment = {"XDG_CONFIG_HOME": str(self.paths.config.parent.parent), "XDG_STATE_HOME": str(self.paths.state.parent), "XDG_RUNTIME_DIR": str(self.paths.runtime.parent)}
        with patch.dict(os.environ, environment, clear=False), patch("work_orchestrator.cli.Paths.discover", return_value=self.paths), patch("work_orchestrator.cli.Monitor") as monitor, redirect_stdout(output):
            monitor.return_value.tmux.ownership_token.return_value = "t" * 40
            monitor.return_value.tmux.owned_identity.return_value = "$1"
            main(["integration", "events", "alpha", "--after", "0", "--limit", "1"])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["sequence"], 1)


if __name__ == "__main__":
    unittest.main()
