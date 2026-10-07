import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.hook import detach_main
from work_orchestrator.paths import Paths


class DetachHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = Paths(base / "config", base / "state", base / "runtime")

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_invalid_arguments_without_subprocess(self):
        for arguments in ([], ["alpha", "beta"], ["../alpha"]):
            with self.subTest(arguments=arguments), patch("work_orchestrator.hook.Paths.discover", return_value=self.paths), patch("work_orchestrator.hook.subprocess.run") as run:
                self.assertEqual(detach_main(arguments), 2)
                run.assert_not_called()

    def test_valid_slug_saves_with_absolute_checkout_executable(self):
        with patch("work_orchestrator.hook.Paths.discover", return_value=self.paths), patch("work_orchestrator.hook.subprocess.run") as run:
            self.assertEqual(detach_main(["alpha-1"]), 0)
        arguments = run.call_args.args[0]
        self.assertTrue(Path(arguments[0]).is_absolute())
        self.assertEqual(Path(arguments[0]).name, "work")
        self.assertEqual(arguments[1:], ["save", "alpha-1"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_subprocess_failure_returns_error(self):
        failure = subprocess.CalledProcessError(1, ["work", "save", "alpha"])
        with patch("work_orchestrator.hook.Paths.discover", return_value=self.paths), patch("work_orchestrator.hook.subprocess.run", side_effect=failure):
            self.assertEqual(detach_main(["alpha"]), 2)


if __name__ == "__main__":
    unittest.main()
