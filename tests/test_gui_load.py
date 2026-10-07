import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from work_orchestrator.gui import load_main, new_main
from work_orchestrator.paths import Paths


class LoadMainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "utm7"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _item(self, slug, name, visuals):
        return {
            "slug": slug,
            "name": name,
            "root": str(self.root),
            "error": None,
            "runtime": {"legacy_fallback": {}},
            "visuals": visuals,
        }

    def test_multiple_visuals_calls_open_terminator_tabs_with_prepared_visuals(self):
        visuals = [
            {"id": "apoio-1", "kind": "tilix", "root": str(self.root), "order": 2, "attach_target": {"session": "work-utm7", "window": "apoio"}},
            {"id": "dev-1", "kind": "tilix", "root": str(self.root), "order": 0, "attach_target": {"session": "work-utm7", "window": "dev"}},
            {"id": "dev-2", "kind": "tilix", "root": str(self.root), "order": 1, "attach_target": {"session": "work-utm7", "window": "dev"}},
        ]
        item = self._item("utm7", "UTM7", visuals)
        service = MagicMock()
        service.projects.return_value = [item]
        with patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), \
             patch("work_orchestrator.gui.Service", return_value=service), \
             patch("work_orchestrator.gui.select_project", return_value="utm7"), \
             patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs, \
             patch("work_orchestrator.gui.open_terminator") as open_single:
            result = load_main()
        self.assertEqual(result, 0)
        open_single.assert_not_called()
        service.open.assert_called_once_with("utm7", attach=False)
        self.assertEqual(
            [call.args[1] for call in service.prepare_visual.call_args_list],
            ["dev-1", "dev-2", "apoio-1"],
        )
        self.assertEqual(
            [mock_call for mock_call in service.mock_calls if mock_call[0] in {"open", "prepare_visual"}],
            [
                call.open("utm7", attach=False),
                call.prepare_visual("utm7", "dev-1", "dev"),
                call.prepare_visual("utm7", "dev-2", "dev"),
                call.prepare_visual("utm7", "apoio-1", "apoio"),
            ],
        )
        open_tabs.assert_called_once()
        args, kwargs = open_tabs.call_args
        self.assertEqual(args[0], self.paths)
        self.assertEqual(args[1], "utm7")
        self.assertEqual(args[2], "UTM7")
        tabs = args[3]
        self.assertEqual([tab["id"] for tab in tabs], ["dev-1", "dev-2", "apoio-1"])
        self.assertEqual([tab["window"] for tab in tabs], ["dev", "dev", "apoio"])
        self.assertEqual([tab["title"] for tab in tabs], ["UTM7 — dev-1", "UTM7 — dev-2", "UTM7 — apoio-1"])
        self.assertTrue(all(tab["command"] == "" for tab in tabs))
        self.assertTrue(all(tab["root"] == str(self.root) for tab in tabs))
        self.assertEqual(kwargs.get("workspace"), "default")

    def test_single_visual_keeps_legacy_open_terminator_call(self):
        visuals = [
            {"id": "main", "kind": "tilix", "root": str(self.root), "order": 0, "attach_target": {"session": "work-alpha"}},
        ]
        item = self._item("alpha", "Alpha", visuals)
        service = MagicMock()
        service.projects.return_value = [item]
        with patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), \
             patch("work_orchestrator.gui.Service", return_value=service), \
             patch("work_orchestrator.gui.select_project", return_value="alpha"), \
             patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs, \
             patch("work_orchestrator.gui.open_terminator") as open_single:
            result = load_main()
        self.assertEqual(result, 0)
        open_tabs.assert_not_called()
        service.open.assert_not_called()
        service.prepare_visual.assert_not_called()
        open_single.assert_called_once_with(self.paths, "alpha", "Alpha", Path(str(self.root)))

    def test_no_visuals_declared_keeps_legacy_open_terminator_call(self):
        item = self._item("legacy", "Legacy", [])
        service = MagicMock()
        service.projects.return_value = [item]
        with patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), \
             patch("work_orchestrator.gui.Service", return_value=service), \
             patch("work_orchestrator.gui.select_project", return_value="legacy"), \
             patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs, \
             patch("work_orchestrator.gui.open_terminator") as open_single:
            result = load_main()
        self.assertEqual(result, 0)
        open_tabs.assert_not_called()
        service.open.assert_not_called()
        service.prepare_visual.assert_not_called()
        open_single.assert_called_once_with(self.paths, "legacy", "Legacy", Path(str(self.root)))

    def test_legacy_conflict_warning_preserved_before_multi_visual_open(self):
        visuals = [
            {"id": "dev-1", "kind": "tilix", "root": str(self.root), "order": 0, "attach_target": {"session": "work-utm7", "window": "dev"}},
            {"id": "dev-2", "kind": "tilix", "root": str(self.root), "order": 1, "attach_target": {"session": "work-utm7", "window": "dev"}},
        ]
        item = self._item("utm7", "UTM7", visuals)
        item["runtime"] = {"legacy_fallback": {"conflict": True}}
        service = MagicMock()
        service.projects.return_value = [item]
        with patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), \
             patch("work_orchestrator.gui.Service", return_value=service), \
             patch("work_orchestrator.gui.select_project", return_value="utm7"), \
             patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs, \
             patch("work_orchestrator.gui.open_terminator"), \
             patch("work_orchestrator.gui._run_zenity") as zenity:
            result = load_main()
        self.assertEqual(result, 0)
        open_tabs.assert_called_once()
        self.assertTrue(any("Conflito de runtime legado" in str(call.args[0]) for call in zenity.call_args_list))

    def test_multiple_visuals_restores_session_before_preparing_post_reboot_tabs(self):
        visuals = [
            {"id": "dev-1", "kind": "tilix", "root": str(self.root), "order": 0, "attach_target": {"session": "work-utm7", "window": "dev"}},
            {"id": "dev-2", "kind": "tilix", "root": str(self.root), "order": 1, "attach_target": {"session": "work-utm7", "window": "dev"}},
        ]
        item = self._item("utm7", "UTM7", visuals)
        service = MagicMock()
        service.projects.return_value = [item]
        session_active = False
        events = []

        def open_session(slug, attach):
            nonlocal session_active
            events.append(("open", slug, attach))
            session_active = True
            return "restored", []

        def prepare_visual(slug, visual_id, window):
            self.assertTrue(session_active)
            events.append(("prepare_visual", slug, visual_id, window))

        def open_tabs(*args, **kwargs):
            events.append(("open_terminator_tabs",))

        service.open.side_effect = open_session
        service.prepare_visual.side_effect = prepare_visual
        with patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), \
             patch("work_orchestrator.gui.Service", return_value=service), \
             patch("work_orchestrator.gui.select_project", return_value="utm7"), \
             patch("work_orchestrator.gui.open_terminator_tabs", side_effect=open_tabs) as open_tabs_mock, \
             patch("work_orchestrator.gui.open_terminator"):
            result = load_main()
        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                ("open", "utm7", False),
                ("prepare_visual", "utm7", "dev-1", "dev"),
                ("prepare_visual", "utm7", "dev-2", "dev"),
                ("open_terminator_tabs",),
            ],
        )
        service.open.assert_called_once_with("utm7", attach=False)
        open_tabs_mock.assert_called_once()

    def test_explicit_titles_are_used_exactly_in_order(self):
        visuals = [
            {"id": "works", "title": "Works", "kind": "tilix", "root": str(self.root), "order": 1, "attach_target": {"session": "work-utm7", "window": "apoio"}},
            {"id": "dev1-markscode", "title": "Dev1 - Markscode", "kind": "tilix", "root": str(self.root), "order": 0, "attach_target": {"session": "work-utm7", "window": "dev"}},
        ]
        item = self._item("utm7", "UTM7", visuals)
        service = MagicMock()
        service.projects.return_value = [item]
        with patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), patch("work_orchestrator.gui.Service", return_value=service), patch("work_orchestrator.gui.select_project", return_value="utm7"), patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs:
            self.assertEqual(load_main(), 0)
        self.assertEqual([tab["title"] for tab in open_tabs.call_args.args[3]], ["Dev1 - Markscode", "Works"])

    def test_new_recommended_reuses_multi_visual_flow_without_project_dialog(self):
        item = self._item("utm7", "UTM7", [
            {"id": "dev1-markscode", "title": "Dev1 - Markscode", "kind": "tilix", "root": str(self.root), "order": 0, "attach_target": {"session": "work-utm7", "window": "dev"}},
            {"id": "works", "title": "Works", "kind": "tilix", "root": str(self.root), "order": 1, "attach_target": {"session": "work-utm7", "window": "apoio"}},
        ])
        service = MagicMock()
        service.projects.return_value = [item]
        answers = [str(self.root), "UTM7", "utm7", "recommended"]
        with patch("work_orchestrator.gui._run_zenity", side_effect=answers), patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), patch("work_orchestrator.gui.Service", return_value=service), patch("work_orchestrator.gui.init_project"), patch("work_orchestrator.gui.select_project") as select, patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs, patch("work_orchestrator.gui.open_terminator") as open_single:
            self.assertEqual(new_main(), 0)
        select.assert_not_called()
        open_single.assert_not_called()
        open_tabs.assert_called_once()
        service.open.assert_called_once_with("utm7", attach=False)

    def test_new_minimal_keeps_single_visual_open(self):
        answers = [str(self.root), "UTM7", "utm7", "minimal"]
        with patch("work_orchestrator.gui._run_zenity", side_effect=answers), patch("work_orchestrator.gui.Paths.discover", return_value=self.paths), patch("work_orchestrator.gui.init_project"), patch("work_orchestrator.gui.Service") as service, patch("work_orchestrator.gui.open_terminator_tabs") as open_tabs, patch("work_orchestrator.gui.open_terminator") as open_single:
            self.assertEqual(new_main(), 0)
        service.assert_not_called()
        open_tabs.assert_not_called()
        open_single.assert_called_once_with(self.paths, "utm7", "UTM7", self.root)


if __name__ == "__main__":
    unittest.main()
