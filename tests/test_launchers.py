import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = tuple(sorted((ROOT / "bin").glob("work-gui-*")))
DESKTOP_FILES = tuple(sorted((ROOT / "desktop").glob("*.desktop")))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.links = self.home / ".local" / "bin"
        self.mock = self.base / "mock"
        self.links.mkdir(parents=True)
        self.mock.mkdir()
        functions = ", ".join(repr(f"{launcher.name.removeprefix('work-gui-')}_main") for launcher in LAUNCHERS)
        (self.mock / "sitecustomize.py").write_text(
            "import json, os, sys, types\n"
            "module = types.ModuleType('work_orchestrator.gui')\n"
            "def main():\n"
            "    with open(os.environ['WORK_GUI_TEST_OUTPUT'], 'w', encoding='utf-8') as stream:\n"
            "        json.dump(sys.argv, stream)\n"
            "    return 0\n"
            f"for name in ({functions},):\n"
            "    setattr(module, name, main)\n"
            "sys.modules['work_orchestrator.gui'] = module\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_launcher(self, launcher: Path, output: Path):
        marker = self.base / "shell-injection"
        argument = f"$(touch {marker})"
        environment = {
            "HOME": str(self.home),
            "PATH": "",
            "PYTHONPATH": str(self.mock),
            "WORK_GUI_TEST_OUTPUT": str(output),
        }
        result = subprocess.run(
            [str(launcher), "argument with spaces", argument],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        return json.loads(output.read_text(encoding="utf-8"))

    def test_launchers_from_checkout_and_symlink_with_empty_path(self):
        self.assertTrue(LAUNCHERS)
        for launcher in LAUNCHERS:
            resolved = str(launcher.resolve(strict=True))
            link = self.links / launcher.name
            link.symlink_to(launcher)
            for invoked in (launcher, link):
                with self.subTest(launcher=launcher.name, invoked=invoked):
                    output = self.base / f"{launcher.name}-{invoked.parent.name}.json"
                    arguments = self.run_launcher(invoked, output)
                    self.assertEqual(arguments[0], resolved)
                    self.assertEqual(arguments[1], "argument with spaces")
                    self.assertTrue(arguments[2].startswith("$(touch "))

    def test_desktop_templates_use_path_commands(self):
        self.assertTrue(DESKTOP_FILES)
        for desktop in DESKTOP_FILES:
            command = desktop.stem
            content = desktop.read_text(encoding="utf-8")
            with self.subTest(desktop=desktop.name):
                self.assertIn(f"Exec={command}\n", content)
                self.assertIn(f"TryExec={command}\n", content)
                self.assertNotIn("/home/", content)


if __name__ == "__main__":
    unittest.main()
