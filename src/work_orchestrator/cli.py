import argparse
import json
import os
import sys
from pathlib import Path

from .config import config_path
from .history import append as append_history, read as read_history
from .errors import WorkError
from .paths import Paths, ensure_regular_private_file, validate_slug
from .store import write_bytes_atomic
from .service import Service
from .monitor import Monitor
from .notifier import notify_or_log
from .runtime import Runtime
from .agent import use as use_agent
from .context import compile_context
from .visuals import default_recommended_toml, migrate_default_visuals


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="work", description="Orquestra projetos em sessões tmux")
    sub = value.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="cria uma configuração de projeto")
    init.add_argument("slug")
    init.add_argument("--root", type=Path, default=Path.cwd())
    init.add_argument("--name")
    init.add_argument("--layout", choices=("minimal", "recommended"), default="minimal")
    visuals = sub.add_parser("visuals", help="gerencia visuais declarativos")
    visuals_sub = visuals.add_subparsers(dest="visuals_command", required=True)
    migrate = visuals_sub.add_parser("migrate-defaults", help="migra o layout padrão legado de duas para seis janelas")
    migrate.add_argument("--project")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--yes", action="store_true")
    for name in ("start", "save", "restore", "status"):
        item = sub.add_parser(name)
        item.add_argument("slug")
    open_project = sub.add_parser("open")
    open_project.add_argument("slug")
    open_project.add_argument("--window")
    open_project.add_argument("--visual-id")
    stop = sub.add_parser("stop")
    stop.add_argument("slug")
    stop.add_argument("--yes", action="store_true")
    edit = sub.add_parser("edit", help="edita campos seguros da configuração")
    edit.add_argument("slug")
    edit.add_argument("--name")
    edit.add_argument("--root")
    edit.add_argument("--command-policy", choices=("always", "prompt", "never"))
    edit.add_argument("--expect-fingerprint")
    sub.add_parser("list")
    workspace = sub.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    for command in ("save", "status"):
        item = workspace_sub.add_parser(command)
        item.add_argument("name", nargs="?", default="default")
        if command == "save":
            item.add_argument("--include-inactive", action="store_true")
    restore = workspace_sub.add_parser("restore")
    restore.add_argument("name", nargs="?", default="default")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--visual", choices=("terminator", "terminator-tabs", "terminator-windows"))
    restore.add_argument("--project")
    restore.add_argument("--yes", action="store_true")
    open_workspace = workspace_sub.add_parser("open")
    open_workspace.add_argument("name", nargs="?", default="default")
    open_workspace.add_argument("--dry-run", action="store_true")
    open_workspace.add_argument("--visual", choices=("terminator", "terminator-tabs", "terminator-windows"))
    open_workspace.add_argument("--yes", action="store_true")
    history = sub.add_parser("history", help="histórico opt-in redigido")
    history.add_argument("action", choices=("show", "record"), nargs="?", default="show")
    history.add_argument("--kind", default="user-command")
    history.add_argument("--value", default="")
    monitor = sub.add_parser("monitor", help="monitora metadados de uma sessão owned")
    monitor_sub = monitor.add_subparsers(dest="monitor_command", required=True)
    monitor_start = monitor_sub.add_parser("start")
    monitor_start.add_argument("slug")
    monitor_start.add_argument("--once", action="store_true")
    monitor_start.add_argument("--interval", type=float, default=5.0)
    monitor_status = monitor_sub.add_parser("status")
    monitor_status.add_argument("slug")
    monitor_stop = monitor_sub.add_parser("stop")
    monitor_stop.add_argument("slug")
    signal_command = sub.add_parser("signal", help="envia estado agêntico explícito metadata-only")
    signal_command.add_argument("slug")
    signal_command.add_argument("--window", required=True)
    signal_command.add_argument("--state", required=True, choices=("working", "waiting-user", "phase-started", "completed", "failed"))
    notify = sub.add_parser("notify", help="notificações locais")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    notify_sub.add_parser("test")
    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_status = runtime_sub.add_parser("status")
    runtime_status.add_argument("project", type=Path)
    agent = sub.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_list = agent_sub.add_parser("list")
    agent_list.add_argument("project", type=Path)
    register = agent_sub.add_parser("register")
    register.add_argument("engine", choices=("markscode", "claude", "opencode", "codex"))
    register.add_argument("project", type=Path)
    register.add_argument("--provider")
    register.add_argument("--version")
    register.add_argument("--capability", action="append", default=[])
    register.add_argument("--worktree")
    register.add_argument("--cwd")
    register.add_argument("--tmux-visual")
    for name in ("start", "heartbeat", "stop"):
        item = agent_sub.add_parser(name)
        item.add_argument("agent_id")
        item.add_argument("project", type=Path)
    use = agent_sub.add_parser("use")
    use.add_argument("engine", choices=("markscode", "claude", "opencode", "codex"))
    use.add_argument("project", type=Path)
    use.add_argument("--worktree", type=Path)
    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    create = task_sub.add_parser("create")
    create.add_argument("project", type=Path)
    create.add_argument("title")
    create.add_argument("--description", default="")
    create.add_argument("--priority", type=int, default=0)
    for name in ("claim", "release"):
        item = task_sub.add_parser(name)
        item.add_argument("project", type=Path)
        item.add_argument("task_id")
        item.add_argument("agent_id")
    claim = sub.add_parser("claim")
    claim_sub = claim.add_subparsers(dest="claim_command", required=True)
    acquire = claim_sub.add_parser("acquire")
    acquire.add_argument("project", type=Path)
    acquire.add_argument("worktree_id")
    acquire.add_argument("path")
    acquire.add_argument("agent_id")
    acquire.add_argument("--shared-readonly", action="store_true")
    release = claim_sub.add_parser("release")
    release.add_argument("project", type=Path)
    release.add_argument("claim_id")
    release.add_argument("agent_id")
    handoff = sub.add_parser("handoff")
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    write = handoff_sub.add_parser("write")
    write.add_argument("project", type=Path)
    write.add_argument("agent_id")
    write.add_argument("body")
    write.add_argument("--format", choices=("json", "markdown"), default="json")
    read_handoff = handoff_sub.add_parser("read")
    read_handoff.add_argument("project", type=Path)
    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    compile_parser = context_sub.add_parser("compile")
    compile_parser.add_argument("project", type=Path)
    compile_parser.add_argument("--limit", type=int, default=20)
    compile_parser.add_argument("--max-chars", type=int, default=12000)
    event = sub.add_parser("event")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    append = event_sub.add_parser("append")
    append.add_argument("project", type=Path)
    append.add_argument("event_id")
    append.add_argument("kind")
    append.add_argument("metadata")
    timeline = event_sub.add_parser("timeline")
    timeline.add_argument("project", type=Path)
    timeline.add_argument("--limit", type=int, default=100)
    return value


def init_project(paths: Paths, slug: str, root: Path, name: str | None = None, layout: str = "minimal") -> Path:
    slug = validate_slug(slug)
    name = name if name is not None else slug
    if not name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise WorkError("nome do projeto deve ser texto não vazio sem controles")
    if layout not in {"minimal", "recommended"}:
        raise WorkError("layout inicial inválido")
    try:
        root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkError(f"diretório raiz não existe: {root}") from exc
    if not root.is_dir():
        raise WorkError(f"raiz não é diretório: {root}")
    path = config_path(paths, slug)
    if path.exists() or path.is_symlink():
        if path.exists():
            ensure_regular_private_file(path)
        raise WorkError(f"configuração já existe: {path}")
    lines = [
        "[project]",
        f"name = {json.dumps(name, ensure_ascii=False)}",
        f"root = {json.dumps(str(root), ensure_ascii=False)}",
        'command_policy = "prompt"',
        "",
        "[monitor]",
        "autosave = true",
        "debounce = 0.75",
        "interval = 5.0",
        "history = false",
        "agentic = false",
        "notify_working = false",
        "",
    ]
    if layout == "recommended":
        lines.extend((default_recommended_toml(slug), ""))
    else:
        lines.extend((
            "[[windows]]",
            'name = "main"',
            "focus = true",
            "",
            "[[windows.panes]]",
            "focus = true",
            "",
        ))
    body = "\n".join(lines)
    write_bytes_atomic(path, body.encode("utf-8"), exclusive=True)
    return path


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        paths = Paths.discover()
        paths.ensure()
        if args.command == "init":
            print(init_project(paths, args.slug, args.root, args.name, args.layout))
            return
        if args.command == "visuals":
            service = Service(paths)
            print(json.dumps(migrate_default_visuals(paths, args.project, dry_run=args.dry_run, yes=args.yes, session_active=service.tmux.exists), ensure_ascii=False, indent=2))
            return
        if args.command == "history":
            if args.action == "record":
                append_history(paths.state, args.kind, {"value": args.value}, enabled=True)
            print(json.dumps(read_history(paths.state), ensure_ascii=False, indent=2))
            return
        if args.command == "notify":
            if args.notify_command == "test":
                notify_or_log("work-orchestrator", "notificação de teste")
                return
        if args.command == "runtime":
            print(json.dumps(Runtime(args.project).status(), ensure_ascii=False, indent=2))
            return
        if args.command == "agent":
            if args.agent_command == "list":
                print(json.dumps(Runtime(args.project).agents(), ensure_ascii=False, indent=2))
            elif args.agent_command == "register":
                runtime = Runtime(args.project)
                print(json.dumps(runtime.register_agent(args.engine, args.provider, args.version, args.capability, args.worktree, args.cwd, args.tmux_visual), ensure_ascii=False, indent=2))
            elif args.agent_command == "use":
                print(json.dumps(use_agent(args.engine, args.project, args.worktree), ensure_ascii=False, indent=2))
            else:
                runtime = Runtime(args.project)
                action = "started" if args.agent_command == "start" else "stopped" if args.agent_command == "stop" else None
                result = runtime.agent_state(args.agent_id, action) if action else runtime.heartbeat(args.agent_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "task":
            runtime = Runtime(args.project)
            if args.task_command == "create":
                print(runtime.create_task(args.title, args.description, args.priority))
            elif args.task_command == "claim":
                print(runtime.claim_task(args.task_id, args.agent_id))
            else:
                runtime.release_task(args.task_id, args.agent_id)
            return
        if args.command == "claim":
            runtime = Runtime(args.project)
            if args.claim_command == "acquire":
                print(runtime.acquire_claim(args.worktree_id, args.path, args.agent_id, args.shared_readonly))
            else:
                runtime.release_claim(args.claim_id, args.agent_id)
            return
        if args.command == "handoff":
            runtime = Runtime(args.project)
            if args.handoff_command == "write":
                print(runtime.write_handoff(args.agent_id, args.body, args.format))
            else:
                print(json.dumps(runtime.read_handoffs(), ensure_ascii=False, indent=2))
            return
        if args.command == "context":
            print(json.dumps(compile_context(args.project, args.limit, args.max_chars), ensure_ascii=False, indent=2))
            return
        if args.command == "event":
            runtime = Runtime(args.project)
            if args.event_command == "append":
                print(json.dumps(runtime.append_event(args.event_id, args.kind, json.loads(args.metadata)), ensure_ascii=False, indent=2))
            else:
                print(json.dumps(runtime.timeline(args.limit), ensure_ascii=False, indent=2))
            return
        if args.command == "monitor":
            monitor_service = Monitor(paths)
            if args.monitor_command == "start":
                monitor_service.start(args.slug, once=args.once, interval=args.interval)
                return
            if args.monitor_command == "status":
                print(json.dumps(monitor_service.status(args.slug), ensure_ascii=False, indent=2))
                return
            if args.monitor_command == "stop":
                monitor_service.stop(args.slug)
                return
        if args.command == "signal":
            state = args.state.replace("-", "_")
            print(json.dumps(Monitor(paths).signal(args.slug, args.window, state), ensure_ascii=False, indent=2))
            return
        service = Service(paths)
        if args.command == "workspace":
            if args.workspace_command == "save":
                print(service.workspace_save(args.name, args.include_inactive))
            elif args.workspace_command == "status":
                print(json.dumps(service.workspace_status(args.name), ensure_ascii=False, indent=2))
            else:
                if args.visual and not args.dry_run and not args.yes:
                    raise WorkError("restore visual exige --yes; nenhuma janela foi aberta")
                print(json.dumps(service.workspace_restore(args.name, args.dry_run, args.visual, args.project), ensure_ascii=False, indent=2))
            return
        if args.command == "start":
            skipped = service.start(args.slug)
            print(f"sessão iniciada: {args.slug}")
            if skipped:
                print(f"comandos prompt não executados sem TTY: {', '.join(skipped)}")
        elif args.command == "open":
            action, skipped = service.open(args.slug, window=args.window, visual_id=args.visual_id)
            print(f"sessão {action}: {args.slug}; use work open {args.slug} em um TTY para anexar")
            if skipped:
                print(f"comandos prompt não executados sem TTY: {', '.join(skipped)}")
        elif args.command == "save":
            print(service.save(args.slug))
        elif args.command == "restore":
            service.restore(args.slug)
            print(f"sessão restaurada: {args.slug}")
        elif args.command == "stop":
            print(service.stop(args.slug, args.yes))
        elif args.command == "status":
            print(json.dumps(service.status(args.slug), ensure_ascii=False, indent=2))
        elif args.command == "edit":
            print(json.dumps(service.edit(
                args.slug,
                name=args.name,
                root=args.root,
                command_policy=args.command_policy,
                expected_fingerprint=args.expect_fingerprint,
            ), ensure_ascii=False, indent=2))
        elif args.command == "list":
            print(json.dumps(service.projects(), ensure_ascii=False, indent=2))
    except (WorkError, OSError, ValueError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
