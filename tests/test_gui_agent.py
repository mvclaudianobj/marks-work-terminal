import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_orchestrator.errors import WorkError
from work_orchestrator.gui import (
    AGENT_ENVIRONMENT,
    AGENT_PATH,
    _agent_argv,
    _agent_executable_candidates,
    _agent_environment,
    _resolve_agent_executable,
    agent_main,
    prepare_agent,
)
from work_orchestrator.runtime import Runtime


class GuiAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.project = base / "project"
        self.project.mkdir()
        self.state = base / "state"
        self.engine = base / "markscode"
        self.engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.engine.chmod(0o700)
        self.env = patch.dict(os.environ, {"WORK_AGENT_MARKSCODE": str(self.engine), "PATH": ""}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_engine_allowlist_accepts_four_names(self):
        for engine in ("markscode", "claude", "opencode", "codex"):
            with self.subTest(engine=engine):
                self.assertIn(engine, {"markscode", "claude", "opencode", "codex"})

    def test_engine_allowlist_rejects_injection(self):
        with self.assertRaises(WorkError):
            _resolve_agent_executable("markscode;touch /tmp/x")

    def test_engine_path_is_absolute(self):
        self.assertEqual(_agent_argv("markscode"), [str(self.engine)])
        self.assertTrue(Path(_agent_argv("markscode")[0]).is_absolute())

    def test_default_engine_candidates_use_current_home(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("/home/example")):
            self.assertEqual(
                _agent_executable_candidates("markscode"),
                (Path("/home/example/.local/bin/markscode"), Path("/usr/local/bin/markscode"), Path("/usr/bin/markscode")),
            )

    def test_engine_symlink_is_rejected(self):
        link = self.project / "engine"
        link.symlink_to(self.engine)
        with patch.dict(os.environ, {"WORK_AGENT_MARKSCODE": str(link)}):
            with self.assertRaises(WorkError):
                _resolve_agent_executable("markscode")

    def test_environment_has_fixed_path_when_path_empty(self):
        with patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            environment = _agent_environment()
        self.assertEqual(environment["PATH"], AGENT_PATH)
        self.assertNotIn("WORK_AGENT_MARKSCODE", environment)

    def test_environment_is_allowlisted(self):
        with patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            environment = _agent_environment()
        self.assertTrue(set(environment).issubset(set(AGENT_ENVIRONMENT) | {"PATH"}))

    def test_prepare_registers_identity_handoff_context_and_claim(self):
        runtime = Runtime(self.project, self.state)
        agent = runtime.register_agent("markscode")
        handoff = runtime.write_handoff(agent["agent_id"], {"session_id": agent["session_id"], "token": "secret"})
        claim = runtime.acquire_claim(str(self.project), ".", agent["agent_id"])
        self.assertTrue(handoff)
        self.assertTrue(claim)
        self.assertEqual(runtime.compile_context()["project_id"], runtime.project_id)

    def test_agent_main_uses_visible_labels_and_passes_internal_values(self):
        item = {"slug": "project", "root": str(self.project)}
        answers = ["claude", "prepare", None]
        with patch("work_orchestrator.gui.Paths.discover"), patch("work_orchestrator.gui.Service") as service, patch("work_orchestrator.gui.select_project", return_value="project"), patch("work_orchestrator.gui._run_zenity", side_effect=answers) as zenity, patch("work_orchestrator.gui._entry", return_value="src"), patch("work_orchestrator.gui.prepare_agent", return_value=None) as prepare:
            service.return_value.projects.return_value = [item]
            self.assertEqual(agent_main(), 0)

        engine_arguments = zenity.call_args_list[0].args[0]
        mode_arguments = zenity.call_args_list[1].args[0]
        for arguments in (engine_arguments, mode_arguments):
            self.assertIn("--hide-column=2", arguments)
            self.assertIn("--print-column=2", arguments)
            self.assertNotIn("--hide-column=3", arguments)
            self.assertIn("--column=Usar", arguments)
            self.assertIn("--column=Valor", arguments)
            self.assertIn("--column=Descrição", arguments)
            self.assertIn("--hide-header", arguments)
            self.assertIn("--separator=--", arguments)
        self.assertEqual(
            engine_arguments[engine_arguments.index("TRUE"):engine_arguments.index("--hide-header")],
            ("TRUE", "markscode", "MarksCode", "FALSE", "claude", "Claude", "FALSE", "opencode", "OpenCode", "FALSE", "codex", "Codex"),
        )
        self.assertEqual(
            mode_arguments[mode_arguments.index("TRUE"):mode_arguments.index("--hide-header")],
            ("TRUE", "prepare", "Preparar identidade/contexto (não inicia engine)", "FALSE", "start", "Iniciar engine após confirmação"),
        )
        prepare.assert_called_once_with(self.project, "claude", "prepare", "src", shared_readonly=False)

    def test_agent_main_cancels_safely_before_prepare(self):
        item = {"slug": "project", "root": str(self.project)}
        for answers in ([None], ["codex", None]):
            with self.subTest(answers=answers), patch("work_orchestrator.gui.Paths.discover"), patch("work_orchestrator.gui.Service") as service, patch("work_orchestrator.gui.select_project", return_value="project"), patch("work_orchestrator.gui._run_zenity", side_effect=answers), patch("work_orchestrator.gui._entry") as entry, patch("work_orchestrator.gui.prepare_agent") as prepare:
                service.return_value.projects.return_value = [item]
                self.assertEqual(agent_main(), 0)
            entry.assert_not_called()
            prepare.assert_not_called()

    def test_start_confirmation_cancel_does_not_spawn(self):
        with patch("work_orchestrator.gui._run_zenity", return_value=None), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}):
            with self.assertRaises(WorkError):
                prepare_agent(self.project, "markscode", "start")
        popen.assert_not_called()

    def test_prepare_confirmation_cancel_does_not_spawn(self):
        with patch("work_orchestrator.gui._run_zenity", return_value=None), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}):
            with self.assertRaises(WorkError):
                prepare_agent(self.project, "markscode", "prepare")
        popen.assert_not_called()

    def test_start_without_confirmation_is_not_spawned(self):
        with patch("work_orchestrator.gui._agent_confirmation", return_value=False), patch("work_orchestrator.gui.subprocess.Popen") as popen, patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}):
            with self.assertRaises(WorkError):
                prepare_agent(self.project, "markscode", "start")
        popen.assert_not_called()

    def test_start_uses_absolute_argv_and_new_session(self):
        process = object()
        with patch("work_orchestrator.gui._agent_confirmation", return_value=True), patch("work_orchestrator.gui.subprocess.Popen", return_value=process) as popen, patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            result = prepare_agent(self.project, "markscode", "start", state_home=self.state)
        self.assertEqual(result["argv"], [str(self.engine)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.args[0], [str(self.engine)])
        self.assertNotIn("PATH", {"XDG_STATE_HOME"})

    def test_start_uses_project_cwd(self):
        process = object()
        with patch("work_orchestrator.gui._agent_confirmation", return_value=True), patch("work_orchestrator.gui.subprocess.Popen", return_value=process) as popen, patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            prepare_agent(self.project, "markscode", "start", state_home=self.state)
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.project))

    def test_start_uses_devnull_stdin(self):
        process = object()
        with patch("work_orchestrator.gui._agent_confirmation", return_value=True), patch("work_orchestrator.gui.subprocess.Popen", return_value=process), patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            prepare_agent(self.project, "markscode", "start", state_home=self.state)
        self.assertIsNotNone(process)

    def test_claim_conflict_overlapping_parent(self):
        runtime = Runtime(self.project, self.state)
        first = runtime.register_agent("markscode")
        second = runtime.register_agent("claude")
        runtime.acquire_claim(str(self.project), ".", first["agent_id"])
        with self.assertRaises(WorkError):
            runtime.acquire_claim(str(self.project), "src", second["agent_id"])

    def test_claim_conflict_overlapping_child(self):
        (self.project / "src").mkdir()
        runtime = Runtime(self.project, self.state)
        first = runtime.register_agent("markscode")
        second = runtime.register_agent("claude")
        runtime.acquire_claim(str(self.project), "src", first["agent_id"])
        with self.assertRaises(WorkError):
            runtime.acquire_claim(str(self.project), ".", second["agent_id"])

    def test_disjoint_claims_are_allowed(self):
        (self.project / "src").mkdir()
        (self.project / "docs").mkdir()
        runtime = Runtime(self.project, self.state)
        first = runtime.register_agent("markscode")
        second = runtime.register_agent("claude")
        self.assertTrue(runtime.acquire_claim(str(self.project), "src", first["agent_id"]))
        self.assertTrue(runtime.acquire_claim(str(self.project), "docs", second["agent_id"]))

    def test_shared_readonly_overlapping_claims_are_allowed(self):
        runtime = Runtime(self.project, self.state)
        first = runtime.register_agent("markscode")
        second = runtime.register_agent("claude")
        runtime.acquire_claim(str(self.project), ".", first["agent_id"], shared_readonly=True)
        self.assertTrue(runtime.acquire_claim(str(self.project), "src", second["agent_id"], shared_readonly=True))

    def test_write_claim_rejects_readonly_existing_claim(self):
        runtime = Runtime(self.project, self.state)
        first = runtime.register_agent("markscode")
        second = runtime.register_agent("claude")
        runtime.acquire_claim(str(self.project), ".", first["agent_id"], shared_readonly=True)
        with self.assertRaises(WorkError):
            runtime.acquire_claim(str(self.project), "src", second["agent_id"])

    def test_lease_is_required_before_start(self):
        runtime = Runtime(self.project, self.state)
        agent = runtime.register_agent("markscode")
        lease = runtime.acquire_lease("project", str(self.project), agent["agent_id"])
        self.assertEqual(lease["agent_id"], agent["agent_id"])

    def test_second_project_lease_conflicts(self):
        runtime = Runtime(self.project, self.state)
        first = runtime.register_agent("markscode")
        second = runtime.register_agent("claude")
        runtime.acquire_lease("project", str(self.project), first["agent_id"])
        with self.assertRaises(WorkError):
            runtime.acquire_lease("project", str(self.project), second["agent_id"])

    def test_handoff_redaction(self):
        runtime = Runtime(self.project, self.state)
        agent = runtime.register_agent("markscode")
        runtime.write_handoff(agent["agent_id"], {"prompt": "secret prompt", "token": "secret-token", "safe": "ok"})
        body = runtime.read_handoffs()[0]["body"]
        self.assertNotIn("secret prompt", body)
        self.assertNotIn("secret-token", body)

    def test_log_directory_is_private_after_start_mock(self):
        process = object()
        with patch("work_orchestrator.gui._agent_confirmation", return_value=True), patch("work_orchestrator.gui.subprocess.Popen", return_value=process), patch("work_orchestrator.gui._root_launch_identity", return_value=None):
            result = prepare_agent(self.project, "markscode", "start", state_home=self.state)
        log_path = self.state / "work-orchestrator" / "runtime" / result["agent"]["project_id"] / "agent-logs" / f"{result['agent']['agent_id']}.log"
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
