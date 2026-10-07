import json
import re
import tomllib
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import config_path, parse_project
from .errors import WorkError
from .paths import Paths, ensure_private_directory, validate_slug
from .store import SCHEMA, read_private_bytes, snapshot_path, write_bytes_atomic


DEFAULT_VISUALS = (
    ("dev1-markscode", "Dev1 - Markscode", "dev1-markscode"),
    ("dev2-opencode", "Dev2 - Opencode", "dev2-opencode"),
    ("dev3-codex", "Dev3 - Codex", "dev3-codex"),
    ("dev4-claude", "Dev4 - Claude", "dev4-claude"),
    ("commands", "Commands", "commands"),
    ("works", "Works", "works"),
)
DEFAULT_ENGINES = {
    "dev1-markscode": "markscode",
    "dev2-opencode": "opencode",
    "dev3-codex": "codex",
    "dev4-claude": "claude",
}
LEGACY_VISUALS = tuple((visual_id, title, "apoio" if visual_id == "works" else "dev") for visual_id, title, _ in DEFAULT_VISUALS)
_HEADER = re.compile(r"^\s*(\[\[?[^\]]+\]\]?)\s*(?:#.*)?$")


def default_visuals_toml(slug: str) -> str:
    session = f"work-{validate_slug(slug)}"
    lines: list[str] = []
    for order, (visual_id, title, window) in enumerate(DEFAULT_VISUALS):
        lines.extend((
            "[[visuals]]",
            f"id = {json.dumps(visual_id)}",
            'kind = "tilix"',
            f"title = {json.dumps(title, ensure_ascii=False)}",
            f"order = {order}",
            "",
            "[visuals.attach_target]",
            f"session = {json.dumps(session)}",
            f"window = {json.dumps(window)}",
            "",
        ))
    return "\n".join(lines)


def default_windows_toml() -> str:
    lines: list[str] = []
    for index, (window, _, _) in enumerate(DEFAULT_VISUALS):
        lines.extend((
            "[[windows]]",
            f"name = {json.dumps(window)}",
            f"focus = {'true' if index == 0 else 'false'}",
        ))
        if window in DEFAULT_ENGINES:
            lines.append(f"engine = {json.dumps(DEFAULT_ENGINES[window])}")
        lines.extend(("", "[[windows.panes]]", 'name = "shell"', "focus = true", ""))
    return "\n".join(lines)


def default_recommended_toml(slug: str) -> str:
    return f"{default_visuals_toml(slug)}\n{default_windows_toml()}"


def _legacy_shape(project: Any) -> bool:
    if [(visual.id, visual.title, visual.window) for visual in project.visuals] != list(LEGACY_VISUALS):
        return False
    if [window.name for window in project.windows] != ["dev", "apoio"]:
        return False
    dev, apoio = project.windows
    if dev.cwd != project.root or apoio.cwd != project.root or dev.layout != "even-horizontal" or apoio.layout is not None:
        return False
    if [pane.name for pane in dev.panes] != ["codigo", "shell"] or [pane.focus for pane in dev.panes] != [True, False]:
        return False
    if [pane.name for pane in apoio.panes] != ["shell"] or [pane.focus for pane in apoio.panes] != [True]:
        return False
    if not dev.focus or apoio.focus:
        return False
    return all(pane.cwd == project.root and pane.command is None for window in project.windows for pane in window.panes)


def _recommended_shape_without_engines(project: Any) -> bool:
    if [(visual.id, visual.title, visual.window) for visual in project.visuals] != list(DEFAULT_VISUALS):
        return False
    if [window.name for window in project.windows] != [item[0] for item in DEFAULT_VISUALS]:
        return False
    for index, window in enumerate(project.windows):
        if window.engine is not None or window.cwd != project.root or window.layout is not None or window.focus != (index == 0) or len(window.panes) != 1:
            return False
        pane = window.panes[0]
        if pane.cwd != project.root or pane.name != "shell" or pane.command is not None or not pane.focus:
            return False
    return True


def _add_recommended_engines(text: str) -> str:
    lines = text.splitlines(keepends=True)
    current_window = None
    output: list[str] = []
    for line in lines:
        header = _HEADER.match(line.rstrip("\r\n"))
        if header and header.group(1) == "[[windows]]":
            current_window = None
        output.append(line)
        match = re.fullmatch(r'(\s*name\s*=\s*")([^"]+)("\s*(?:#.*)?)(\r?\n)?', line)
        if match and current_window is None and match.group(2) in DEFAULT_ENGINES:
            current_window = match.group(2)
            newline = match.group(4) or "\n"
            output.append(f'engine = "{DEFAULT_ENGINES[current_window]}"{newline}')
    return "".join(output)


def transform_legacy_default_toml(paths: Paths, slug: str, payload: bytes) -> tuple[bytes | None, str]:
    project = parse_project(paths, slug, payload)
    if _recommended_shape_without_engines(project):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkError("configuração recommended inválida") from exc
        replacement = _add_recommended_engines(text).encode("utf-8")
        parse_project(paths, slug, replacement)
        return replacement, "topologia recommended de seis janelas recebeu somente metadata engine"
    if [(visual.id, visual.title, visual.window) for visual in project.visuals] == list(DEFAULT_VISUALS) and [window.name for window in project.windows] == [item[0] for item in DEFAULT_VISUALS]:
        if all(window.engine == DEFAULT_ENGINES.get(window.name) for window in project.windows) and all(window.cwd == project.root and window.layout is None and window.focus == (index == 0) and len(window.panes) == 1 and window.panes[0].cwd == project.root and window.panes[0].name == "shell" and window.panes[0].command is None and window.panes[0].focus for index, window in enumerate(project.windows)):
            return None, "topologia recommended de seis janelas já aplicada"
        return None, "topologia customizada ou diferente do padrão recommended exato; nenhuma alteração"
    if not _legacy_shape(project):
        return None, "topologia customizada ou diferente do padrão legado exato; nenhuma alteração"
    try:
        text = payload.decode("utf-8")
        raw = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise WorkError("configuração legada inválida") from exc
    if set(raw) - {"version", "project", "monitor", "visuals", "windows"}:
        return None, "topologia contém seções de topo não reconhecidas; nenhuma alteração"
    lines = text.splitlines(keepends=True)
    insertion = None
    for index, line in enumerate(lines):
        match = _HEADER.match(line.rstrip("\r\n"))
        if match and match.group(1) in {"[[visuals]]", "[[windows]]"}:
            insertion = index
            break
    if insertion is None:
        raise WorkError("configuração não contém visuals/windows legados")
    prefix = "".join(lines[:insertion])
    if prefix and not prefix.endswith("\n\n"):
        prefix = prefix.rstrip("\r\n") + "\n\n"
    replacement = (prefix + default_recommended_toml(slug) + "\n").encode("utf-8")
    parse_project(paths, slug, replacement)
    return replacement, "padrão legado exato de duas janelas reconhecido"


def _snapshot_layout() -> str:
    body = "80x24,0,0,0"
    checksum = 0
    for value in body.encode("utf-8"):
        checksum = ((checksum >> 1) | ((checksum & 1) << 15)) + value
    return f"{checksum & 0xffff:04x},{body}"


def transform_legacy_snapshot(snapshot: dict[str, Any], slug: str, session: str, root: Path) -> tuple[dict[str, Any] | None, str]:
    value = deepcopy(snapshot)
    if value.get("schema") != SCHEMA or value.get("project") != slug or value.get("session") != session:
        return None, "snapshot não corresponde ao projeto/schema"
    windows = value.get("windows")
    if not isinstance(windows, list):
        return None, "snapshot sem topologia legada reconhecível"
    if [window.get("name") for window in windows if isinstance(window, dict)] == [item[0] for item in DEFAULT_VISUALS]:
        return None, "snapshot de seis janelas já aplicado"
    if len(windows) != 2 or any(not isinstance(window, dict) for window in windows):
        return None, "snapshot customizado; nenhuma alteração"
    dev, apoio = windows
    if set(dev) != {"name", "layout", "focus", "panes"} or set(apoio) != {"name", "layout", "focus", "panes"}:
        return None, "snapshot customizado; nenhuma alteração"
    if dev["name"] != "dev" or apoio["name"] != "apoio" or not isinstance(dev["focus"], bool) or not isinstance(apoio["focus"], bool) or dev["focus"] == apoio["focus"]:
        return None, "snapshot customizado; nenhuma alteração"
    if not isinstance(dev["panes"], list) or len(dev["panes"]) != 2 or not isinstance(apoio["panes"], list) or len(apoio["panes"]) != 1:
        return None, "snapshot customizado; nenhuma alteração"
    if [pane.get("name") for pane in dev["panes"] if isinstance(pane, dict)] != ["codigo", "shell"] or [pane.get("name") for pane in apoio["panes"] if isinstance(pane, dict)] != ["shell"]:
        return None, "snapshot customizado; nenhuma alteração"
    panes = [*dev["panes"], *apoio["panes"]]
    if any(not isinstance(pane, dict) or set(pane) not in ({"cwd", "name", "focus"}, {"cwd", "name", "focus", "cwd_translated"}) for pane in panes):
        return None, "snapshot customizado; nenhuma alteração"
    if [pane["focus"] for pane in dev["panes"]] != [True, False] or apoio["panes"][0]["focus"] is not True:
        return None, "snapshot customizado; nenhuma alteração"
    dev_cwd = dev["panes"][0]["cwd"]
    works_cwd = apoio["panes"][0]["cwd"]
    for cwd in (dev_cwd, works_cwd, str(root)):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return None, "snapshot contém cwd inválido"
    layout = _snapshot_layout()
    migrated_windows = []
    for name, _, _ in DEFAULT_VISUALS:
        cwd = dev_cwd if name == "dev1-markscode" else works_cwd if name == "works" else str(root)
        focus = dev["focus"] if name == "dev1-markscode" else apoio["focus"] if name == "works" else False
        migrated_windows.append({"name": name, "layout": layout, "focus": focus, "panes": [{"cwd": cwd, "name": "shell", "focus": True, "cwd_translated": False}]})
    value["windows"] = migrated_windows
    value["cwd_translation"] = {"translated": False, "count": 0}
    return value, "snapshot legado metadata-only reconhecido"


def migrate_default_visuals(paths: Paths, project: str | None = None, *, dry_run: bool = False, yes: bool = False, session_active: Callable[[str], bool] | None = None) -> list[dict[str, str]]:
    if not dry_run and not yes:
        raise WorkError("migração exige --dry-run ou --yes")
    selected = validate_slug(project) if project is not None else None
    candidates = [config_path(paths, selected)] if selected else sorted(paths.config.glob("*.toml"))
    if selected and not candidates[0].exists():
        raise WorkError(f"projeto não configurado: {selected}")
    prepared: list[dict[str, Any]] = []
    results: list[dict[str, str]] = []
    for path in candidates:
        slug = validate_slug(path.stem)
        payload, fingerprint = read_private_bytes(path)
        project_value = parse_project(paths, slug, payload)
        replacement, reason = transform_legacy_default_toml(paths, slug, payload)
        if replacement is None:
            action = "unchanged" if "já aplicada" in reason else "skipped"
            results.append({"project": slug, "path": str(path), "action": action, "reason": reason})
            continue
        snapshot_file = snapshot_path(paths.state, slug)
        snapshot_payload = None
        snapshot_fingerprint = None
        snapshot_replacement = None
        snapshot_reason = "snapshot inexistente"
        if snapshot_file.exists() or snapshot_file.is_symlink():
            snapshot_payload, snapshot_fingerprint = read_private_bytes(snapshot_file)
            if session_active is not None and session_active(project_value.session):
                snapshot_reason = "sessão ativa; snapshot preservado para re-save pelo orquestrador"
            else:
                try:
                    snapshot_value = json.loads(snapshot_payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WorkError(f"snapshot inválido: {snapshot_file}") from exc
                snapshot_migrated, snapshot_reason = transform_legacy_snapshot(snapshot_value, slug, project_value.session, project_value.root)
                if snapshot_migrated is None and "já aplicado" not in snapshot_reason:
                    results.append({"project": slug, "path": str(path), "action": "skipped", "reason": snapshot_reason})
                    continue
                if snapshot_migrated is not None:
                    from .tmux import Tmux

                    migrated_project = parse_project(paths, slug, replacement)
                    Tmux(paths)._validate_snapshot(migrated_project, deepcopy(snapshot_migrated))
                    snapshot_replacement = (json.dumps(snapshot_migrated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        prepared.append({"path": path, "payload": payload, "fingerprint": fingerprint, "replacement": replacement, "snapshot_path": snapshot_file, "snapshot_payload": snapshot_payload, "snapshot_fingerprint": snapshot_fingerprint, "snapshot_replacement": snapshot_replacement})
        results.append({"project": slug, "path": str(path), "action": "would-migrate" if dry_run else "migrated", "reason": reason, "snapshot": snapshot_reason, "workspace": "execute `work workspace save default --include-inactive` após concluir a migração real"})
    if dry_run or not prepared:
        return results
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = paths.state / "backups" / f"recommended-layout-{identifier}"
    ensure_private_directory(backup_root)
    for item in prepared:
        path = item["path"]
        backup = backup_root / path.name
        write_bytes_atomic(backup, item["payload"], exclusive=True)
        if item["snapshot_replacement"] is not None:
            snapshot_backup = backup_root / f"{path.stem}.snapshot.json"
            write_bytes_atomic(snapshot_backup, item["snapshot_payload"], exclusive=True)
            write_bytes_atomic(item["snapshot_path"], item["snapshot_replacement"], expected_fingerprint=item["snapshot_fingerprint"])
        write_bytes_atomic(path, item["replacement"], expected_fingerprint=item["fingerprint"])
        for result in results:
            if result["path"] == str(path):
                result["backup"] = str(backup)
                if item["snapshot_replacement"] is not None:
                    result["snapshot_backup"] = str(backup_root / f"{path.stem}.snapshot.json")
                break
    return results
