import os
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

try:
    from configobj import ConfigObj
except ImportError:
    ConfigObj = None

from work_orchestrator.errors import WorkError
from work_orchestrator.gui import build_terminator_layout, open_terminator_tabs
from work_orchestrator.paths import Paths


class TerminatorTabsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "utm7"
        self.root.mkdir()
        self.tabs = [
            {"id": "dev-1", "title": "dev-1", "root": str(self.root), "command": "", "window": "dev"},
            {"id": "dev-2", "title": "dev-2", "root": str(self.root), "command": "", "window": "dev"},
            {"id": "apoio-1", "title": "apoio-1", "root": str(self.root), "command": "", "window": "apoio"},
            {"id": "apoio-2", "title": "apoio-2", "root": str(self.root), "command": "", "window": "apoio"},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_layout_uses_real_terminator_configobj_format_with_notebook_for_multiple_tabs(self):
        content = build_terminator_layout(self.tabs, "utm7")
        self.assertIn("[layouts]", content)
        self.assertIn("[[utm7]]", content)
        self.assertIn("[[[window]]]", content)
        self.assertIn("[[[notebook]]]", content)
        self.assertIn("[[[term0]]]", content)
        self.assertIn("[[[term1]]]", content)
        self.assertIn("[[[term2]]]", content)
        self.assertIn("[[[term3]]]", content)
        self.assertIn("type = Terminal", content)
        self.assertIn("type = Notebook", content)
        self.assertIn("type = Window", content)
        for tab in self.tabs:
            self.assertIn(f'"""{tab["root"]}"""', content)
            self.assertIn(f'"""{tab["title"]}"""', content)
        self.assertIn("profile = default", content)
        self.assertIn('parent = notebook', content)

    @unittest.skipIf(ConfigObj is None, "ConfigObj não está instalado")
    def test_layout_labels_parse_as_complete_configobj_list(self):
        titles = ["Dev1, principal", 'Dev2 "duplo"', "Apoio 'simples'", "Hash # literal", "Barra \\ literal"]
        tabs = [{**tab, "title": title} for tab, title in zip(self.tabs, titles[:4])]
        tabs.append({**self.tabs[0], "id": "barra", "title": titles[4]})
        content = build_terminator_layout(tabs, "utm7")
        labels = ConfigObj(StringIO(content), encoding="utf-8")["layouts"]["utm7"]["notebook"]["labels"]
        self.assertIsInstance(labels, list)
        self.assertEqual(labels, titles)

    @unittest.skipIf(ConfigObj is None, "ConfigObj não está instalado")
    def test_multi_tab_layout_parses_window_title(self):
        content = build_terminator_layout(self.tabs, "utm7", window_title="Projeto: UTM7")
        window = ConfigObj(StringIO(content), encoding="utf-8")["layouts"]["utm7"]["window"]
        self.assertEqual(window["type"], "Window")
        self.assertEqual(window["title"], "Projeto: UTM7")

    @unittest.skipIf(ConfigObj is None, "ConfigObj não está instalado")
    def test_single_tab_layout_parses_window_title(self):
        content = build_terminator_layout(self.tabs[:1], "utm7", window_title="Projeto: UTM7")
        layout = ConfigObj(StringIO(content), encoding="utf-8")["layouts"]["utm7"]
        self.assertEqual(layout["window"]["title"], "Projeto: UTM7")
        self.assertEqual(layout["term0"]["title"], "dev-1")

    def test_layout_rejects_configobj_list_item_with_both_quote_types(self):
        tabs = [{**tab, "title": "Aspas 'simples' e \"duplas\""} for tab in self.tabs]
        with self.assertRaisesRegex(WorkError, "não pode ser serializado"):
            build_terminator_layout(tabs, "utm7")

    def test_single_tab_omits_notebook_and_attaches_terminal_to_window(self):
        single = [self.tabs[0]]
        content = build_terminator_layout(single, "utm7")
        self.assertNotIn("Notebook", content)
        self.assertNotIn("[[[notebook]]]", content)
        self.assertIn("[[[term0]]]", content)
        self.assertIn("parent = window", content)

    def test_mock_argv_private_files_and_no_secret(self):
        process = type("Process", (), {"returncode": 0, "communicate": lambda self: ("", "")})()
        with patch("work_orchestrator.gui._root_launch_identity", return_value=None), \
             patch("work_orchestrator.gui._work_executable", return_value=Path("/checkout/bin/work")), \
             patch("work_orchestrator.gui.subprocess.Popen", return_value=process) as popen, \
             patch("work_orchestrator.gui.threading.Thread") as thread:
            self.assertTrue(open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs, workspace="default"))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/terminator")
        self.assertIn("-u", argv)
        config_path = Path(argv[argv.index("-g") + 1])
        self.assertEqual(argv[argv.index("-l") + 1], "utm7")
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("[layouts]", text)
        self.assertIn("[[[notebook]]]", text)
        if ConfigObj is not None:
            window = ConfigObj(StringIO(text), encoding="utf-8")["layouts"]["utm7"]["window"]
            self.assertEqual(window["title"], "Projeto: UTM7")
        self.assertNotIn("token", text.lower())
        self.assertNotIn("secret", text.lower())
        self.assertNotIn("environment", text.lower())
        self.assertNotIn("pid", text.lower())
        self.assertNotIn("pty", text.lower())
        thread_kwargs = thread.call_args.kwargs
        marker = next(self.paths.runtime.joinpath("visual").glob("*.tabs.claim"))
        thread_kwargs["target"](self.paths, process, marker, *thread_kwargs["args"][3:])
        self.assertFalse(config_path.exists())

    def test_preparation_failure_cleans_marker(self):
        with patch("work_orchestrator.gui.build_terminator_layout", side_effect=RuntimeError("invalid layout")):
            with self.assertRaises(RuntimeError):
                open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs)
        self.assertEqual(list(self.paths.runtime.joinpath("visual").glob("*.tabs.claim")), [])
        self.assertEqual(list(self.paths.runtime.joinpath("terminator").glob("*.conf")), [])

    def test_config_write_failure_cleans_own_files_only(self):
        unrelated = self.paths.runtime / "terminator" / "third-party.conf"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("external", encoding="utf-8")
        original_fsync = os.fsync
        calls = {"count": 0}

        def flaky_fsync(fd):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("disk full")
            return original_fsync(fd)

        with patch("work_orchestrator.gui.os.fsync", side_effect=flaky_fsync):
            with self.assertRaises(OSError):
                open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs)
        self.assertEqual(list(self.paths.runtime.joinpath("visual").glob("*.tabs.claim")), [])
        self.assertEqual(list(self.paths.runtime.joinpath("terminator").glob("layout-*.conf")), [])
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "external")

    def test_marker_write_failure_cleans_marker(self):
        with patch("work_orchestrator.gui.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs)
        self.assertEqual(list(self.paths.runtime.joinpath("visual").glob("*.tabs.claim")), [])

    def test_path_empty_does_not_change_safe_command(self):
        with patch.dict(os.environ, {"PATH": ""}, clear=False), \
             patch("work_orchestrator.gui._root_launch_identity", return_value=None), \
             patch("work_orchestrator.gui.subprocess.Popen") as popen, \
             patch("work_orchestrator.gui.threading.Thread"):
            popen.return_value = object()
            open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs)
        argv = popen.call_args.args[0]
        config_path = Path(argv[argv.index("-g") + 1])
        content = config_path.read_text(encoding="utf-8")
        self.assertIn(
            str(Path(__file__).resolve().parents[1] / "bin" / "work"),
            content,
        )

    def test_second_grouped_open_is_idempotent(self):
        with patch("work_orchestrator.gui._root_launch_identity", return_value=None), \
             patch("work_orchestrator.gui.subprocess.Popen", return_value=object()) as popen, \
             patch("work_orchestrator.gui.threading.Thread"):
            self.assertTrue(open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs))
            self.assertFalse(open_terminator_tabs(self.paths, "utm7", "UTM7", self.tabs))
        self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
