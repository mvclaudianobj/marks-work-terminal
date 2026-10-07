import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.config import parse_project
from work_orchestrator.errors import WorkError
from work_orchestrator.paths import Paths
from work_orchestrator.store import snapshot_path, write_atomic
from work_orchestrator.tmux import Tmux
from work_orchestrator.workspace import WorkspaceService, _topology_fingerprint, visual_document, workspace_document, workspace_path


class TmuxGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.socket = self.paths.runtime / "groups.sock"
        self.previous = os.environ.get("WORK_TMUX_SOCKET")
        self.previous_testing = os.environ.get("WORK_ORCHESTRATOR_TESTING")
        os.environ["WORK_TMUX_SOCKET"] = str(self.socket)
        os.environ["WORK_ORCHESTRATOR_TESTING"] = "1"
        self.tmux = Tmux(self.paths)
        payload = (
            f'[project]\nroot = "{base}"\nsession = "work-integration"\n'
            '[[windows]]\nname = "dev"\nfocus = true\n[[windows.panes]]\nfocus = true\n'
            '[[windows]]\nname = "apoio"\nfocus = false\n[[windows.panes]]\nfocus = true\n'
        ).encode()
        self.project = parse_project(self.paths, "integration", payload)
        self.token = self.tmux.ownership_token(self.project)
        self.parent_id, _ = self.tmux.create_from_project(self.project, self.token, False)
        self.clients = []
        self.ptys = []

    def tearDown(self):
        for process in self.clients:
            process.terminate()
        for process in self.clients:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        for master, slave in self.ptys:
            os.close(master)
            os.close(slave)
        self.tmux.run("kill-server", check=False)
        if self.previous is None:
            os.environ.pop("WORK_TMUX_SOCKET", None)
        else:
            os.environ["WORK_TMUX_SOCKET"] = self.previous
        if self.previous_testing is None:
            os.environ.pop("WORK_ORCHESTRATOR_TESTING", None)
        else:
            os.environ["WORK_ORCHESTRATOR_TESTING"] = self.previous_testing
        self.temp.cleanup()

    def panes(self, target):
        result = self.tmux.run("list-panes", "-s", "-t", target, "-F", "#{pane_id}\t#{pane_pid}")
        return sorted(tuple(line.split("\t")) for line in result.stdout.splitlines())

    def detach_hook(self, target):
        return self.tmux.run("show-hooks", "-t", target, "client-detached").stdout

    def assert_literal_detach_hook(self, target):
        hook = self.detach_hook(target)
        executable = str((Path(__file__).resolve().parents[1] / "bin" / "work-hook-detach").resolve())
        self.assertIn(shlex.join([executable, self.project.slug]), hook)
        self.assertNotIn("#{hook_session}", hook)
        self.assertNotIn("#{session_name}", hook)
        self.assertNotIn("session_name", hook)

    def attach(self, group):
        master, slave = os.openpty()
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        process = subprocess.Popen(
            [*self.tmux.base, "attach-session", "-t", f"={group}"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
            start_new_session=True,
        )
        self.ptys.append((master, slave))
        self.clients.append(process)

    def attach_window(self, group, window_id):
        master, slave = os.openpty()
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        process = subprocess.Popen(
            [*self.tmux.base, "attach-session", "-t", f"={group}", ";", "switch-client", "-t", f"={group}:{window_id}"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
            start_new_session=True,
        )
        self.ptys.append((master, slave))
        self.clients.append(process)

    def test_four_visual_groups_share_panes_and_are_filtered(self):
        self.assert_literal_detach_hook(self.parent_id)
        before = self.panes(self.parent_id)
        groups = []
        group_ids = []
        for visual_id, window in (("dev-1", "dev"), ("dev-2", "dev"), ("apoio-1", "apoio"), ("apoio-2", "apoio")):
            group_id, group = self.tmux.ensure_visual_group(self.project, self.token, visual_id, window)
            group_ids.append(group_id)
            groups.append(group)
            self.attach(group)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(len(self.tmux.client_targets(group)) != 1 for group in groups):
            time.sleep(0.05)
        self.assertEqual([len(self.tmux.client_targets(group)) for group in groups], [1, 1, 1, 1])
        self.assertEqual([self.tmux.client_targets(group)[0] for group in groups], ["dev", "dev", "apoio", "apoio"])
        self.assertEqual(
            [self.tmux.visual_claimed(self.project, self.token, visual_id, window) for visual_id, window in (("dev-1", "dev"), ("dev-2", "dev"), ("apoio-1", "apoio"), ("apoio-2", "apoio"))],
            [True, True, True, True],
        )
        for group_id, group in zip(group_ids, groups):
            self.assertEqual(self.panes(f"={group}"), before)
            self.assert_literal_detach_hook(group_id)
        self.assertEqual(self.tmux.sessions(), [{"name": "work-integration", "owned": True}])
        count = self.tmux.run("list-sessions", "-F", "#{session_name}").stdout.splitlines().count("work-integration")
        self.assertEqual(count, 1)
        group_id, group = self.tmux.ensure_visual_group(self.project, self.token, "dev-1", "dev")
        self.assertEqual(group, groups[0])
        self.assertEqual(group_id, self.tmux.session_id(group))
        external = self.tmux.visual_group_name(self.project, "external")
        self.tmux.run("new-session", "-d", "-s", external)
        with self.assertRaises(WorkError):
            self.tmux.ensure_visual_group(self.project, self.token, "external", "dev")
        self.assertEqual(self.tmux.run("show-hooks", "-t", f"={external}", "client-detached", check=False).stdout, "")
        self.clients[0].terminate()
        self.clients[0].wait(timeout=3)
        self.tmux.run("kill-session", "-t", group_id)
        self.assertEqual(self.panes(self.parent_id), before)
        self.assertTrue(self.tmux.exists(self.project.session))

    def test_visual_attach_queue_selects_requested_window_after_reselection(self):
        group_id, group = self.tmux.ensure_visual_group(self.project, self.token, "apoio-1", "apoio")
        apoio_id = self.tmux._window_id(group_id, "apoio")
        self.tmux.run("select-window", "-t", f"{group_id}:{self.tmux._window_id(group_id, 'dev')}")
        self.attach_window(group, apoio_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.tmux.client_targets(group) != ["apoio"]:
            time.sleep(0.05)
        self.assertEqual(self.tmux.client_targets(group), ["apoio"])

    def test_attach_visual_execs_attach_and_switch_client_in_one_queue(self):
        group_id, _ = self.tmux.ensure_visual_group(self.project, self.token, "apoio-1", "apoio")
        apoio_id = self.tmux._window_id(group_id, "apoio")
        with patch("work_orchestrator.tmux.os.execvp") as execvp:
            self.tmux.attach_visual(self.project, self.token, "apoio-1", "apoio")
        execvp.assert_called_once_with(
            "tmux",
            [*self.tmux.base, "attach-session", "-t", group_id, ";", "switch-client", "-t", f"{group_id}:{apoio_id}"],
        )

    def test_visual_claim_requires_owned_group_topology_client_and_unambiguous_markers(self):
        groups = []
        for visual_id, window in (("dev-1", "dev"), ("dev-2", "dev"), ("apoio-1", "apoio"), ("apoio-2", "apoio")):
            _, group = self.tmux.ensure_visual_group(self.project, self.token, visual_id, window)
            groups.append(group)
        self.attach(groups[0])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.tmux.client_targets(groups[0]):
            time.sleep(0.05)
        self.assertEqual(
            [self.tmux.visual_claimed(self.project, self.token, visual_id, window) for visual_id, window in (("dev-1", "dev"), ("dev-2", "dev"), ("apoio-1", "apoio"), ("apoio-2", "apoio"))],
            [True, False, False, False],
        )
        self.attach(self.project.session)
        while time.monotonic() < deadline and not self.tmux.client_targets(self.project.session):
            time.sleep(0.05)
        self.assertFalse(self.tmux.visual_claimed(self.project, self.token, "dev-2", "dev"))
        self.tmux.run("set-option", "-t", self.tmux.session_id(groups[1]), "@work-orchestrator-visual", "dev-1")
        self.assertFalse(self.tmux.visual_claimed(self.project, self.token, "dev-1", "dev"))
        self.tmux.run("set-option", "-t", self.tmux.session_id(groups[1]), "@work-orchestrator-visual", "dev-2")
        self.tmux.run("set-option", "-t", self.tmux.session_id(groups[0]), "@work-orchestrator-parent-session-id", "$999")
        self.assertFalse(self.tmux.visual_claimed(self.project, self.token, "dev-1", "dev"))

    def test_workspace_restore_real_tmux_is_idempotent_for_one_attached_and_three_empty_groups(self):
        visuals = (("dev-1", "dev"), ("dev-2", "dev"), ("apoio-1", "apoio"), ("apoio-2", "apoio"))
        groups = []
        for visual_id, window in visuals:
            _, group = self.tmux.ensure_visual_group(self.project, self.token, visual_id, window)
            groups.append(group)
        self.attach(groups[0])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.tmux.client_targets(groups[0]):
            time.sleep(0.05)
        snapshot = {
            "schema": 2,
            "timestamp": "2026-10-04T00:00:00+00:00",
            "session": self.project.session,
            "project": self.project.slug,
            "windows": [
                {"name": "dev", "layout": None, "focus": True, "panes": [{"cwd": str(self.project.root), "name": "", "focus": True}]},
                {"name": "apoio", "layout": None, "focus": False, "panes": [{"cwd": str(self.project.root), "name": "", "focus": True}]},
            ],
        }
        snapshot_file = snapshot_path(self.paths.state, self.project.slug)
        write_atomic(snapshot_file, snapshot)
        entry = {
            "slug": self.project.slug,
            "session": self.project.session,
            "snapshot": str(snapshot_file),
            "snapshot_timestamp": snapshot["timestamp"],
            "topology_fingerprint": _topology_fingerprint(snapshot),
            "focus": {"window": 0, "pane": 0},
            "state": "active",
            "cwd_translation": {"translated": False, "count": 0},
            "visuals": [visual_document(self.project.slug, visual_id, visual_id, self.project.root, order, self.project.session, window) for order, (visual_id, window) in enumerate(visuals)],
        }
        write_atomic(workspace_path(self.paths.state), workspace_document("default", [entry], []))

        class RealTmuxService:
            def __init__(service_self, outer):
                service_self.paths = outer.paths
                service_self.tmux = outer.tmux

            def status(service_self, slug):
                return {"slug": slug, "name": "Integration", "active": True, "owned": True}

            def open(service_self, slug, **kwargs):
                return "existing", []

            def visual_claimed(service_self, slug, visual_id, window=None):
                return service_self.tmux.visual_claimed(self.project, self.token, visual_id, window)

            def prepare_visual(service_self, slug, visual_id, window=None):
                return service_self.tmux.ensure_visual_group(self.project, self.token, visual_id, window)[1]

        workspace = WorkspaceService(RealTmuxService(self))
        first = workspace.restore(dry_run=True, visual="terminator")
        self.assertEqual([item["action"] for item in first[0]["visuals"]], ["confirmed", "would-open", "would-open", "would-open"])

        opened = []

        def open_mock(paths, slug, name, root, **kwargs):
            opened.append(kwargs["visual_id"])
            self.attach(self.tmux.visual_group_name(self.project, kwargs["visual_id"]))
            return True

        with patch("work_orchestrator.gui.open_terminator", side_effect=open_mock):
            restored = workspace.restore(visual="terminator")
        self.assertEqual(opened, ["dev-2", "apoio-1", "apoio-2"])
        self.assertEqual([item["action"] for item in restored[0]["visuals"]], ["confirmed", "opened", "opened", "opened"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(not self.tmux.client_targets(group) for group in groups):
            time.sleep(0.05)
        second = workspace.restore(dry_run=True, visual="terminator")
        self.assertEqual([item["action"] for item in second[0]["visuals"]], ["confirmed", "confirmed", "confirmed", "confirmed"])

    def test_stop_group_preflight_rejects_clients_and_stale_parent(self):
        group_id, group = self.tmux.ensure_visual_group(self.project, self.token, "dev-1", "dev")
        self.attach(group)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.tmux.client_targets(group):
            time.sleep(0.05)
        with self.assertRaises(WorkError):
            self.tmux.prepare_stop_visual_groups(self.project, self.token, self.parent_id)
        self.assertTrue(self.tmux.exists(group))
        self.assertTrue(self.tmux.exists(self.project.session))
        self.clients[0].terminate()
        self.clients[0].wait(timeout=3)
        self.tmux.run("set-option", "-t", group_id, "@work-orchestrator-parent-session-id", "$999")
        with self.assertRaises(WorkError):
            self.tmux.prepare_stop_visual_groups(self.project, self.token, self.parent_id)
        self.assertTrue(self.tmux.exists(group))
        self.assertTrue(self.tmux.exists(self.project.session))


if __name__ == "__main__":
    unittest.main()
