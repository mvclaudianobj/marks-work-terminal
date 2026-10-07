import json
import os
import shutil
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from work_orchestrator.cli import init_project
from work_orchestrator.config import parse_project
from work_orchestrator.errors import WorkError
from work_orchestrator.paths import Paths
from work_orchestrator.store import snapshot_path, write_atomic
from work_orchestrator.tmux import Tmux
from work_orchestrator.visuals import DEFAULT_ENGINES, DEFAULT_VISUALS, LEGACY_VISUALS, default_visuals_toml, migrate_default_visuals, transform_legacy_snapshot


class VisualDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_config_parses_explicit_title_and_absent_title_fallback_state(self):
        explicit = self._project('title = "Dev1 - Markscode"\n')
        fallback = self._project("")
        self.assertEqual(parse_project(self.paths, "alpha", explicit).visuals[0].title, "Dev1 - Markscode")
        self.assertIsNone(parse_project(self.paths, "alpha", fallback).visuals[0].title)
        with self.assertRaises(WorkError):
            parse_project(self.paths, "alpha", self._project('title = "bad\\u0001title"\n'))

    def test_init_recommended_has_six_independent_windows_and_minimal_is_unchanged(self):
        recommended = init_project(self.paths, "alpha", self.root, "Alpha", "recommended")
        project = parse_project(self.paths, "alpha", recommended.read_bytes())
        self.assertEqual([(item.id, item.title, item.window) for item in project.visuals], list(DEFAULT_VISUALS))
        self.assertEqual([window.name for window in project.windows], [item[0] for item in DEFAULT_VISUALS])
        self.assertEqual([len(window.panes) for window in project.windows], [1] * 6)
        self.assertEqual([window.panes[0].name for window in project.windows], ["shell"] * 6)
        self.assertEqual([window.focus for window in project.windows], [True, False, False, False, False, False])
        minimal = init_project(self.paths, "beta", self.root, "Beta", "minimal")
        self.assertNotIn("[[visuals]]", minimal.read_text())
        self.assertEqual([window.name for window in parse_project(self.paths, "beta", minimal.read_bytes()).windows], ["main"])

    def test_migration_dry_run_never_writes_and_requires_yes(self):
        path = self._write_legacy("alpha")
        original = path.read_bytes()
        result = migrate_default_visuals(self.paths, dry_run=True)
        self.assertEqual(result[0]["action"], "would-migrate")
        self.assertIn("duas janelas", result[0]["reason"])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.paths.state / "backups").exists())
        with self.assertRaises(WorkError):
            migrate_default_visuals(self.paths)

    def test_migration_backups_preserves_sections_and_is_idempotent(self):
        path = self._write_legacy("alpha")
        original = path.read_bytes()
        before = tomllib.loads(original.decode())
        result = migrate_default_visuals(self.paths, yes=True)
        backup = Path(result[0]["backup"])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(backup.read_bytes(), original)
        after = tomllib.loads(path.read_text())
        self.assertEqual(after["project"], before["project"])
        self.assertEqual(after["monitor"], before["monitor"])
        self.assertEqual([item["name"] for item in after["windows"]], [item[0] for item in DEFAULT_VISUALS])
        self.assertEqual([(item["id"], item["title"], item["attach_target"]["window"]) for item in after["visuals"]], list(DEFAULT_VISUALS))
        migrated = path.read_bytes()
        second = migrate_default_visuals(self.paths, yes=True)
        self.assertEqual(second[0]["action"], "unchanged")
        self.assertEqual(path.read_bytes(), migrated)

    def test_migration_skips_custom_topology(self):
        path = self._write_legacy("alpha")
        path.write_text(path.read_text().replace('name = "shell"\nfocus = false', 'name = "terminal"\nfocus = false', 1))
        path.chmod(0o600)
        original = path.read_bytes()
        result = migrate_default_visuals(self.paths, project="alpha", dry_run=True)
        self.assertEqual(result[0]["action"], "skipped")
        self.assertIn("customizada", result[0]["reason"])
        self.assertEqual(path.read_bytes(), original)

    def test_migration_adds_only_engine_metadata_to_exact_six_window_layout(self):
        path = self.paths.config / "alpha.toml"
        windows = []
        for index, (name, _, _) in enumerate(DEFAULT_VISUALS):
            windows.extend(("[[windows]]", f'name = "{name}"', f'focus = {str(index == 0).lower()}', "", "[[windows.panes]]", 'name = "shell"', "focus = true", ""))
        payload = (f'[project]\nroot = "{self.root}"\n\n' + default_visuals_toml("alpha") + "\n" + "\n".join(windows)).encode()
        path.write_bytes(payload)
        path.chmod(0o600)
        before = tomllib.loads(payload.decode())
        result = migrate_default_visuals(self.paths, project="alpha", yes=True)
        after = tomllib.loads(path.read_text())
        self.assertEqual(result[0]["action"], "migrated")
        self.assertEqual(after["project"], before["project"])
        self.assertEqual([(item["name"], item.get("engine"), item["panes"]) for item in after["windows"]], [(item["name"], DEFAULT_ENGINES.get(item["name"]), item["panes"]) for item in before["windows"]])
        self.assertEqual(migrate_default_visuals(self.paths, project="alpha", yes=True)[0]["action"], "unchanged")

    def test_engine_metadata_migration_skips_custom_six_window_layout(self):
        path = init_project(self.paths, "alpha", self.root, "Alpha", "recommended")
        text = path.read_text().replace('engine = "markscode"\n', "", 1).replace('name = "shell"', 'name = "custom"', 1)
        path.write_text(text)
        path.chmod(0o600)
        original = path.read_bytes()
        result = migrate_default_visuals(self.paths, project="alpha", dry_run=True)
        self.assertEqual(result[0]["action"], "skipped")
        self.assertEqual(path.read_bytes(), original)

    def test_inactive_snapshot_is_migrated_metadata_only_with_backup(self):
        path = self._write_legacy("alpha")
        snapshot = self._legacy_snapshot("alpha")
        snapshot_file = snapshot_path(self.paths.state, "alpha")
        write_atomic(snapshot_file, snapshot)
        result = migrate_default_visuals(self.paths, yes=True, session_active=lambda _: False)
        migrated = json.loads(snapshot_file.read_text())
        self.assertEqual([item["name"] for item in migrated["windows"]], [item[0] for item in DEFAULT_VISUALS])
        self.assertEqual(migrated["windows"][0]["panes"][0]["cwd"], str(self.root / "dev-cwd"))
        self.assertEqual(migrated["windows"][-1]["panes"][0]["cwd"], str(self.root / "works-cwd"))
        self.assertEqual([item["panes"][0]["name"] for item in migrated["windows"]], ["shell"] * 6)
        snapshot_backup = Path(result[0]["snapshot_backup"])
        self.assertEqual(stat.S_IMODE(snapshot_backup.stat().st_mode), 0o600)
        self.assertEqual(json.loads(snapshot_backup.read_text()), snapshot)
        self.assertEqual(parse_project(self.paths, "alpha", path.read_bytes()).windows[0].name, "dev1-markscode")

    def test_active_snapshot_is_left_untouched(self):
        self._write_legacy("alpha")
        snapshot = self._legacy_snapshot("alpha")
        snapshot_file = snapshot_path(self.paths.state, "alpha")
        write_atomic(snapshot_file, snapshot)
        result = migrate_default_visuals(self.paths, yes=True, session_active=lambda _: True)
        self.assertEqual(json.loads(snapshot_file.read_text()), snapshot)
        self.assertIn("sessão ativa", result[0]["snapshot"])

    def test_snapshot_transform_skips_custom_shape(self):
        snapshot = self._legacy_snapshot("alpha")
        snapshot["windows"][0]["panes"][1]["name"] = "custom"
        migrated, reason = transform_legacy_snapshot(snapshot, "alpha", "work-alpha", self.root)
        self.assertIsNone(migrated)
        self.assertIn("customizado", reason)

    def _project(self, title: str) -> bytes:
        return (
            f'[project]\nroot = "{self.root}"\n'
            '[[visuals]]\nid = "main"\nkind = "tilix"\n'
            f'{title}order = 0\n[visuals.attach_target]\nsession = "work-alpha"\nwindow = "main"\n'
            '[[windows]]\nname = "main"\n[[windows.panes]]\nfocus = true\n'
        ).encode()

    def _write_legacy(self, slug: str) -> Path:
        lines = [
            "[project]",
            f'name = "{slug.title()}"',
            f'root = "{self.root}"',
            'command_policy = "prompt"',
            "",
            "[monitor]",
            "autosave = true",
            "debounce = 0.75",
            "interval = 5.0",
            "history = false",
            "",
        ]
        for order, (visual_id, title, window) in enumerate(LEGACY_VISUALS):
            lines.extend(("[[visuals]]", f'id = "{visual_id}"', 'kind = "tilix"', f'title = "{title}"', f"order = {order}", "", "[visuals.attach_target]", f'session = "work-{slug}"', f'window = "{window}"', ""))
        lines.extend(("[[windows]]", 'name = "dev"', 'layout = "even-horizontal"', "focus = true", "", "[[windows.panes]]", 'name = "codigo"', "focus = true", "", "[[windows.panes]]", 'name = "shell"', "focus = false", "", "[[windows]]", 'name = "apoio"', "focus = false", "", "[[windows.panes]]", 'name = "shell"', "focus = true", ""))
        path = self.paths.config / f"{slug}.toml"
        path.write_text("\n".join(lines))
        path.chmod(0o600)
        return path

    def _legacy_snapshot(self, slug: str) -> dict:
        dev_cwd = self.root / "dev-cwd"
        works_cwd = self.root / "works-cwd"
        dev_cwd.mkdir(exist_ok=True)
        works_cwd.mkdir(exist_ok=True)
        return {
            "schema": 2,
            "timestamp": "2026-10-06T00:00:00+00:00",
            "session": f"work-{slug}",
            "project": slug,
            "cwd_translation": {"translated": False, "count": 0},
            "windows": [
                {"name": "dev", "layout": "legacy", "focus": True, "panes": [{"cwd": str(dev_cwd), "name": "codigo", "focus": True}, {"cwd": str(self.root), "name": "shell", "focus": False}]},
                {"name": "apoio", "layout": "legacy", "focus": False, "panes": [{"cwd": str(works_cwd), "name": "shell", "focus": True}]},
            ],
        }


@unittest.skipUnless(shutil.which("tmux"), "tmux não disponível")
class RecommendedTmuxIntegrationTests(unittest.TestCase):
    def test_recommended_windows_have_distinct_panes_and_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = Paths(base / "config", base / "state", base / "runtime")
            paths.ensure()
            root = base / "project"
            root.mkdir()
            config = init_project(paths, "alpha", root, "Alpha", "recommended")
            project = parse_project(paths, "alpha", config.read_bytes())
            previous_socket = os.environ.get("WORK_TMUX_SOCKET")
            previous_testing = os.environ.get("WORK_ORCHESTRATOR_TESTING")
            os.environ["WORK_TMUX_SOCKET"] = str(paths.runtime / "recommended.sock")
            os.environ["WORK_ORCHESTRATOR_TESTING"] = "1"
            tmux = Tmux(paths)
            try:
                token = tmux.ownership_token(project)
                tmux.create_from_project(project, token, False)
                result = tmux.run("list-panes", "-s", "-t", f"={project.session}", "-F", "#{window_name}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}")
                rows = [line.split("\t") for line in result.stdout.splitlines()]
                self.assertEqual([row[0] for row in rows], [item[0] for item in DEFAULT_VISUALS])
                self.assertEqual(len({row[1] for row in rows}), 6)
                self.assertEqual(len({row[2] for row in rows}), 6)
                self.assertEqual({row[3] for row in rows}, {str(root)})
            finally:
                tmux.run("kill-server", check=False)
                if previous_socket is None:
                    os.environ.pop("WORK_TMUX_SOCKET", None)
                else:
                    os.environ["WORK_TMUX_SOCKET"] = previous_socket
                if previous_testing is None:
                    os.environ.pop("WORK_ORCHESTRATOR_TESTING", None)
                else:
                    os.environ["WORK_ORCHESTRATOR_TESTING"] = previous_testing


if __name__ == "__main__":
    unittest.main()
