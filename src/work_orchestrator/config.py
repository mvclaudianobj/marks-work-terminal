import tomllib
from pathlib import Path
from typing import Any

from .errors import WorkError
from .model import ENGINES, MONITOR_STATES, POLICIES, MonitorConfig, Pane, Project, Visual, Window
from .paths import Paths, safe_child, validate_slug
from .store import read_private_bytes


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkError(f"{label} deve ser uma tabela TOML")
    return value


def _string(value: Any, label: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorkError(f"{label} deve ser texto não vazio")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise WorkError(f"{label} deve ser booleano")
    return value


def _policy(value: Any, label: str) -> str:
    policy = _string(value, label)
    if policy not in POLICIES:
        raise WorkError(f"{label} deve ser always, prompt ou never")
    return policy


def _cwd(value: Any, label: str, base: Path) -> Path:
    text = _string(value, label)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkError(f"{label} não existe: {path}") from exc
    if not resolved.is_dir():
        raise WorkError(f"{label} não é diretório: {resolved}")
    return resolved


def _cwd_aliases(value: Any, root: Path) -> tuple[tuple[str, Path], ...]:
    if value is None:
        return ()
    table = _table(value, "project.cwd_aliases")
    aliases: list[tuple[str, Path]] = []
    for source, destination_value in table.items():
        if not isinstance(source, str) or not source or any(ord(char) < 32 or ord(char) == 127 for char in source):
            raise WorkError("origem de project.cwd_aliases deve ser caminho absoluto seguro")
        source_path = Path(source)
        if not source_path.is_absolute() or str(source_path) != source or source_path.parts.count(".."):
            raise WorkError(f"origem de project.cwd_aliases deve ser absoluta e canônica: {source}")
        destination = _cwd(destination_value, f"project.cwd_aliases[{source!r}]", root)
        if not Path(str(destination_value)).is_absolute():
            raise WorkError(f"destino de project.cwd_aliases deve ser absoluto: {source}")
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise WorkError(f"destino de project.cwd_aliases deve estar sob project.root: {source}") from exc
        aliases.append((source, destination))
    return tuple(aliases)


def _command(value: Any, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise WorkError(f"{label} deve ser um array não vazio")
    if any(not isinstance(item, str) or not item or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in item) for item in value):
        raise WorkError(f"{label} aceita apenas strings não vazias sem controles")
    return tuple(value)


def _monitor_keywords(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 32:
        raise WorkError(f"{label} deve ser uma lista com no máximo 32 tokens")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in item):
            raise WorkError(f"{label} aceita apenas tokens literais seguros")
        if any(char in item for char in "\\[](){}*+?|^$."):
            raise WorkError(f"{label} não aceita regex ou metacaracteres")
        if item not in result:
            result.append(item)
    return tuple(result)


def _monitor_config(value: Any, label: str = "monitor") -> MonitorConfig:
    if value is None:
        return MonitorConfig()
    table = _table(value, label)
    allowed = {"autosave", "debounce", "interval", "history", "agentic", "notify_working", "idle_warning_seconds", "idle_attention_seconds", "states"}
    unknown = set(table) - allowed
    if unknown:
        raise WorkError(f"chaves desconhecidas em monitor: {', '.join(sorted(unknown))}")
    warning = table.get("idle_warning_seconds", 300)
    attention = table.get("idle_attention_seconds", 600)
    if type(warning) is not int or type(attention) is not int or not 1 <= warning <= 86400 or not 1 <= attention <= 172800 or warning >= attention:
        raise WorkError("limites de inatividade inválidos")
    debounce = table.get("debounce", 0.75)
    interval = table.get("interval", 5.0)
    history = table.get("history", False)
    states = table.get("states", sorted(MONITOR_STATES))
    if not isinstance(states, list) or not states or any(not isinstance(item, str) or item not in MONITOR_STATES for item in states) or len(set(states)) != len(states):
        raise WorkError("monitor.states contém estados inválidos ou duplicados")
    autosave = table.get("autosave", True)
    agentic = table.get("agentic", False)
    notify_working = table.get("notify_working", False)
    if type(autosave) is not bool or type(history) is not bool or type(agentic) is not bool or type(notify_working) is not bool:
        raise WorkError("booleano de monitor inválido")
    if type(debounce) not in {int, float} or not 0.5 <= debounce <= 1.0:
        raise WorkError("monitor.debounce inválido")
    if type(interval) not in {int, float} or not 0.1 <= interval <= 86400:
        raise WorkError("monitor.interval inválido")
    return MonitorConfig(
        autosave=autosave,
        debounce=float(debounce),
        interval=float(interval),
        history=history,
        agentic=agentic,
        notify_working=notify_working,
        idle_warning_seconds=warning,
        idle_attention_seconds=attention,
        states=tuple(states),
    )


def config_path(paths: Paths, slug: str) -> Path:
    return safe_child(paths.config, f"{validate_slug(slug)}.toml")


def parse_project(paths: Paths, slug: str, payload: bytes) -> Project:
    slug = validate_slug(slug)
    path = config_path(paths, slug)
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise WorkError(f"configuração não é UTF-8: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise WorkError(f"TOML inválido em {path}: {exc}") from exc
    allowed_top = {"version", "project", "windows", "visuals", "monitor"}
    unknown = set(raw) - allowed_top
    if unknown:
        raise WorkError(f"chaves desconhecidas no topo: {', '.join(sorted(unknown))}")
    if raw.get("version", 1) != 1:
        raise WorkError("versão TOML incompatível; esperada 1")
    project = _table(raw.get("project"), "project")
    allowed_project = {"name", "root", "session", "command_policy", "cwd_aliases"}
    unknown = set(project) - allowed_project
    if unknown:
        raise WorkError(f"chaves desconhecidas em project: {', '.join(sorted(unknown))}")
    root = _cwd(project.get("root"), "project.root", path.parent)
    cwd_aliases = _cwd_aliases(project.get("cwd_aliases"), root)
    name = _string(project.get("name", slug), "project.name")
    session = _string(project.get("session", f"work-{slug}"), "project.session")
    if any(char in session for char in ".:"):
        raise WorkError("project.session não pode conter ponto ou dois-pontos")
    default_policy = _policy(project.get("command_policy", "prompt"), "project.command_policy")
    windows_raw = raw.get("windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise WorkError("windows deve conter ao menos uma janela")
    windows: list[Window] = []
    names: set[str] = set()
    focused_windows = 0
    for wi, item in enumerate(windows_raw):
        window = _table(item, f"windows[{wi}]")
        allowed_window = {"name", "cwd", "layout", "focus", "engine", "panes"}
        unknown = set(window) - allowed_window
        if unknown:
            raise WorkError(f"chaves desconhecidas em windows[{wi}]: {', '.join(sorted(unknown))}")
        window_name = _string(window.get("name"), f"windows[{wi}].name")
        if window_name in names or any(char in window_name for char in ".:"):
            raise WorkError(f"nome de janela inválido ou duplicado: {window_name}")
        names.add(window_name)
        window_cwd = _cwd(window.get("cwd", str(root)), f"windows[{wi}].cwd", root)
        layout = _string(window.get("layout"), f"windows[{wi}].layout", required=False)
        window_focus = _boolean(window.get("focus", wi == 0), f"windows[{wi}].focus")
        engine = _string(window.get("engine"), f"windows[{wi}].engine", required=False)
        if engine is not None and engine not in ENGINES:
            raise WorkError(f"windows[{wi}].engine inválida")
        focused_windows += int(window_focus)
        panes_raw = window.get("panes", [{}])
        if not isinstance(panes_raw, list) or not panes_raw:
            raise WorkError(f"windows[{wi}].panes deve conter ao menos um pane")
        panes: list[Pane] = []
        focused_panes = 0
        for pi, pane_item in enumerate(panes_raw):
            pane = _table(pane_item, f"windows[{wi}].panes[{pi}]")
            allowed_pane = {"cwd", "name", "command", "policy", "focus"}
            unknown = set(pane) - allowed_pane
            if unknown:
                raise WorkError(f"chaves desconhecidas em pane: {', '.join(sorted(unknown))}")
            pane_cwd = _cwd(pane.get("cwd", str(window_cwd)), f"pane {wi}:{pi} cwd", root)
            pane_name = _string(pane.get("name"), f"pane {wi}:{pi} name", required=False)
            command = _command(pane.get("command"), f"pane {wi}:{pi} command")
            policy = _policy(pane.get("policy", default_policy), f"pane {wi}:{pi} policy")
            pane_focus = _boolean(pane.get("focus", pi == 0), f"pane {wi}:{pi} focus")
            focused_panes += int(pane_focus)
            panes.append(Pane(pane_cwd, pane_name, command, policy, pane_focus))
        if focused_panes != 1:
            raise WorkError(f"janela {window_name} deve ter exatamente um pane com focus=true")
        windows.append(Window(window_name, window_cwd, layout, tuple(panes), window_focus, engine))
    if focused_windows != 1:
        raise WorkError("deve haver exatamente uma janela com focus=true")
    visuals_raw = raw.get("visuals", [])
    if not isinstance(visuals_raw, list):
        raise WorkError("visuals deve ser um array")
    visuals: list[Visual] = []
    visual_ids: set[str] = set()
    visual_orders: set[int] = set()
    for vi, item in enumerate(visuals_raw):
        visual = _table(item, f"visuals[{vi}]")
        allowed_visual = {"id", "kind", "title", "root", "order", "attach_target"}
        unknown = set(visual) - allowed_visual
        if unknown:
            raise WorkError(f"chaves desconhecidas em visuals[{vi}]: {', '.join(sorted(unknown))}")
        visual_id = validate_slug(_string(visual.get("id"), f"visuals[{vi}].id"))
        kind = _string(visual.get("kind", "tilix"), f"visuals[{vi}].kind")
        title = _string(visual.get("title"), f"visuals[{vi}].title", required=False)
        order = visual.get("order")
        if kind != "tilix" or type(order) is not int or order < 0 or visual_id in visual_ids or order in visual_orders:
            raise WorkError(f"visuals[{vi}] possui id, kind ou order inválido/duplicado")
        visual_root = _cwd(visual.get("root", str(root)), f"visuals[{vi}].root", root)
        try:
            visual_root.relative_to(root)
        except ValueError as exc:
            raise WorkError(f"visuals[{vi}].root deve estar sob project.root") from exc
        target = _table(visual.get("attach_target"), f"visuals[{vi}].attach_target")
        if set(target) - {"session", "window"}:
            raise WorkError(f"chaves desconhecidas em visuals[{vi}].attach_target")
        target_session = _string(target.get("session"), f"visuals[{vi}].attach_target.session")
        target_window = _string(target.get("window"), f"visuals[{vi}].attach_target.window", required=False)
        if target_session != session or target_window is not None and target_window not in names:
            raise WorkError(f"visuals[{vi}].attach_target inválido")
        visual_ids.add(visual_id)
        visual_orders.add(order)
        visuals.append(Visual(visual_id, kind, visual_root, order, target_session, target_window, title))
    visuals.sort(key=lambda item: item.order)
    return Project(slug, name, root, session, default_policy, cwd_aliases, tuple(windows), tuple(visuals), _monitor_config(raw.get("monitor")))


def load_project_document(paths: Paths, slug: str) -> tuple[Project, bytes, str]:
    slug = validate_slug(slug)
    path = config_path(paths, slug)
    try:
        payload, fingerprint = read_private_bytes(path)
    except WorkError as exc:
        if not path.exists() and not path.is_symlink():
            raise WorkError(f"projeto não configurado: {slug}") from exc
        raise
    return parse_project(paths, slug, payload), payload, fingerprint


def load_project(paths: Paths, slug: str) -> Project:
    project, _, _ = load_project_document(paths, slug)
    return project
