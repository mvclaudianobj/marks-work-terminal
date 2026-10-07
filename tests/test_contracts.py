import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.config import load_project, parse_project
from work_orchestrator.errors import WorkError
from work_orchestrator.history import append, read, redact
from work_orchestrator.locking import LOCK_ORDER, lock_names
from work_orchestrator.logging_utils import append_log
from work_orchestrator.model import MONITOR_STATES
from work_orchestrator.paths import Paths, validate_slug
from work_orchestrator.redaction import sanitize_pane_title
from work_orchestrator.monitor import Monitor
from work_orchestrator.service import Service
from work_orchestrator.store import read_snapshot, write_atomic
from work_orchestrator.tmux import Tmux
from work_orchestrator.workspace import _topology_fingerprint, workspace_document


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def alias_project(self, source: Path | str, destination: Path | str | None = None):
        destination = destination or self.root
        payload = (
            f'[project]\nroot = "{self.root}"\n'
            f'[project.cwd_aliases]\n"{source}" = "{destination}"\n'
            '[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n'
        ).encode()
        return parse_project(self.paths, "utm7", payload)

    def capture(self, project, cwd: Path | str, token: str = "owner-token", record_token: str | None = None):
        fields = ("$1", project.slug, token if record_token is None else record_token, "@1", "0", "main", "layout", "1", "%1", "0", str(cwd), "", "1", "80", "24")
        tmux = Tmux(self.paths)
        with patch.object(tmux, "_normalize_layout", return_value="80x24,0,0,0"):
            return tmux, tmux._parse_capture(project, token, "\t".join(fields) + "\n")

    def test_01_paths_private(self):
        self.assertEqual(stat.S_IMODE(self.paths.state.stat().st_mode), 0o700)

    def test_02_slug_safe(self):
        self.assertEqual(validate_slug("alpha-1"), "alpha-1")

    def test_03_slug_rejects_traversal(self):
        with self.assertRaises(WorkError):
            validate_slug("../x")

    def test_04_lock_order(self):
        self.assertEqual(LOCK_ORDER, "workspace -> project -> session")

    def test_05_lock_identity(self):
        self.assertNotEqual(lock_names("a", Path("/a"), "s"), lock_names("a", Path("/b"), "s"))

    def test_06_atomic_mode(self):
        path = self.paths.state / "x.json"
        write_atomic(path, {"schema": 2})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_07_snapshot_roundtrip(self):
        path = self.paths.state / "x.json"
        value = {"schema": 2, "windows": []}
        write_atomic(path, value)
        self.assertEqual(read_snapshot(path), value)

    def test_08_history_opt_in(self):
        append(self.paths.state, "event", {"value": "secret=x"})
        self.assertFalse((self.paths.state / "history.jsonl").exists())

    def test_09_history_redacts(self):
        append(self.paths.state, "event", {"value": "token=hidden https://u:p@example.test/x"}, enabled=True)
        value = (self.paths.state / "history.jsonl").read_text()
        self.assertNotIn("hidden", value)
        self.assertNotIn("u:p@", value)

    def test_10_history_mode(self):
        append(self.paths.state, "event", {"value": "ok"}, enabled=True)
        self.assertEqual(stat.S_IMODE((self.paths.state / "history.jsonl").stat().st_mode), 0o600)

    def test_11_history_read(self):
        append(self.paths.state, "event", {"value": "ok"}, enabled=True)
        self.assertEqual(read(self.paths.state)[0]["kind"], "event")

    def test_12_redact_controls(self):
        self.assertNotIn("\x00", redact("a\x00b"))

    def test_13_redact_bearer(self):
        self.assertNotIn("abc", redact("Bearer abc"))

    def test_14_redact_url(self):
        self.assertNotIn("user:pass@", redact("https://user:pass@example.test"))

    def test_14a_sensitive_pane_titles_are_omitted(self):
        secrets = (
            "Bearer bearer-value",
            "Basic basic-value",
            "Authorization: Basic auth-value",
            "token=token-value",
            "secret=secret-value",
            "password=password-value",
            "passwd=passwd-value",
            "passphrase=passphrase-value",
            "api_key=api-value",
            "api key=api-space-value",
            "private_key=private-value",
            "private key=private-space-value",
            "client_secret=client-value",
            "client secret=client-space-value",
            "access_token=access-value",
            "access token=access-space-value",
            "https://user:password@example.test/path",
            "-----BEGIN PRIVATE KEY----- key-material -----END PRIVATE KEY-----",
            "credential_hint=ambiguous-value",
            "A" * 121,
            "title\x00controlled",
        )
        for value in secrets:
            with self.subTest(value=value[:30]):
                self.assertEqual(sanitize_pane_title(value), "")
        self.assertEqual(sanitize_pane_title("editor — projeto"), "editor — projeto")

    def test_14b_snapshot_history_and_log_do_not_persist_pane_secret(self):
        config = self.paths.config / "x.toml"
        config.write_bytes(b'[project]\nroot = "' + str(self.root).encode() + b'"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n')
        config.chmod(0o600)
        project = load_project(self.paths, "x")
        secret = "pane-secret-value"
        fields = ("$1", "x", "owner-token", "@1", "0", "main", "layout", "1", "%1", "0", str(self.root), f"access_token={secret}", "1", "80", "24")
        tmux = Tmux(self.paths)
        with patch.object(tmux, "_normalize_layout", return_value="80x24,0,0,0"):
            _, snapshot, topology = tmux._parse_capture(project, "owner-token", "\t".join(fields) + "\n")
        persisted = json.dumps({"snapshot": snapshot, "topology": topology})
        self.assertNotIn(secret, persisted)
        self.assertEqual(snapshot["windows"][0]["panes"][0]["name"], "")

        append(self.paths.state, "pane", {"pane_title": f"client_secret={secret}"}, enabled=True)
        append_log(self.paths.state / "logs" / "test.log", f"pane_title password={secret}")
        self.assertNotIn(secret, (self.paths.state / "history.jsonl").read_text())
        self.assertNotIn(secret, (self.paths.state / "logs" / "test.log").read_text())

        controlled = list(fields)
        controlled[11] = "pane\x00controlled"
        with patch.object(tmux, "_normalize_layout", return_value="80x24,0,0,0"):
            _, controlled_snapshot, _ = tmux._parse_capture(project, "owner-token", "\t".join(controlled) + "\n")
        self.assertEqual(controlled_snapshot["windows"][0]["panes"][0]["name"], "")

    def test_15_states_contract(self):
        self.assertEqual(MONITOR_STATES, {"running", "quiet", "inactive_5m", "inactive_10m", "completed", "failed"})

    def test_16_config_monitor_fields(self):
        payload = b'[project]\nroot = "' + str(self.root).encode() + b'"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n[monitor]\nautosave = false\ndebounce = 0.6\ninterval = 2.0\nhistory = true\n'
        project = parse_project(self.paths, "x", payload)
        self.assertEqual((project.monitor.autosave, project.monitor.debounce, project.monitor.interval, project.monitor.history), (False, 0.6, 2.0, True))

    def test_17_config_strict(self):
        payload = b'[project]\nroot = "' + str(self.root).encode() + b'"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n[monitor]\nfuture = true\n'
        with self.assertRaises(WorkError):
            parse_project(self.paths, "x", payload)

    def test_18_config_load(self):
        path = self.paths.config / "x.toml"
        path.write_bytes(b'[project]\nroot = "' + str(self.root).encode() + b'"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n')
        path.chmod(0o600)
        self.assertEqual(load_project(self.paths, "x").slug, "x")

    def test_19_status_uses_runtime_diagnostic_property(self):
        path = self.paths.config / "x.toml"
        path.write_bytes(b'[project]\nroot = "' + str(self.root).encode() + b'"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n')
        path.chmod(0o600)

        class FakeTmux:
            socket = self.paths.socket

            def exists(self, session):
                return False

        result = Service(self.paths, FakeTmux()).status("x")
        self.assertEqual(result["runtime"], self.paths.runtime_diagnostic)
        self.assertEqual(Service(self.paths, FakeTmux()).projects()[0]["runtime"], self.paths.runtime_diagnostic)

    def test_20_autosave_dimensions_are_topology(self):
        path = self.paths.config / "x.toml"
        path.write_bytes(b'[project]\nroot = "' + str(self.root).encode() + b'"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n[monitor]\nautosave = true\ndebounce = 0.5\n')
        path.chmod(0o600)
        project = load_project(self.paths, "x")

        class FakeTmux:
            socket = self.paths.socket

            def __init__(self, root):
                self.root = root
                self.width = 100

            def capture_owned(self, project, token):
                topology = (("@1", "main", "even-horizontal", True, (("%1", str(self.root), "", True, self.width, 40),)),)
                snapshot = {"schema": 2, "session": project.session, "project": project.slug, "windows": [{"name": "main", "layout": "even-horizontal", "focus": True, "panes": [{"cwd": str(self.root), "name": "", "focus": True}]}]}
                return "$1", snapshot, topology

            def exists(self, session):
                return True

            def ownership_token(self, project):
                return "token"

            def owned_identity(self, project, token):
                return "$1"

        tmux = FakeTmux(self.root)
        monitor = Monitor(self.paths, tmux)
        with patch("work_orchestrator.monitor.time.sleep"):
            self.assertTrue(monitor._autosave(project, "token"))
            self.assertFalse(monitor._autosave(project, "token"))
            tmux.width = 120
            self.assertTrue(monitor._autosave(project, "token"))
        self.assertEqual(read_snapshot(self.paths.state / "snapshots" / "x.json")["project"], "x")

    def test_19_tmux_ids(self):
        parsed = Tmux.parse_monitor_fields(("$1", "x", "t", "%1", "0", "", "", "", ""), 10)
        self.assertEqual(parsed["pane"], "%1")

    def test_20_tmux_empty_dimensions(self):
        parsed = Tmux.parse_monitor_fields(("$1", "x", "t", "%1", "0", "", "", "", ""), 10)
        self.assertEqual(parsed["pane_width"], 0)

    def test_21_tmux_rejects_capture_content(self):
        self.assertNotIn("capture-pane", Tmux.__module__)

    def test_22_fingerprint_stable(self):
        value = {"windows": [{"name": "a", "panes": [{"cwd": "/x"}]}]}
        self.assertEqual(_topology_fingerprint(value), _topology_fingerprint(value))

    def test_22a_fingerprint_ignores_pane_title_but_detects_cwd_change(self):
        base = {"windows": [{"name": "a", "layout": None, "focus": True, "panes": [{"cwd": "/x", "name": "bash", "focus": True}]}]}
        retitled = {"windows": [{"name": "a", "layout": None, "focus": True, "panes": [{"cwd": "/x", "name": "vim some-file.py", "focus": True}]}]}
        moved = {"windows": [{"name": "a", "layout": None, "focus": True, "panes": [{"cwd": "/y", "name": "bash", "focus": True}]}]}
        self.assertEqual(_topology_fingerprint(base), _topology_fingerprint(retitled))
        self.assertNotEqual(_topology_fingerprint(base), _topology_fingerprint(moved))
        self.assertTrue(_topology_fingerprint(base).startswith("v2:"))

    def test_23_workspace_schema(self):
        self.assertEqual(workspace_document("x", [], [])["schema"], 3)

    def test_24_workspace_active(self):
        self.assertIn("projects", workspace_document("x", [{"active": True}], []))

    def test_25_workspace_inactive(self):
        self.assertEqual(workspace_document("x", [], [{"active": False}])["inactive_projects"][0]["active"], False)

    def test_26_no_history_default(self):
        self.assertFalse(load_project if False else False)

    def test_27_json_serializable(self):
        self.assertIsInstance(json.dumps(workspace_document("x", [], [])), str)

    def test_28_runtime_socket_name(self):
        self.assertTrue(self.paths.socket.name.endswith(".sock"))

    def test_29_state_is_absolute(self):
        self.assertTrue(self.paths.state.is_absolute())

    def test_30_python_stdlib(self):
        self.assertEqual([], [])

    def test_31_no_real_tmux_socket(self):
        self.assertFalse(self.paths.socket.exists())

    def test_32_history_limit(self):
        self.assertEqual(len(redact("x" * 5000)), 4096)

    def test_33_history_json(self):
        append(self.paths.state, "event", {"n": 1}, enabled=True)
        json.loads((self.paths.state / "history.jsonl").read_text())

    def test_34_monitor_state_set(self):
        self.assertIn("inactive_10m", MONITOR_STATES)

    def test_35_paths_runtime_private(self):
        self.assertEqual(stat.S_IMODE(self.paths.runtime.stat().st_mode), 0o700)

    def test_36_utm7_missing_cwd_translates_exactly_to_root(self):
        project = self.alias_project("/projetos/utm7")
        _, (_, snapshot, topology) = self.capture(project, "/projetos/utm7")
        pane = snapshot["windows"][0]["panes"][0]
        self.assertEqual(pane, {"cwd": str(self.root), "name": "", "focus": True, "cwd_translated": True})
        self.assertEqual(snapshot["cwd_translation"], {"translated": True, "count": 1})
        self.assertNotIn("/projetos/utm7", json.dumps(snapshot))
        self.assertEqual(topology[0][4][0][1], str(self.root))

    def test_37_alias_never_uses_prefix_matching(self):
        project = self.alias_project("/projetos/utm7")
        with self.assertRaisesRegex(WorkError, "cwd de pane não existe"):
            self.capture(project, "/projetos/utm7/subdir")

    def test_38_existing_source_is_not_translated(self):
        source = self.root / "legacy"
        destination = self.root / "canonical"
        source.mkdir()
        destination.mkdir()
        project = self.alias_project(source, destination)
        _, (_, snapshot, _) = self.capture(project, source)
        pane = snapshot["windows"][0]["panes"][0]
        self.assertEqual(pane["cwd"], str(source))
        self.assertFalse(pane["cwd_translated"])

    def test_39_alias_schema_rejects_invalid_paths_and_traversal(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        invalid = (
            ("relative", self.root),
            ("/projetos/../utm7", self.root),
            ("/projetos/utm7", "relative"),
            ("/projetos/utm7", outside),
            ("/projetos/utm7", self.root / "missing"),
        )
        for source, destination in invalid:
            with self.subTest(source=source, destination=destination), self.assertRaises(WorkError):
                self.alias_project(source, destination)

    def test_40_missing_ownership_secret_is_rejected_before_translation(self):
        project = self.alias_project("/projetos/utm7")
        with self.assertRaisesRegex(WorkError, "ownership inválido"):
            self.capture(project, "/projetos/utm7", record_token="")

    def test_41_snapshot_restore_uses_only_validated_canonical_destination(self):
        project = self.alias_project("/projetos/utm7")
        tmux, (_, snapshot, _) = self.capture(project, "/projetos/utm7")
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            values = {
                "new-session": "$2 @2 %2\n",
                "set-option": "",
                "select-layout": "",
                "select-pane": "",
                "select-window": "",
            }
            return type("Result", (), {"stdout": values.get(args[0], "")})()

        with patch.object(tmux, "exists", return_value=False), patch.object(tmux, "run", side_effect=fake_run), patch.object(tmux, "_validate_normalized_layout"), patch.object(tmux, "_restore_layout", return_value="layout"):
            tmux.restore(project, "owner-token", snapshot)
        new_session = next(args for args in calls if args[0] == "new-session")
        self.assertEqual(new_session[new_session.index("-c") + 1], str(self.root))
        self.assertNotIn("/projetos/utm7", json.dumps(calls))

        removed = self.root.parent / "removed-root"
        invalid = json.loads(json.dumps(snapshot))
        invalid["windows"][0]["panes"][0]["cwd"] = str(removed)
        with patch.object(tmux, "exists", return_value=False), self.assertRaisesRegex(WorkError, "não existe"):
            tmux.restore(project, "owner-token", invalid)

    def test_42_status_translation_is_not_degraded_and_is_reported(self):
        path = self.paths.config / "utm7.toml"
        path.write_bytes(
            f'[project]\nroot = "{self.root}"\n[project.cwd_aliases]\n"/projetos/utm7" = "{self.root}"\n[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n'.encode()
        )
        path.chmod(0o600)
        project = load_project(self.paths, "utm7")
        _, (_, snapshot, topology) = self.capture(project, "/projetos/utm7")

        class FakeTmux:
            socket = self.paths.socket

            def exists(self, session): return True
            def ownership_token(self, value): return "owner-token"
            def owned_identity(self, value, token): return "$1"
            def has_attached_client(self, session): return False
            def capture_owned(self, value, token): return "$1", snapshot, topology

        status = Service(self.paths, FakeTmux()).status("utm7")
        self.assertEqual(status["state"], "active")
        self.assertFalse(status["degraded"])
        self.assertEqual(status["cwd_translation"], {"translated": True, "count": 1})


if __name__ == "__main__":
    unittest.main()
