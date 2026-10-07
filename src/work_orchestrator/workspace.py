import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import WorkError
from .locking import _lock
from .paths import Paths, ensure_private_directory, validate_slug
from .store import read_private_bytes, read_snapshot, snapshot_path, write_atomic


WORKSPACE_SCHEMA = 3
MAX_WORKSPACE_BYTES = 1024 * 1024
FORBIDDEN_VISUAL_KEYS = {"pid", "pty", "argv", "token", "content", "command", "environment", "scrollback"}


def validate_workspace_name(value: str) -> str:
    return validate_slug(value)


def workspace_path(state: Path, name: str = "default") -> Path:
    return state / "workspaces" / f"{validate_workspace_name(name)}.json"


def workspace_lock_path(runtime: Path, name: str = "default") -> Path:
    return runtime / "locks" / f"workspace-{validate_workspace_name(name)}.lock"


@contextmanager
def workspace_lock(runtime: Path, name: str = "default") -> Iterator[None]:
    ensure_private_directory(runtime / "locks")
    with _lock(workspace_lock_path(runtime, name)):
        yield


def _topology_fingerprint(snapshot: dict[str, Any]) -> str:
    topology = {
        "windows": [
            {
                "name": window.get("name"),
                "layout": window.get("layout"),
                "focus": window.get("focus"),
                "panes": [
                    {"cwd": pane.get("cwd"), "focus": pane.get("focus")}
                    for pane in window.get("panes", [])
                ],
            }
            for window in snapshot.get("windows", [])
        ]
    }
    payload = json.dumps(topology, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"v2:{hashlib.sha256(payload).hexdigest()}"


def visual_title(slug: str, name: str, visual_id: str, count: int) -> str:
    base = f"Work — {slug}" if name == slug else f"Work — {name} ({slug})"
    return base if count == 1 and visual_id == "main" else f"{base} — {visual_id}"


def visual_document(slug: str, visual_id: str, title: str, root: Path, order: int, session: str, window: str | None = None) -> dict[str, Any]:
    return {
        "id": validate_slug(visual_id),
        "kind": "tilix",
        "order": order,
        "title": title,
        "root": str(root),
        "attach_target": {"session": session, **({"window": window} if window is not None else {})},
    }


def _validate_visual(value: Any, slug: str, session: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"id", "kind", "order", "title", "root", "attach_target"}:
        raise WorkError("visual de workspace inválido")
    if set(value) & FORBIDDEN_VISUAL_KEYS:
        raise WorkError("visual contém campo proibido")
    if value["kind"] != "tilix" or value["id"] != validate_slug(value["id"]) or type(value["order"]) is not int or value["order"] < 0:
        raise WorkError("visual de workspace inválido")
    if not isinstance(value["title"], str) or not value["title"] or any(ord(char) < 32 or ord(char) == 127 for char in value["title"]):
        raise WorkError("título visual inválido")
    root = Path(value["root"])
    if not root.is_absolute():
        raise WorkError("raiz visual deve ser absoluta")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise WorkError(f"raiz visual não existe: {root}") from exc
    if not resolved.is_dir() or str(resolved) != value["root"]:
        raise WorkError("raiz visual inválida")
    target = value["attach_target"]
    if not isinstance(target, dict) or set(target) - {"session", "window"} or target.get("session") != session:
        raise WorkError("attach target visual inválido")
    window = target.get("window")
    if window is not None and (not isinstance(window, str) or not window or any(ord(char) < 32 or ord(char) == 127 for char in window) or any(char in window for char in ".:")):
        raise WorkError("janela visual inválida")
    return value


def _migrate_workspace(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") not in {1, 2, 3}:
        raise WorkError("schema de workspace incompatível")
    if value["schema"] == 3:
        return value
    migrated = dict(value)
    projects = []
    for entry in value.get("projects", []):
        if not isinstance(entry, dict):
            raise WorkError("projeto de workspace inválido")
        item = dict(entry)
        old = item.pop("visual", None)
        if old is None:
            visuals = item.get("visuals", [])
        else:
            visuals = [{
                "id": "main",
                "kind": old.get("kind"),
                "order": old.get("order"),
                "title": old.get("title"),
                "root": old.get("root"),
                "attach_target": {"session": item.get("session")},
            }]
        item["visuals"] = visuals
        projects.append(item)
    migrated["schema"] = 3
    migrated["projects"] = projects
    migrated.setdefault("inactive_projects", [])
    return migrated


def workspace_document(name: str, entries: list[dict[str, Any]], inactive: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": WORKSPACE_SCHEMA,
        "workspace": validate_workspace_name(name),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "projects": entries,
        "inactive_projects": inactive,
    }


def read_workspace(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        raise WorkError(f"workspace inexistente: {path}")
    try:
        payload, _ = read_private_bytes(path, max_bytes=MAX_WORKSPACE_BYTES)
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkError(f"workspace inválido: {path}: {exc}") from exc
    try:
        value = _migrate_workspace(value)
    except WorkError as exc:
        raise WorkError(f"workspace inválido: {path}: {exc}") from exc
    if not isinstance(value.get("workspace"), str) or value["workspace"] != validate_workspace_name(value["workspace"]):
        raise WorkError("nome de workspace inválido")
    for key in ("projects", "inactive_projects"):
        if not isinstance(value.get(key), list):
            raise WorkError(f"workspace inválido: {key}")
    for entry in value["projects"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str) or not isinstance(entry.get("session"), str) or not isinstance(entry.get("visuals"), list) or not entry["visuals"]:
            raise WorkError("projeto de workspace inválido")
        slug = validate_slug(entry["slug"])
        ids: set[str] = set()
        orders: set[int] = set()
        for visual_value in entry["visuals"]:
            visual = _validate_visual(visual_value, slug, entry["session"])
            if visual["id"] in ids or visual["order"] in orders:
                raise WorkError("id ou ordem visual duplicada")
            ids.add(visual["id"])
            orders.add(visual["order"])
    return value


class WorkspaceService:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.paths: Paths = service.paths

    def _entry(self, item: dict[str, Any], order: int) -> dict[str, Any]:
        slug = item["slug"]
        path = snapshot_path(self.paths.state, slug)
        snapshot = read_snapshot(path)
        declared = item.get("visuals") or [{
            "id": "main",
            "kind": "tilix",
            "root": item["root"],
            "order": order,
            "attach_target": {"session": item["session"]},
        }]
        visuals = [
            visual_document(
                slug,
                visual["id"],
                visual.get("title") or visual_title(slug, str(item.get("name") or slug), visual["id"], len(declared)),
                Path(visual["root"]),
                visual["order"],
                visual["attach_target"]["session"],
                visual["attach_target"].get("window"),
            )
            for visual in declared
        ]
        entry = {
            "slug": slug,
            "session": item.get("session"),
            "snapshot": str(path),
            "snapshot_timestamp": snapshot.get("timestamp"),
            "topology_fingerprint": _topology_fingerprint(snapshot),
            "focus": {
                "window": next((index for index, window in enumerate(snapshot.get("windows", [])) if window.get("focus")), None),
                "pane": next((index for window in snapshot.get("windows", []) for index, pane in enumerate(window.get("panes", [])) if pane.get("focus")), None),
            },
            "state": item.get("state", "active"),
            "cwd_translation": item.get("cwd_translation", {"translated": False, "count": 0}),
            "visuals": visuals,
        }
        if item.get("degraded"):
            entry["degraded"] = True
            entry["degraded_reason"] = item.get("degraded_reason")
        return entry

    def save(self, name: str = "default", include_inactive: bool = True, refresh_snapshots: bool = True) -> Path:
        name = validate_workspace_name(name)
        entries: list[dict[str, Any]] = []
        inactive: list[dict[str, Any]] = []
        with workspace_lock(self.paths.runtime, name):
            projects = self.service.projects()
            previous: dict[str, dict[str, Any]] = {}
            existing_path = workspace_path(self.paths.state, name)
            if existing_path.exists() or existing_path.is_symlink():
                previous = {entry["slug"]: entry for entry in read_workspace(existing_path)["projects"]}
            for order, item in enumerate(projects):
                slug = item.get("slug")
                if not isinstance(slug, str):
                    continue
                if item.get("error"):
                    inactive.append({"slug": slug, "configured": True, "state": "invalid", "error": item["error"]})
                    continue
                if item.get("active") and item.get("owned"):
                    if not item.get("visuals") and slug in previous:
                        item = dict(item, visuals=previous[slug]["visuals"])
                    if refresh_snapshots and not item.get("degraded"):
                        try:
                            self.service.save(slug)
                        except WorkError as exc:
                            item = dict(item, degraded=True, state="degraded", degraded_reason=str(exc))
                    try:
                        entries.append(self._entry(item, order))
                    except WorkError as exc:
                        inactive.append({"slug": slug, "session": item.get("session"), "configured": True, "state": "degraded", "error": str(exc)})
                else:
                    state = "conflict" if item.get("active") else "inactive"
                    inactive.append({"slug": slug, "session": item.get("session"), "configured": True, "state": state})
            path = workspace_path(self.paths.state, name)
            write_atomic(path, workspace_document(name, entries, inactive))
            return path

    def status(self, name: str = "default") -> dict[str, Any]:
        path = workspace_path(self.paths.state, name)
        stored: dict[str, Any] = {"projects": [], "inactive_projects": []}
        if path.exists() or path.is_symlink():
            stored = read_workspace(path)
        diagnostic = self.service.workspace_diagnostic()
        return {
            "workspace": validate_workspace_name(name),
            "path": str(path),
            "exists": path.exists() or path.is_symlink(),
            "timestamp": stored.get("timestamp"),
            "projects": stored["projects"],
            "inactive_projects": stored["inactive_projects"],
            "configured": self.service.projects(),
            "unmanaged": diagnostic["unmanaged"],
            "external_unmanaged": diagnostic["external_unmanaged"],
        }

    def restore(self, name: str = "default", dry_run: bool = False, visual: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        name = validate_workspace_name(name)
        if project is not None:
            project = validate_slug(project)
        results: list[dict[str, Any]] = []
        with workspace_lock(self.paths.runtime, name):
            value = read_workspace(workspace_path(self.paths.state, name))
            entries = sorted(value["projects"], key=lambda entry: min(item["order"] for item in entry["visuals"]))
            if project is not None:
                entries = [entry for entry in entries if entry["slug"] == project]
                if not entries:
                    raise WorkError(f"projeto não está no workspace: {project}")
            for entry in entries:
                slug = entry["slug"]
                result: dict[str, Any] = {"slug": slug, "action": "error"}
                try:
                    if entry.get("degraded"):
                        raise WorkError("projeto degraded; snapshot anterior preservado")
                    expected_path = snapshot_path(self.paths.state, slug)
                    if not Path(entry["snapshot"]).is_absolute() or Path(entry["snapshot"]) != expected_path:
                        raise WorkError("snapshot fora do caminho canônico")
                    snapshot = read_snapshot(expected_path)
                    fingerprint = _topology_fingerprint(snapshot)
                    if entry.get("topology_fingerprint") != fingerprint:
                        result["action"] = "conflict"
                        result["error"] = "snapshot mudou desde o save do workspace; nada foi alterado"
                        results.append(result)
                        continue
                    current = self.service.status(slug)
                    if current.get("active") and not current.get("owned"):
                        result["action"] = "conflict"
                        result["error"] = "sessão ativa sem ownership; nada foi alterado"
                        results.append(result)
                        continue
                    result["action"] = "would-confirm" if current.get("active") else "would-restore"
                    if not dry_run:
                        action, _ = self.service.open(slug, expected_snapshot_fingerprint=fingerprint, attach=False)
                        result["action"] = action
                    if visual in {"terminator", "terminator-tabs", "terminator-windows"}:
                        result["visuals"] = []
                        visual_values = []
                        for visual_value in sorted(entry["visuals"], key=lambda item: item["order"]):
                            visual_value = _validate_visual(visual_value, slug, entry["session"])
                            window = visual_value["attach_target"].get("window")
                            existing = self.service.visual_claimed(slug, visual_value["id"], window)
                            visual_result = {"id": visual_value["id"], "window": window}
                            if dry_run:
                                visual_result["action"] = "confirmed" if existing else "would-open"
                            elif existing:
                                visual_result["action"] = "confirmed"
                            elif visual == "terminator-tabs":
                                visual_values.append((visual_value, visual_result, window))
                            else:
                                from .gui import open_terminator

                                self.service.prepare_visual(slug, visual_value["id"], window)
                                opened = open_terminator(
                                    self.paths,
                                    slug,
                                    str(current.get("name") or slug),
                                    Path(visual_value["root"]),
                                    visual_id=visual_value["id"],
                                    title=visual_value["title"],
                                    window=window,
                                    tmux=self.service.tmux,
                                    session=entry["session"],
                                    workspace=name,
                                    check_existing=False,
                                )
                                visual_result["action"] = "opened" if opened else "confirmed"
                            result["visuals"].append(visual_result)
                        if not dry_run and visual in {"terminator", "terminator-tabs"} and visual_values:
                            from .gui import open_terminator_tabs

                            for visual_value, _, window in visual_values:
                                self.service.prepare_visual(slug, visual_value["id"], window)
                            tabs = [
                                {
                                    "id": visual_value["id"],
                                    "title": visual_value["title"],
                                    "root": str(Path(visual_value["root"]).expanduser().resolve(strict=True)),
                                    "command": "",
                                    "window": window,
                                }
                                for visual_value, _, window in visual_values
                            ]
                            opened = open_terminator_tabs(
                                self.paths,
                                slug,
                                str(current.get("name") or slug),
                                tabs,
                                workspace=name,
                            )
                            for _, visual_result, _ in visual_values:
                                visual_result["action"] = "opened" if opened else "confirmed"
                except (WorkError, OSError, KeyError, TypeError) as exc:
                    result["error"] = str(exc)
                results.append(result)
        return results
