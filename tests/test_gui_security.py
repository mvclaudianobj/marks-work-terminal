import os
import pwd
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.errors import WorkError
from work_orchestrator.gui import _root_launch_identity, _terminal_environment, _work_executable, open_terminator
from work_orchestrator.paths import Paths


class GuiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")
        self.paths.ensure()
        self.root = base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_work_executable_ignores_argv_and_home(self):
        with patch.dict(os.environ, {"HOME": "/root"}, clear=False), patch("sys.argv", ["/root/.local/bin/work-gui-workspace"]):
            self.assertEqual(_work_executable(), Path(__file__).resolve().parents[1] / "bin" / "work")

    def test_work_executable_rejects_symlink_override(self):
        link = Path(self.temp.name) / "work"
        link.symlink_to("/bin/true")
        with patch.dict(os.environ, {"WORK_EXECUTABLE": str(link)}, clear=False), self.assertRaises(WorkError):
            _work_executable()

    def test_terminal_environment_removes_work_and_python_overrides(self):
        environment = {"HOME": "/root", "WORK_EXECUTABLE": "/root/work", "WORK_BAD": "1", "PYTHONPATH": "/root"}
        with patch.dict(os.environ, environment, clear=True), patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            child = _terminal_environment(self.paths)
        self.assertNotIn("WORK_EXECUTABLE", child)
        self.assertNotIn("WORK_BAD", child)
        self.assertNotIn("PYTHONPATH", child)
        self.assertNotEqual(child["XDG_RUNTIME_DIR"], "/root")

    def test_root_like_requires_explicit_allowlist(self):
        with patch("work_orchestrator.gui.os.geteuid", return_value=0), patch("work_orchestrator.gui.pwd.getpwnam", return_value=pwd.getpwnam("root")), patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/0"}, clear=True), self.assertRaises(WorkError):
            _root_launch_identity()

    def test_root_like_allowlist_resolves_marcos_runtime(self):
        target = pwd.struct_passwd(("marcos", "x", 4242, 4242, "", "/home/example", "/bin/sh"))
        details = type("Stat", (), {"st_mode": stat.S_IFDIR | 0o700, "st_uid": 4242})()
        environment = {"XDG_RUNTIME_DIR": "/run/user/4242", "WORK_ALLOW_ROOT_LAUNCH": "marcos", "WORK_TARGET_UID": "4242"}
        with patch("work_orchestrator.gui.os.geteuid", return_value=0), patch("work_orchestrator.gui.pwd.getpwnam", return_value=target), patch("work_orchestrator.gui.Path.lstat", return_value=details), patch.dict(os.environ, environment, clear=True):
            identity = _root_launch_identity()
        self.assertEqual(identity[0].pw_name, "marcos")
        self.assertEqual(identity[1], Path("/run/user/4242"))

    def test_mock_terminator_command_uses_checkout_work_absolute_path(self):
        process = object()
        with patch("work_orchestrator.gui._root_launch_identity", return_value=None), patch("work_orchestrator.gui.subprocess.Popen", return_value=process) as popen, patch("work_orchestrator.gui.threading.Thread"):
            open_terminator(self.paths, "alpha", "Alpha", self.root, check_existing=False)
        argv = popen.call_args.kwargs["args"] if "args" in popen.call_args.kwargs else popen.call_args.args[0]
        config_path = Path(argv[argv.index("-g") + 1])
        command_text = config_path.read_text(encoding="utf-8")
        self.assertIn(str(Path(__file__).resolve().parents[1] / "bin" / "work"), command_text)
        self.assertNotIn("/root", command_text)


if __name__ == "__main__":
    unittest.main()
