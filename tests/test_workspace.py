import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.config import parse_project
from work_orchestrator.errors import WorkError
from work_orchestrator.gui import open_terminator
from work_orchestrator.paths import Paths
from work_orchestrator.store import read_snapshot, snapshot_path, write_atomic
from work_orchestrator.workspace import WorkspaceService, read_workspace, workspace_path


class FakeTmux:
    def __init__(self, paths):
        self.socket = paths.socket
        self.attached = set()
        self.canonical = []
        self.external = []
        self.visual_attached = set()

    def client_targets(self, session):
        return ["main"] if session in self.attached else []

    def has_attached_client(self, session, window=None):
        targets = self.client_targets(session)
        return bool(targets) if window is None else window in targets

    def sessions(self):
        return self.canonical

    def external_sessions(self, socket):
        return self.external


class FakeService:
    def __init__(self, paths, projects):
        self.paths = paths
        self.tmux = FakeTmux(paths)
        self.values = projects
        self.saved = []
        self.opened = []
        self.prepared = []

    def projects(self):
        return [dict(item) for item in self.values]

    def save(self, slug):
        self.saved.append(slug)
        return snapshot_path(self.paths.state, slug)

    def status(self, slug):
        return next(dict(item) for item in self.values if item["slug"] == slug)

    def open(self, slug, **kwargs):
        self.opened.append(slug)
        return "restored", []

    def visual_attached(self, slug, visual_id, window=None):
        return (slug, visual_id, window) in self.tmux.visual_attached

    def visual_claimed(self, slug, visual_id, window=None):
        return self.visual_attached(slug, visual_id, window)

    def prepare_visual(self, slug, visual_id, window=None):
        self.prepared.append((slug, visual_id, window))
        return f"group-{slug}-{visual_id}"

    def workspace_diagnostic(self):
        configured = {item["session"] for item in self.values}
        unmanaged = [
            {"session": item["name"], "state": "unmanaged" if item["name"] not in configured else "conflict"}
            for item in self.tmux.canonical
            if item["name"].startswith("work-") and (item["name"] not in configured or not item["owned"])
        ]
        return {
            "unmanaged": unmanaged,
            "external_unmanaged": [{"session": item, "state": "external_unmanaged"} for item in self.tmux.external],
        }


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.roots = [base / "alpha", base / "beta", base / "idle"]
        for root in self.roots:
            root.mkdir()
        self.projects = [
            self.project("alpha", self.roots[0], True, True),
            self.project("beta", self.roots[1], True, True),
            self.project("idle", self.roots[2], False, False),
        ]
        for project in self.projects[:2]:
            self.write_snapshot(project["slug"], project["root"])
        self.service = FakeService(self.paths, self.projects)
        self.workspace = WorkspaceService(self.service)
        self.root_identity = patch("work_orchestrator.gui._root_launch_identity", return_value=None)
        self.root_identity.start()

    def tearDown(self):
        self.root_identity.stop()
        self.temp.cleanup()

    def project(self, slug, root, active, owned):
        return {
            "slug": slug,
            "name": slug.title(),
            "root": str(root),
            "session": f"work-{slug}",
            "active": active,
            "owned": owned,
            "attached": False,
            "state": "active" if active and owned else "inactive",
            "degraded": False,
        }

    def write_snapshot(self, slug, root):
        write_atomic(snapshot_path(self.paths.state, slug), {
            "schema": 2,
            "timestamp": "2026-10-04T00:00:00+00:00",
            "session": f"work-{slug}",
            "project": slug,
            "windows": [{"name": "main", "layout": None, "focus": True, "panes": [{"cwd": str(root), "name": "", "focus": True}]}],
        })

    def test_save_enumerates_active_owned_and_inactive(self):
        path = self.workspace.save()
        value = read_workspace(path)
        self.assertEqual([item["slug"] for item in value["projects"]], ["alpha", "beta"])
        self.assertEqual([item["slug"] for item in value["inactive_projects"]], ["idle"])
        self.assertEqual(self.service.saved, ["alpha", "beta"])

    def test_visual_is_declarative_ordered_and_has_no_sensitive_fields(self):
        value = read_workspace(self.workspace.save())
        self.assertEqual([item["visuals"][0]["order"] for item in value["projects"]], [0, 1])
        self.assertEqual(set(value["projects"][0]["visuals"][0]), {"id", "kind", "order", "title", "root", "attach_target"})
        payload = json.dumps(value).lower()
        for forbidden in ("pid", "pty", "argv", "token", "content", "capture-pane"):
            self.assertNotIn(forbidden, payload)

    def test_workspace_and_terminator_ignore_pane_title(self):
        secret = "workspace-pane-secret"
        path = snapshot_path(self.paths.state, "alpha")
        snapshot = read_snapshot(path)
        snapshot["windows"][0]["panes"][0]["name"] = f"access_token={secret}"
        write_atomic(path, snapshot)
        workspace = read_workspace(self.workspace.save(refresh_snapshots=False))
        self.assertNotIn(secret, json.dumps(workspace))
        with patch("work_orchestrator.gui._work_executable", return_value=Path("/bin/true")), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch("work_orchestrator.gui.threading.Thread"):
            popen.return_value = object()
            self.workspace.restore(visual="terminator")
        argv_values = [call.args[0] for call in popen.call_args_list]
        config_paths = [Path(argv[argv.index("-g") + 1]) for argv in argv_values]
        contents = [config_path.read_text(encoding="utf-8") for config_path in config_paths]
        self.assertTrue(any("Work — Alpha (alpha)" in content for content in contents))
        self.assertNotIn(secret, "\n".join(contents))

    def test_workspace_save_preserves_explicit_visual_title(self):
        self.service.values[0]["visuals"] = [
            {
                "id": "dev1-markscode",
                "kind": "tilix",
                "title": "Dev1 - Markscode",
                "root": str(self.roots[0]),
                "order": 0,
                "attach_target": {"session": "work-alpha", "window": "main"},
            },
            {
                "id": "commands",
                "kind": "tilix",
                "title": "Commands",
                "root": str(self.roots[0]),
                "order": 1,
                "attach_target": {"session": "work-alpha", "window": "main"},
            },
        ]
        workspace = read_workspace(self.workspace.save(refresh_snapshots=False))
        self.assertEqual([visual["title"] for visual in workspace["projects"][0]["visuals"]], ["Dev1 - Markscode", "Commands"])

    def test_restore_individual_visual_uses_explicit_title(self):
        self.projects = [self.project("alpha", self.roots[0], True, True)]
        self.projects[0]["visuals"] = [{
            "id": "dev1-markscode",
            "kind": "tilix",
            "title": "Dev1 - Markscode",
            "root": str(self.roots[0]),
            "order": 0,
            "attach_target": {"session": "work-alpha", "window": "main"},
        }]
        self.service.values = self.projects
        self.workspace.save(refresh_snapshots=False)
        with patch("work_orchestrator.gui.open_terminator", return_value=True) as open_visual:
            self.workspace.restore(visual="terminator")
        self.assertEqual(open_visual.call_args.kwargs["title"], "Dev1 - Markscode")

    def test_restore_terminator_tabs_uses_explicit_titles_in_order(self):
        self.projects = [self.project("alpha", self.roots[0], True, True)]
        self.projects[0]["visuals"] = [
            {
                "id": "commands",
                "kind": "tilix",
                "title": "Commands",
                "root": str(self.roots[0]),
                "order": 1,
                "attach_target": {"session": "work-alpha", "window": "main"},
            },
            {
                "id": "dev1-markscode",
                "kind": "tilix",
                "title": "Dev1 - Markscode",
                "root": str(self.roots[0]),
                "order": 0,
                "attach_target": {"session": "work-alpha", "window": "main"},
            },
        ]
        self.service.values = self.projects
        self.workspace.save(refresh_snapshots=False)
        with patch("work_orchestrator.gui.open_terminator_tabs", return_value=True) as open_tabs:
            self.workspace.restore(visual="terminator-tabs")
        tabs = open_tabs.call_args.args[3]
        self.assertEqual([tab["title"] for tab in tabs], ["Dev1 - Markscode", "Commands"])

    def test_restore_dry_run_lists_order_without_mutation(self):
        self.workspace.save()
        result = self.workspace.restore(dry_run=True, visual="terminator")
        self.assertEqual([item["slug"] for item in result], ["alpha", "beta"])
        self.assertEqual([item["action"] for item in result], ["would-confirm", "would-confirm"])
        self.assertEqual([item["visuals"][0]["action"] for item in result], ["would-open", "would-open"])
        self.assertEqual(self.service.opened, [])

    def test_restore_opens_one_visual_per_project_and_deduplicates(self):
        self.workspace.save()
        self.service.tmux.visual_attached.add(("beta", "main", None))
        dry_run = self.workspace.restore(dry_run=True, visual="terminator")
        self.assertEqual([item["visuals"][0]["action"] for item in dry_run], ["would-open", "confirmed"])
        with patch("work_orchestrator.gui._work_executable", return_value=Path("/bin/true")), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch("work_orchestrator.gui.threading.Thread"):
            popen.return_value = object()
            result = self.workspace.restore(visual="terminator")
        self.assertEqual([item["visuals"][0]["action"] for item in result], ["opened", "confirmed"])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.service.prepared, [("alpha", "main", None)])

    def test_restore_project_filter_is_exact_and_isolates_other_projects(self):
        self.workspace.save()
        self.service.tmux.visual_attached.add(("alpha", "main", None))
        result = self.workspace.restore(dry_run=True, visual="terminator", project="alpha")
        self.assertEqual([item["slug"] for item in result], ["alpha"])
        self.assertEqual(result[0]["visuals"][0]["action"], "confirmed")
        self.assertEqual(self.service.opened, [])
        self.assertEqual(self.service.prepared, [])

    def test_restore_project_filter_rejects_ambiguous_or_missing_project_without_effects(self):
        self.workspace.save()
        for project in ("../alpha", "alpha*", "alpha,beta", "missing"):
            with self.subTest(project=project), self.assertRaises(WorkError):
                self.workspace.restore(dry_run=True, visual="terminator", project=project)
        self.assertEqual(self.service.opened, [])
        self.assertEqual(self.service.prepared, [])

    def test_unmanaged_and_external_are_diagnostic_only(self):
        self.service.tmux.canonical = [{"name": "work-orphan", "owned": False}, {"name": "work-alpha", "owned": False}]
        self.service.tmux.external = ["work-external"]
        self.workspace.save()
        status = self.workspace.status()
        self.assertEqual([item["state"] for item in status["unmanaged"]], ["unmanaged", "conflict"])
        self.assertEqual(status["external_unmanaged"][0]["state"], "external_unmanaged")
        stored = read_workspace(workspace_path(self.paths.state))
        self.assertNotIn("work-orphan", json.dumps(stored))
        self.assertNotIn("work-external", json.dumps(stored))

    def test_degraded_preserves_previous_snapshot(self):
        previous = read_snapshot(snapshot_path(self.paths.state, "alpha"))
        self.projects[0]["degraded"] = True
        self.projects[0]["state"] = "degraded"
        self.projects[0]["degraded_reason"] = "cwd divergiu"
        self.workspace.save()
        self.assertEqual(read_snapshot(snapshot_path(self.paths.state, "alpha")), previous)
        value = read_workspace(workspace_path(self.paths.state))
        self.assertTrue(value["projects"][0]["degraded"])

    def test_workspace_reports_translation_without_degrading(self):
        self.projects[0]["cwd_translation"] = {"translated": True, "count": 1}
        value = read_workspace(self.workspace.save(refresh_snapshots=False))
        entry = value["projects"][0]
        self.assertEqual(entry["state"], "active")
        self.assertNotIn("degraded", entry)
        self.assertEqual(entry["cwd_translation"], {"translated": True, "count": 1})

    def utm7_payload(self):
        return (
            f'[project]\nname = "UTM7"\nroot = "{self.roots[0]}"\nsession = "work-utm7"\n'
            '[[visuals]]\nid = "dev-1"\nkind = "tilix"\norder = 0\n[visuals.attach_target]\nsession = "work-utm7"\nwindow = "dev"\n'
            '[[visuals]]\nid = "dev-2"\nkind = "tilix"\norder = 1\n[visuals.attach_target]\nsession = "work-utm7"\nwindow = "dev"\n'
            '[[visuals]]\nid = "apoio-1"\nkind = "tilix"\norder = 2\n[visuals.attach_target]\nsession = "work-utm7"\nwindow = "apoio"\n'
            '[[visuals]]\nid = "apoio-2"\nkind = "tilix"\norder = 3\n[visuals.attach_target]\nsession = "work-utm7"\nwindow = "apoio"\n'
            '[[windows]]\nname = "dev"\nfocus = true\n[[windows.panes]]\nfocus = true\n'
            '[[windows]]\nname = "apoio"\nfocus = false\n[[windows.panes]]\nfocus = true\n'
        ).encode()

    def test_config_supports_four_visuals_two_plus_two(self):
        project = parse_project(self.paths, "utm7", self.utm7_payload())
        self.assertEqual([item.id for item in project.visuals], ["dev-1", "dev-2", "apoio-1", "apoio-2"])
        self.assertEqual([item.window for item in project.visuals], ["dev", "dev", "apoio", "apoio"])

    def test_config_rejects_invalid_visual_fields(self):
        replacements = (
            (b'id = "dev-1"', b'id = "../dev"'),
            (b'order = 1', b'order = 0'),
            (b'window = "apoio"', b'window = "missing"'),
            (f'root = "{self.roots[0]}"'.encode(), b'root = "/missing/visual-root"'),
        )
        for old, new in replacements:
            with self.subTest(new=new), self.assertRaises(WorkError):
                parse_project(self.paths, "utm7", self.utm7_payload().replace(old, new, 1))

    def test_schema_one_and_two_migrate_to_three(self):
        for schema in (1, 2):
            path = workspace_path(self.paths.state, f"legacy-{schema}")
            source = {
                "schema": schema,
                "workspace": f"legacy-{schema}",
                "timestamp": "2026-10-04T00:00:00+00:00",
                "projects": [{
                    "slug": "alpha",
                    "session": "work-alpha",
                    "visual": {"kind": "tilix", "order": 0, "title": "Work — Alpha (alpha)", "root": str(self.roots[0]), "slug": "alpha"},
                }],
                "inactive_projects": [],
            }
            write_atomic(path, source)
            migrated = read_workspace(path)
            self.assertEqual(migrated["schema"], 3)
            self.assertEqual(migrated["projects"][0]["visuals"][0]["id"], "main")
            self.assertEqual(json.loads(path.read_text())["schema"], schema)

    def test_four_visual_restore_opens_only_missing_clients(self):
        self.projects = [self.project("alpha", self.roots[0], True, True)]
        self.projects[0]["visuals"] = [
            {"id": visual_id, "kind": "tilix", "root": str(self.roots[0]), "order": order, "attach_target": {"session": "work-alpha", "window": window}}
            for order, (visual_id, window) in enumerate((("dev-1", "dev"), ("dev-2", "dev"), ("apoio-1", "apoio"), ("apoio-2", "apoio")))
        ]
        self.service.values = self.projects
        self.workspace.save(refresh_snapshots=False)
        self.service.tmux.visual_attached.update({("alpha", "dev-1", "dev"), ("alpha", "apoio-1", "apoio")})
        with patch("work_orchestrator.gui._work_executable", return_value=Path("/bin/true")), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch("work_orchestrator.gui.threading.Thread"):
            popen.return_value = object()
            result = self.workspace.restore(visual="terminator")
        self.assertEqual([item["action"] for item in result[0]["visuals"]], ["confirmed", "opened", "confirmed", "opened"])
        self.assertEqual(popen.call_count, 2)
        config_paths = [Path(call.args[0][call.args[0].index("-g") + 1]) for call in popen.call_args_list]
        commands = [config_path.read_text(encoding="utf-8") for config_path in config_paths]
        self.assertIn("/bin/true open alpha --visual-id dev-2 --window dev", commands[0])
        self.assertIn("/bin/true open alpha --visual-id apoio-2 --window apoio", commands[1])

    def test_autosave_preserves_four_workspace_override_visuals(self):
        self.projects = [self.project("alpha", self.roots[0], True, True)]
        self.service.values = self.projects
        self.workspace.save(refresh_snapshots=False)
        stored = read_workspace(workspace_path(self.paths.state))
        base = stored["projects"][0]["visuals"][0]
        stored["projects"][0]["visuals"] = [dict(base, id=f"visual-{index}", order=index) for index in range(4)]
        write_atomic(workspace_path(self.paths.state), stored)
        saved = read_workspace(self.workspace.save(refresh_snapshots=False))
        self.assertEqual(len(saved["projects"][0]["visuals"]), 4)

    def test_terminator_argv_isolates_window_and_contains_no_secret(self):
        secret = "token-do-not-leak"
        with patch("work_orchestrator.gui._work_executable", return_value=Path("/bin/true")), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch("work_orchestrator.gui.threading.Thread"):
            popen.return_value = object()
            open_terminator(self.paths, "alpha", "Alpha", self.roots[0], visual_id="dev-1", window="dev", title="Work — Alpha — dev-1")
        argv = popen.call_args.args[0]
        self.assertIn("-u", argv)
        config_path = Path(argv[argv.index("-g") + 1])
        content = config_path.read_text(encoding="utf-8")
        self.assertIn("/bin/true open alpha --visual-id dev-1 --window dev", content)
        self.assertIn('title = """Projeto: Alpha"""', content)
        self.assertIn('title = """Work — Alpha — dev-1"""', content)
        self.assertNotIn(secret, json.dumps(argv))
        self.assertNotIn(secret, content)

    def test_helper_skips_attached_without_spawning(self):
        self.service.tmux.attached.add("custom-session")
        with patch("work_orchestrator.gui.subprocess.Popen") as popen:
            opened = open_terminator(self.paths, "alpha", "Alpha", self.roots[0], tmux=self.service.tmux, session="custom-session")
        self.assertFalse(opened)
        popen.assert_not_called()

    def test_helper_claim_deduplicates_launch_race(self):
        with patch("work_orchestrator.gui._work_executable", return_value=Path("/bin/true")), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch("work_orchestrator.gui.threading.Thread"):
            popen.return_value = object()
            first = open_terminator(self.paths, "alpha", "Alpha", self.roots[0], tmux=self.service.tmux)
            second = open_terminator(self.paths, "alpha", "Alpha", self.roots[0], tmux=self.service.tmux)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(popen.call_count, 1)
