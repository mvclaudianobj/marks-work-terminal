from .runtime import ENGINES, Runtime


def use(engine, project, worktree=None):
    runtime = Runtime(worktree or project)
    agent = runtime.register_agent(engine, worktree_id=str(worktree or project), cwd=worktree or project)
    runtime.write_handoff(agent["agent_id"], {"engine": engine, "project_id": runtime.project_id, "session_id": agent["session_id"], "prepared": True})
    return agent
