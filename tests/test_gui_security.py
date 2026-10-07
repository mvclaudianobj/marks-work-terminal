import os
import pwd
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.errors import WorkError
from work_orchestrator.gui import _root_launch_identity, _terminal_environment, _work_executable, open_terminator
from work_orchestrator.paths import Paths, validate_trusted_executable


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
        with patch("work_orchestrator.gui.os.geteuid", return_value=os.stat(__file__).st_uid), patch.dict(os.environ, {"HOME": "/root"}, clear=False), patch("sys.argv", ["/root/.local/bin/work-gui-workspace"]):
            self.assertEqual(_work_executable(), Path(__file__).resolve().parents[1] / "bin" / "work")

    def test_work_executable_rejects_symlink_override(self):
        link = Path(self.temp.name) / "work"
        link.symlink_to("/bin/true")
        with patch.dict(os.environ, {"WORK_EXECUTABLE": str(link)}, clear=False), self.assertRaises(WorkError):
            _work_executable()

    def test_checkout_executable_accepts_user_and_root_owned_mocks(self):
        checkout = Path("/safe/checkout")
        executable = checkout / "bin" / "work"
        for owner in (1000, 0):
            def details(path):
                mode = stat.S_IFREG | 0o700 if path == executable else stat.S_IFDIR | 0o700
                return type("Stat", (), {"st_mode": mode, "st_uid": owner, "st_nlink": 1})()
            with self.subTest(owner=owner), patch.object(Path, "resolve", autospec=True, side_effect=lambda path, strict=False: path), patch.object(Path, "lstat", autospec=True, side_effect=details):
                self.assertEqual(validate_trusted_executable(executable, checkout_root=checkout, allowed_owners={0, 1000}), executable)

    def test_checkout_executable_rejects_writable_component(self):
        checkout = Path("/safe/checkout")
        executable = checkout / "bin" / "work"
        def details(path):
            mode = stat.S_IFREG | 0o700 if path == executable else stat.S_IFDIR | (0o720 if path == checkout / "bin" else 0o700)
            return type("Stat", (), {"st_mode": mode, "st_uid": 1000, "st_nlink": 1})()
        with patch.object(Path, "resolve", autospec=True, side_effect=lambda path, strict=False: path), patch.object(Path, "lstat", autospec=True, side_effect=details), self.assertRaises(WorkError):
            validate_trusted_executable(executable, checkout_root=checkout, allowed_owners={1000})

    def test_checkout_executable_rejects_dotdot_and_intermediate_symlink(self):
        checkout = Path(self.temp.name) / "checkout"
        nested = checkout / "one" / "two"
        nested.mkdir(parents=True)
        executable = nested / "work"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
        with self.assertRaises(WorkError):
            validate_trusted_executable(checkout / "one" / ".." / "one" / "two" / "work", checkout_root=checkout, allowed_owners={os.geteuid(), 0})
        executable.unlink()
        target = checkout / "target"
        target.mkdir()
        linked = checkout / "one" / "linked"
        linked.symlink_to(target, target_is_directory=True)
        escaped = linked / "work"
        (target / "work").write_text("#!/bin/sh\n")
        (target / "work").chmod(0o700)
        with self.assertRaises(WorkError):
            validate_trusted_executable(escaped, checkout_root=checkout, allowed_owners={os.geteuid(), 0})

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

    def test_root_like_work_executable_rejects_target_without_allowlist(self):
        with patch("work_orchestrator.gui.os.geteuid", return_value=0), patch("work_orchestrator.gui.validate_trusted_executable", side_effect=WorkError("owner")), patch("work_orchestrator.gui._root_launch_identity", side_effect=WorkError("allowlist")), self.assertRaises(WorkError):
            _work_executable()

    def test_root_like_work_executable_accepts_only_resolved_marcos_uid(self):
        target = pwd.struct_passwd(("marcos", "x", 4242, 4242, "", "/home/example", "/bin/sh"))
        executable = Path(__file__).resolve().parents[1] / "bin" / "work"
        with patch("work_orchestrator.gui.os.geteuid", return_value=0), patch("work_orchestrator.gui._root_launch_identity", return_value=(target, Path("/run/user/4242"))) as identity, patch("work_orchestrator.gui.validate_trusted_executable", side_effect=(WorkError("owner"), executable)) as validate:
            self.assertEqual(_work_executable(), executable)
        identity.assert_called_once_with()
        self.assertEqual(validate.call_args_list[0].kwargs["allowed_owners"], {0})
        self.assertEqual(validate.call_args_list[1].kwargs["allowed_owners"], {0, 4242})

    def test_mock_terminator_command_uses_checkout_work_absolute_path(self):
        process = object()
        executable = Path(__file__).resolve().parents[1] / "bin" / "work"
        with patch("work_orchestrator.gui._work_executable", return_value=executable), patch("work_orchestrator.gui._root_launch_identity", return_value=None), patch("work_orchestrator.gui.subprocess.Popen", return_value=process) as popen, patch("work_orchestrator.gui.threading.Thread"):
            open_terminator(self.paths, "alpha", "Alpha", self.root, check_existing=False)
        argv = popen.call_args.kwargs["args"] if "args" in popen.call_args.kwargs else popen.call_args.args[0]
        config_path = Path(argv[argv.index("-g") + 1])
        command_text = config_path.read_text(encoding="utf-8")
        self.assertIn(str(Path(__file__).resolve().parents[1] / "bin" / "work"), command_text)
        self.assertNotIn("/root", command_text)


if __name__ == "__main__":
    unittest.main()
