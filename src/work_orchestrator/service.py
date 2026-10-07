from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import load_project, load_project_document
from .editor import patch_project
from .errors import WorkError
from .locking import project_lock, session_lock
from .model import Project
from .paths import Paths, validate_slug
from .store import read_snapshot, snapshot_path, write_atomic
from .tmux import Tmux, is_interactive
from .workspace import WorkspaceService


class Service:
    def __init__(self, paths: Paths | None = None, tmux: Tmux | None = None) -> None:
        self.paths = paths or Paths.discover()
        self.paths.ensure()
        self.tmux = tmux or Tmux(self.paths)
        self.workspace = WorkspaceService(self)

    def workspace_save(self, name: str = "default", include_inactive: bool = True) -> Path:
        return self.workspace.save(name, include_inactive)

    def workspace_status(self, name: str = "default") -> dict[str, Any]:
        return self.workspace.status(name)

    def workspace_restore(self, name: str = "default", dry_run: bool = False, visual: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        return self.workspace.restore(name, dry_run, visual, project)

    @contextmanager
    def _locked_project(self, slug: str) -> Iterator[Project]:
        slug = validate_slug(slug)
        with project_lock(self.paths.runtime, slug):
            project = load_project(self.paths, slug)
            with session_lock(self.paths.runtime, self.tmux.socket, project.session):
                yield project

    def start(self, slug: str) -> list[str]:
        with self._locked_project(slug) as project:
            token = self.tmux.ownership_token(project)
            _, skipped = self.tmux.create_from_project(project, token, is_interactive())
            return skipped

    def save(self, slug: str) -> Path:
        with self._locked_project(slug) as project:
            token = self.tmux.ownership_token(project)
            return self._save_locked(project, token)

    def _save_locked(self, project: Project, token: str) -> Path:
        _, snapshot, _ = self.tmux.capture_owned(project, token)
        path = snapshot_path(self.paths.state, project.slug)
        write_atomic(path, snapshot)
        return path

    def restore(self, slug: str) -> None:
        with self._locked_project(slug) as project:
            token = self.tmux.ownership_token(project)
            self.tmux.restore(project, token, read_snapshot(snapshot_path(self.paths.state, project.slug)))

    def open(self, slug: str, *, expected_snapshot_fingerprint: str | None = None, reject_active: bool = False, attach: bool = True, window: str | None = None, visual_id: str | None = None) -> tuple[str, list[str]]:
        with self._locked_project(slug) as project:
            token = self.tmux.ownership_token(project)
            if self.tmux.exists(project.session):
                if reject_active:
                    return "conflict", []
                identity = self.tmux.owned_identity(project, token)
                action = "existing"
                skipped: list[str] = []
            else:
                path = snapshot_path(self.paths.state, project.slug)
                if path.exists() or path.is_symlink():
                    snapshot = read_snapshot(path)
                    if expected_snapshot_fingerprint is not None:
                        from .workspace import _topology_fingerprint

                        if _topology_fingerprint(snapshot) != expected_snapshot_fingerprint:
                            raise WorkError("snapshot mudou desde o save do workspace; nada foi alterado")
                    identity = self.tmux.restore(project, token, snapshot)
                    action = "restored"
                    skipped = []
                else:
                    identity, skipped = self.tmux.create_from_project(project, token, is_interactive())
                    action = "created"
            if attach and is_interactive() and visual_id is not None:
                self.tmux.ensure_visual_group(project, token, visual_id, window)
        if attach and is_interactive():
            if visual_id is None:
                self.tmux.attach_id(project, token, identity, window)
            else:
                self.tmux.attach_visual(project, token, visual_id, window)
        return action, skipped

    def visual_attached(self, slug: str, visual_id: str, window: str | None = None) -> bool:
        return self.visual_claimed(slug, visual_id, window)

    def visual_claimed(self, slug: str, visual_id: str, window: str | None = None) -> bool:
        with self._locked_project(slug) as project:
            token = self.tmux.ownership_token(project)
            return self.tmux.visual_claimed(project, token, visual_id, window)

    def prepare_visual(self, slug: str, visual_id: str, window: str | None = None) -> str:
        with self._locked_project(slug) as project:
            token = self.tmux.ownership_token(project)
            _, group = self.tmux.ensure_visual_group(project, token, visual_id, window)
            return group

    def stop(self, slug: str, yes: bool) -> Path:
        with self._locked_project(slug) as project:
            if not yes:
                if not is_interactive():
                    raise WorkError("stop não interativo exige --yes; nenhuma alteração foi feita")
                try:
                    answer = input(f"Encerrar sessão {project.session}? [s/N] ")
                except (EOFError, KeyboardInterrupt) as exc:
                    raise WorkError("confirmação de encerramento interrompida; nenhuma alteração foi feita") from exc
                if answer.strip().lower() not in {"s", "sim", "y", "yes"}:
                    raise WorkError("encerramento cancelado; nenhuma alteração foi feita")
            token = self.tmux.ownership_token(project)
            identity, snapshot, topology = self.tmux.capture_owned(project, token)
            path = snapshot_path(self.paths.state, project.slug)
            current_identity, current_topology = self.tmux.topology_owned(project, token)
            if current_identity != identity or current_topology != topology:
                raise WorkError("sessão ou topologia divergiu após o snapshot; nada foi encerrado")
            self.tmux.prepare_stop_visual_groups(project, token, identity)
            killed_identity, killed_snapshot, killed_topology = self.tmux.capture_and_kill_owned(project, token, identity)
            if killed_identity != identity or killed_topology != topology:
                raise WorkError("sessão ou topologia divergiu durante o encerramento; snapshot anterior preservado")
            write_atomic(path, killed_snapshot)
            return path

    def edit_details(self, slug: str) -> dict[str, str]:
        slug = validate_slug(slug)
        with project_lock(self.paths.runtime, slug):
            project, _, fingerprint = load_project_document(self.paths, slug)
        return {
            "slug": project.slug,
            "name": project.name,
            "root": str(project.root),
            "session": project.session,
            "command_policy": project.command_policy,
            "fingerprint": fingerprint,
        }

    def edit(
        self,
        slug: str,
        *,
        name: str | None = None,
        root: str | None = None,
        command_policy: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, str]:
        slug = validate_slug(slug)
        with project_lock(self.paths.runtime, slug):
            project, payload, fingerprint = load_project_document(self.paths, slug)
            if expected_fingerprint is not None and expected_fingerprint != fingerprint:
                raise WorkError("configuração mudou desde a leitura; nenhuma alteração foi publicada")
            updated, new_fingerprint = patch_project(
                self.paths,
                project,
                payload,
                fingerprint,
                name=name,
                root=root,
                command_policy=command_policy,
            )
        return {
            "slug": updated.slug,
            "name": updated.name,
            "root": str(updated.root),
            "session": updated.session,
            "command_policy": updated.command_policy,
            "fingerprint": new_fingerprint,
        }

    def status(self, slug: str) -> dict[str, Any]:
        with self._locked_project(slug) as project:
            path = snapshot_path(self.paths.state, project.slug)
            snapshot = read_snapshot(path) if path.exists() or path.is_symlink() else None
            active = self.tmux.exists(project.session)
            owned = False
            session_id = None
            attached = False
            degraded = False
            degraded_reason = None
            cwd_translation = {"translated": False, "count": 0}
            if active:
                try:
                    token = self.tmux.ownership_token(project)
                    session_id = self.tmux.owned_identity(project, token)
                    owned = True
                    attached = self.tmux.has_attached_client(project.session) if hasattr(self.tmux, "has_attached_client") else False
                    if hasattr(self.tmux, "capture_owned"):
                        _, live, _ = self.tmux.capture_owned(project, token)
                        expected = [str(pane.cwd) for window in project.windows for pane in window.panes]
                        actual = [pane for window in live.get("windows", []) for pane in window.get("panes", [])]
                        cwd_translation = {
                            "translated": any(pane.get("cwd_translated") is True for pane in actual),
                            "count": sum(pane.get("cwd_translated") is True for pane in actual),
                        }
                        if len(expected) != len(actual) or any(not pane.get("cwd_translated", False) and wanted != str(pane.get("cwd")) for wanted, pane in zip(expected, actual)):
                            degraded = True
                            degraded_reason = "cwd da sessão diverge da configuração; snapshot anterior preservado"
                except WorkError as exc:
                    degraded = True
                    degraded_reason = str(exc)
        return {
            "slug": project.slug,
            "name": project.name,
            "root": str(project.root),
            "session": project.session,
            "session_id": session_id,
            "active": active,
            "owned": owned,
            "attached": attached,
            "visuals": [
                {
                    "id": visual.id,
                    "kind": visual.kind,
                    "title": visual.title,
                    "root": str(visual.root),
                    "order": visual.order,
                    "attach_target": {"session": visual.session, **({"window": visual.window} if visual.window is not None else {})},
                }
                for visual in project.visuals
            ],
            "state": "degraded" if degraded and owned else ("active" if active and owned else ("conflict" if active else "inactive")),
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "cwd_translation": cwd_translation,
            "snapshot": str(path) if snapshot else None,
            "snapshot_timestamp": snapshot.get("timestamp") if snapshot else None,
            "runtime": self.paths.runtime_diagnostic,
        }

    def workspace_diagnostic(self) -> dict[str, list[dict[str, Any]]]:
        configured = {item.get("session") for item in self.projects() if isinstance(item.get("session"), str)}
        unmanaged: list[dict[str, Any]] = []
        if hasattr(self.tmux, "sessions"):
            for session in self.tmux.sessions():
                if session["name"].startswith("work-") and (session["name"] not in configured or not session.get("owned")):
                    unmanaged.append({"session": session["name"], "state": "unmanaged" if session["name"] not in configured else "conflict"})
        external = self.tmux.external_sessions(self.paths.legacy_socket) if hasattr(self.tmux, "external_sessions") else []
        return {"unmanaged": unmanaged, "external_unmanaged": [{"session": item, "state": "external_unmanaged"} for item in external]}

    def projects(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            paths = sorted(self.paths.config.glob("*.toml"))
        except OSError as exc:
            raise WorkError(f"não foi possível listar projetos: {exc}") from exc
        for path in paths:
            try:
                slug = validate_slug(path.stem)
                items.append(self.status(slug))
            except (WorkError, OSError) as exc:
                items.append({"slug": path.stem, "error": str(exc)})
        return items
