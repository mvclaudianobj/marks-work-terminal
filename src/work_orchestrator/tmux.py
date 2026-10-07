import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import WorkError
from .model import Pane, Project
from .paths import Paths, validate_slug
from .redaction import sanitize_pane_title
from .store import SCHEMA, read_private_text, token_path, write_bytes_atomic


SESSION_RECORD = re.compile(r"\A(\$[0-9]+) (@[0-9]+) (%[0-9]+)\Z")
WINDOW_RECORD = re.compile(r"\A(@[0-9]+) (%[0-9]+)\Z")
SESSION_ID = re.compile(r"\A\$[0-9]+\Z")
WINDOW_ID = re.compile(r"\A@[0-9]+\Z")
PANE_ID = re.compile(r"\A%[0-9]+\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
GROUP_NAME = re.compile(r"\A[a-zA-Z0-9_-]{1,96}\Z")
TIMEOUT = 10
FIELD_SEPARATOR = "\t"
LAYOUT_LEAF = re.compile(r"(\d+x\d+,\d+,\d+,)(\d+)(?=,|\}|\]|$)")
CAPTURE_FORMAT = FIELD_SEPARATOR.join(
    (
        "#{session_id}",
        "#{@work-orchestrator-project}",
        "#{@work-orchestrator-token}",
        "#{window_id}",
        "#{window_index}",
        "#{window_name}",
        "#{window_layout}",
        "#{window_active}",
        "#{pane_id}",
        "#{pane_index}",
        "#{pane_current_path}",
        "#{pane_title}",
        "#{pane_active}",
        "#{pane_width}",
        "#{pane_height}",
    )
)


class Tmux:
    def __init__(self, paths: Paths) -> None:
        override = os.environ.get("WORK_TMUX_SOCKET")
        if override is not None:
            if os.environ.get("WORK_ORCHESTRATOR_TESTING") != "1":
                raise WorkError("WORK_TMUX_SOCKET só é aceito em modo de teste")
            socket = Path(override)
            if not socket.is_absolute() or socket.parent.resolve() != paths.runtime.resolve():
                raise WorkError("WORK_TMUX_SOCKET de teste deve ser absoluto e estar no runtime privado")
        else:
            socket = paths.socket
        self.socket = socket
        self.base = ["tmux", "-S", str(socket)]
        self.paths = paths

    def _validate_socket(self) -> None:
        try:
            details = self.socket.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkError(f"socket tmux inacessível: {self.socket}: {exc}") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISSOCK(details.st_mode):
            raise WorkError(f"socket tmux inseguro: {self.socket}")
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
            raise WorkError(f"owner ou permissões inseguros no socket tmux: {self.socket}")

    def run(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
        self._validate_socket()
        try:
            result = subprocess.run(
                [*self.base, *args],
                check=False,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise WorkError("tmux não encontrado no PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkError(f"tmux excedeu o timeout de {TIMEOUT}s") from exc
        except OSError as exc:
            raise WorkError(f"não foi possível executar tmux: {exc}") from exc
        self._validate_socket()
        if check and result.returncode:
            detail = (result.stderr or "erro desconhecido").strip()
            raise WorkError(f"tmux falhou: {detail}")
        return result

    def exists(self, session: str) -> bool:
        return self.run("has-session", "-t", f"={session}", check=False).returncode == 0

    def sessions(self) -> list[dict[str, Any]]:
        result = self.run("list-sessions", "-F", "#{session_name}\t#{@work-orchestrator-project}\t#{@work-orchestrator-visual}", check=False)
        if result.returncode:
            return []
        sessions: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or any(CONTROL.search(field) for field in fields):
                raise WorkError("tmux retornou diagnóstico de sessões inválido")
            if fields[2]:
                continue
            sessions.append({"name": fields[0], "owned": bool(fields[1])})
        return sessions

    def external_sessions(self, socket: Path) -> list[str]:
        if socket == self.socket or not socket.exists():
            return []
        try:
            details = socket.lstat()
        except OSError:
            return []
        if not stat.S_ISSOCK(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
            return []
        try:
            result = subprocess.run(
                ["tmux", "-S", str(socket), "list-sessions", "-F", "#{session_name}"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        values = result.stdout.splitlines() if result.returncode == 0 else []
        return [value for value in values if value.startswith("work-") and not CONTROL.search(value)]

    def client_targets(self, session: str) -> list[str]:
        result = self.run("list-clients", "-F", "#{client_session}\t#{window_name}", check=False)
        if result.returncode:
            return []
        targets: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 2 or not fields[0] or not fields[1] or any(CONTROL.search(field) for field in fields):
                raise WorkError("tmux retornou cliente inválido")
            if fields[0] == session:
                targets.append(fields[1])
        return targets

    def has_attached_client(self, session: str, window: str | None = None) -> bool:
        targets = self.client_targets(session)
        return bool(targets) if window is None else window in targets

    def visual_group_name(self, project: Project, visual_id: str) -> str:
        if validate_slug(visual_id) != visual_id:
            raise WorkError("id visual inválido")
        stem = re.sub(r"[^A-Za-z0-9_-]", "-", project.session).strip("-") or "session"
        digest = hashlib.sha256(f"{project.session}\0{visual_id}".encode("utf-8")).hexdigest()[:12]
        value = f"{stem[:48]}-visual-{visual_id[:20]}-{digest}"
        if GROUP_NAME.fullmatch(value) is None:
            raise WorkError("nome de grupo visual inválido")
        return value

    def _visual_proof(self, token: str, parent_id: str, visual_id: str, group: str) -> str:
        return hashlib.sha256(f"{token}\0{parent_id}\0{visual_id}\0{group}".encode("utf-8")).hexdigest()

    def _visual_identity(self, project: Project, token: str, visual_id: str) -> tuple[str, str]:
        parent_id = self.owned_identity(project, token)
        group = self.visual_group_name(project, visual_id)
        if not self.exists(group):
            raise WorkError(f"grupo visual não está ativo: {group}")
        group_id = self.session_id(group)
        expected = {
            "@work-orchestrator-visual": visual_id,
            "@work-orchestrator-project": project.slug,
            "@work-orchestrator-parent-session-id": parent_id,
            "@work-orchestrator-parent-token-proof": self._visual_proof(token, parent_id, visual_id, group),
        }
        if any(not secrets.compare_digest(self._option(group_id, key), value) for key, value in expected.items()):
            raise WorkError(f"grupo visual recusado por ownership inválido: {group}")
        return group_id, group

    def ensure_visual_group(self, project: Project, token: str, visual_id: str, window: str | None = None) -> tuple[str, str]:
        parent_id = self.owned_identity(project, token)
        group = self.visual_group_name(project, visual_id)
        if self.exists(group):
            group_id, _ = self._visual_identity(project, token, visual_id)
            self._install_detach_hook(group_id, project.slug)
        else:
            result = self.run("new-session", "-d", "-P", "-F", "#{session_id}", "-t", parent_id, "-s", group)
            group_id = result.stdout.rstrip("\n")
            if SESSION_ID.fullmatch(group_id) is None:
                raise WorkError("tmux retornou ID de grupo visual inválido")
            try:
                self.run("set-option", "-t", group_id, "@work-orchestrator-visual", visual_id)
                self.run("set-option", "-t", group_id, "@work-orchestrator-project", project.slug)
                self.run("set-option", "-t", group_id, "@work-orchestrator-parent-session-id", parent_id)
                self.run("set-option", "-t", group_id, "@work-orchestrator-parent-token-proof", self._visual_proof(token, parent_id, visual_id, group))
                self._install_detach_hook(group_id, project.slug)
            except BaseException:
                self.run("kill-session", "-t", group_id, check=False)
                raise
        window_id = self._window_id(parent_id, window)
        self.run("select-window", "-t", f"{group_id}:{window_id}")
        return group_id, group

    def visual_client_attached(self, project: Project, token: str, visual_id: str, window: str | None = None) -> bool:
        return self.visual_claimed(project, token, visual_id, window)

    def visual_claimed(self, project: Project, token: str, visual_id: str, window: str | None = None) -> bool:
        validate_slug(visual_id)
        try:
            parent_id = self.owned_identity(project, token)
        except WorkError:
            return False
        result = self.run(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_id}\t#{session_group}\t#{@work-orchestrator-project}\t#{@work-orchestrator-visual}\t#{@work-orchestrator-parent-session-id}\t#{@work-orchestrator-parent-token-proof}",
            check=False,
        )
        if result.returncode:
            return False
        records: list[tuple[str, str, str, str, str, str, str]] = []
        parent_group = ""
        for line in result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 7 or any(CONTROL.search(field) for field in fields):
                raise WorkError("tmux retornou diagnóstico de claims visuais inválido")
            record = tuple(fields)
            if fields[1] == parent_id:
                parent_group = fields[2]
            if fields[3] == project.slug and fields[4] == visual_id:
                records.append(record)
        if len(records) != 1:
            return False
        name, identity, group_marker, slug, marked_visual, marked_parent, proof = records[0]
        expected_name = self.visual_group_name(project, visual_id)
        expected_proof = self._visual_proof(token, parent_id, visual_id, expected_name)
        if (
            name != expected_name
            or identity == parent_id
            or not group_marker
            or group_marker != parent_group
            or slug != project.slug
            or marked_visual != visual_id
            or marked_parent != parent_id
            or not secrets.compare_digest(proof, expected_proof)
        ):
            return False
        parent_windows = self.run("list-windows", "-t", parent_id, "-F", "#{window_id}", check=False)
        group_windows = self.run("list-windows", "-t", identity, "-F", "#{window_id}", check=False)
        if parent_windows.returncode or group_windows.returncode or parent_windows.stdout != group_windows.stdout:
            return False
        return self.has_attached_client(name, window)

    def prepare_stop_visual_groups(self, project: Project, token: str, parent_id: str) -> None:
        result = self.run("list-sessions", "-F", "#{session_name}\t#{session_id}\t#{@work-orchestrator-visual}\t#{@work-orchestrator-project}\t#{@work-orchestrator-parent-session-id}", check=False)
        if result.returncode:
            return
        groups: list[tuple[str, str, str]] = []
        for line in result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 5 or any(CONTROL.search(field) for field in fields):
                raise WorkError("tmux retornou diagnóstico de grupos inválido")
            name, identity, visual_id, slug, marked_parent = fields
            if not visual_id or slug != project.slug:
                continue
            if marked_parent != parent_id:
                raise WorkError(f"grupo visual stale ou parent divergente: {name}")
            owned_id, owned_name = self._visual_identity(project, token, visual_id)
            if owned_id != identity or owned_name != name:
                raise WorkError(f"grupo visual divergente: {name}")
            if self.has_attached_client(name):
                raise WorkError(f"grupo visual possui cliente anexado: {name}; nada foi encerrado")
            groups.append((name, identity, visual_id))
        for name, identity, visual_id in groups:
            current_id, current_name = self._visual_identity(project, token, visual_id)
            if current_id != identity or current_name != name or self.has_attached_client(name):
                raise WorkError(f"grupo visual divergiu antes da limpeza: {name}")
            self.run("kill-session", "-t", identity)

    def session_id(self, session: str) -> str:
        result = self.run("list-sessions", "-F", "#{session_name}\t#{session_id}")
        matches = []
        for line in result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 2 or CONTROL.search(fields[0]) or SESSION_ID.fullmatch(fields[1]) is None:
                raise WorkError("tmux retornou lista de sessões inválida")
            if fields[0] == session:
                matches.append(fields[1])
        if len(matches) != 1:
            raise WorkError(f"sessão inexistente ou ambígua: {session}")
        value = matches[0]
        if not SESSION_ID.fullmatch(value):
            raise WorkError("tmux retornou ID de sessão inválido")
        return value

    def ownership_token(self, project: Project) -> str:
        path = token_path(self.paths.state, project.slug)
        try:
            token = read_private_text(path).strip()
        except WorkError as exc:
            if path.exists() or path.is_symlink():
                raise
            token = secrets.token_urlsafe(32)
            try:
                write_bytes_atomic(path, (token + "\n").encode("ascii"), exclusive=True)
            except WorkError:
                token = read_private_text(path).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
            raise WorkError(f"token de ownership inválido: {path}")
        return token

    def owned_identity(self, project: Project, token: str) -> str:
        if not self.exists(project.session):
            raise WorkError(f"sessão não está ativa: {project.session}")
        identity = self.session_id(project.session)
        marked_project = self._option(identity, "@work-orchestrator-project")
        marked_token = self._option(identity, "@work-orchestrator-token")
        if marked_project != project.slug or not secrets.compare_digest(marked_token, token):
            raise WorkError(f"sessão recusada por ownership inválido: {project.session}")
        self._install_detach_hook(identity, project.slug)
        return identity

    def _option(self, target: str, option: str) -> str:
        result = self.run("show-options", "-qv", "-t", target, option, check=False)
        if result.returncode:
            return ""
        return result.stdout.rstrip("\n")

    def _mark(self, identity: str, project: Project, token: str) -> None:
        self.run("set-option", "-t", identity, "@work-orchestrator-project", project.slug)
        self.run("set-option", "-t", identity, "@work-orchestrator-token", token)

    def _install_detach_hook(self, identity: str, slug: str) -> None:
        if SESSION_ID.fullmatch(identity) is None:
            raise WorkError("ID de sessão inválido para hook detach")
        slug = validate_slug(slug)
        executable = (Path(__file__).resolve().parents[2] / "bin" / "work-hook-detach").resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise WorkError(f"executável hook detach não encontrado no checkout: {executable}")
        command = shlex.join([str(executable), slug])
        self.run("set-hook", "-t", identity, "client-detached", shlex.join(["run-shell", command]))

    def _command_allowed(self, pane: Pane, interactive: bool) -> bool:
        if pane.command is None or pane.policy == "never":
            return False
        if pane.policy == "always":
            return True
        if not interactive:
            return False
        rendered = shlex.join(pane.command)
        try:
            answer = input(f"Executar comando configurado [{rendered}]? [s/N] ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise WorkError("confirmação de comando interrompida") from exc
        return answer.strip().lower() in {"s", "sim", "y", "yes"}

    def create_from_project(self, project: Project, token: str, interactive: bool) -> tuple[str, list[str]]:
        if self.exists(project.session):
            raise WorkError(f"sessão já existe: {project.session}")
        skipped: list[str] = []
        first = project.windows[0]
        result = self.run(
            "new-session", "-d", "-P", "-F", "#{session_id} #{window_id} #{pane_id}",
            "-s", project.session, "-n", first.name, "-c", str(first.panes[0].cwd),
        )
        match = SESSION_RECORD.fullmatch(result.stdout.rstrip("\n"))
        if match is None:
            raise WorkError("tmux retornou IDs inválidos ao criar sessão")
        identity, first_window, first_pane = match.groups()
        window_ids: list[str] = []
        try:
            self._mark(identity, project, token)
            self._install_detach_hook(identity, project.slug)
            for wi, window in enumerate(project.windows):
                if wi == 0:
                    window_id, initial_pane = first_window, first_pane
                else:
                    created = self.run(
                        "new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}",
                        "-t", identity, "-n", window.name, "-c", str(window.panes[0].cwd),
                    )
                    parsed = WINDOW_RECORD.fullmatch(created.stdout.rstrip("\n"))
                    if parsed is None:
                        raise WorkError("tmux retornou IDs inválidos ao criar janela")
                    window_id, initial_pane = parsed.groups()
                window_ids.append(window_id)
                pane_ids = [initial_pane, *([""] * (len(window.panes) - 1))]
                for pi in range(len(window.panes) - 1, 0, -1):
                    pane = window.panes[pi]
                    pane_id = self.run(
                        "split-window", "-d", "-P", "-F", "#{pane_id}",
                        "-t", initial_pane, "-c", str(pane.cwd),
                    ).stdout.rstrip("\n")
                    if not PANE_ID.fullmatch(pane_id):
                        raise WorkError("tmux retornou ID de pane inválido")
                    pane_ids[pi] = pane_id
                if window.layout:
                    self.run("select-layout", "-t", window_id, window.layout)
                for pi, pane in enumerate(window.panes):
                    pane_id = pane_ids[pi]
                    if pane.name:
                        self.run("select-pane", "-t", pane_id, "-T", pane.name)
                    if self._command_allowed(pane, interactive):
                        self._exec_in_pane(pane_id, pane)
                    elif pane.command and pane.policy == "prompt" and not interactive:
                        skipped.append(f"{window.name}.{pi}")
                focused = next(index for index, pane in enumerate(window.panes) if pane.focus)
                self.run("select-pane", "-t", pane_ids[focused])
            focused_window = next(index for index, window in enumerate(project.windows) if window.focus)
            self.run("select-window", "-t", window_ids[focused_window])
        except BaseException:
            self._kill_if_same(identity)
            raise
        return identity, skipped

    def _exec_in_pane(self, pane_id: str, pane: Pane) -> None:
        payload = base64.urlsafe_b64encode(json.dumps(pane.command).encode("utf-8")).decode("ascii")
        runner = str(Path(__file__).with_name("_exec.py"))
        self.run("set-option", "-p", "-t", pane_id, "remain-on-exit", "on")
        self.run("respawn-pane", "-k", "-t", pane_id, "-c", str(pane.cwd), "--", sys.executable, runner, payload)

    def capture_owned(self, project: Project, token: str) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
        result = self.run("list-panes", "-s", "-t", f"={project.session}", "-F", CAPTURE_FORMAT)
        return self._parse_capture(project, token, result.stdout)

    def capture_and_kill_owned(
        self, project: Project, token: str, identity: str
    ) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
        if not SESSION_ID.fullmatch(identity):
            raise WorkError("ID de sessão inválido")
        result = self.run(
            "list-panes", "-s", "-t", identity, "-F", CAPTURE_FORMAT,
            ";", "kill-session", "-t", identity,
        )
        return self._parse_capture(project, token, result.stdout)

    def _parse_capture(
        self, project: Project, token: str, output: str
    ) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
        records: list[tuple[str, ...]] = []
        for line in output.splitlines():
            fields = tuple(line.split(FIELD_SEPARATOR))
            if len(fields) != 15 or any(CONTROL.search(field) for index, field in enumerate(fields) if index != 11):
                raise WorkError("tmux retornou captura inválida")
            records.append(fields)
        if not records:
            raise WorkError("tmux retornou captura vazia")
        identity = records[0][0]
        if not SESSION_ID.fullmatch(identity):
            raise WorkError("tmux retornou ID de sessão inválido")
        if any(record[0] != identity or record[1] != project.slug or not secrets.compare_digest(record[2], token) for record in records):
            raise WorkError(f"sessão recusada por ownership inválido: {project.session}")
        grouped: dict[str, list[tuple[str, ...]]] = {}
        for record in records:
            if not WINDOW_ID.fullmatch(record[3]) or not PANE_ID.fullmatch(record[8]):
                raise WorkError("tmux retornou IDs inválidos na captura")
            try:
                int(record[4])
                int(record[9])
            except ValueError as exc:
                raise WorkError("tmux retornou índices inválidos na captura") from exc
            grouped.setdefault(record[3], []).append(record)
        windows: list[dict[str, Any]] = []
        topology: list[Any] = []
        ordered_groups = sorted(grouped.items(), key=lambda item: int(item[1][0][4]))
        for window_id, rows in ordered_groups:
            rows.sort(key=lambda row: int(row[9]))
            first = rows[0]
            if any(row[4:8] != first[4:8] for row in rows):
                raise WorkError("tmux retornou janela inconsistente na captura")
            name = self._safe_text(first[5], "nome da janela")
            active = self._flag(first[7], "foco da janela")
            panes: list[dict[str, Any]] = []
            pane_topology: list[Any] = []
            pane_ids = [row[8] for row in rows]
            layout = self._normalize_layout(first[6], pane_ids)
            for row in rows:
                pane_id = row[8]
                cwd_text = self._safe_text(row[10], "cwd do pane")
                title = sanitize_pane_title(row[11])
                pane_active = self._flag(row[12], "foco do pane")
                cwd_translated = False
                try:
                    cwd = Path(cwd_text).resolve(strict=True)
                except OSError as exc:
                    alias = dict(project.cwd_aliases).get(cwd_text)
                    if alias is None:
                        raise WorkError(f"cwd de pane não existe: {cwd_text}") from exc
                    try:
                        cwd = alias.resolve(strict=True)
                        cwd.relative_to(project.root)
                    except (OSError, ValueError) as alias_exc:
                        raise WorkError(f"alias de cwd não é mais válido: {cwd_text}") from alias_exc
                    if not cwd.is_dir():
                        raise WorkError(f"destino de alias de cwd não é diretório: {cwd}")
                    cwd_translated = True
                if not cwd.is_dir():
                    raise WorkError(f"cwd de pane não é diretório: {cwd}")
                panes.append({"cwd": str(cwd), "name": title, "focus": pane_active, "cwd_translated": cwd_translated})
                try:
                    width, height = int(row[13]), int(row[14])
                except ValueError as exc:
                    raise WorkError("tmux retornou dimensões inválidas na captura") from exc
                if width <= 0 or height <= 0:
                    raise WorkError("tmux retornou dimensões inválidas na captura")
                pane_topology.append((pane_id, str(cwd), title, pane_active, width, height))
            if sum(pane["focus"] for pane in panes) != 1:
                raise WorkError("tmux retornou foco de panes inconsistente")
            windows.append({"name": name, "layout": layout, "focus": active, "panes": panes})
            topology.append((window_id, name, layout, active, tuple(pane_topology)))
        if not windows or sum(window["focus"] for window in windows) != 1:
            raise WorkError("tmux retornou foco de janelas inconsistente")
        snapshot = {
            "schema": SCHEMA,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session": project.session,
            "project": project.slug,
            "cwd_translation": {
                "translated": any(pane["cwd_translated"] for window in windows for pane in window["panes"]),
                "count": sum(pane["cwd_translated"] for window in windows for pane in window["panes"]),
            },
            "windows": windows,
        }
        return identity, snapshot, tuple(topology)

    @staticmethod
    def parse_monitor_fields(fields: tuple[str, ...], now: int) -> dict[str, Any]:
        if len(fields) != 9 or any(CONTROL.search(field) for field in fields):
            raise WorkError("tmux retornou metadados de monitor inválidos")
        if not SESSION_ID.fullmatch(fields[0]) or not PANE_ID.fullmatch(fields[3]):
            raise WorkError("tmux retornou identificadores de monitor inválidos")
        if fields[4] not in {"0", "1"}:
            raise WorkError("tmux retornou pane_dead inválido")
        try:
            dead_status = int(fields[5]) if fields[5] else 0
            activity = int(fields[6]) if fields[6] else now
            width = int(fields[7]) if fields[7] else 0
            height = int(fields[8]) if fields[8] else 0
        except ValueError as exc:
            raise WorkError("tmux retornou metadados numéricos inválidos") from exc
        if dead_status < 0 or activity < 0 or width < 0 or height < 0:
            raise WorkError("tmux retornou metadados numéricos inválidos")
        return {
            "pane": fields[3],
            "pane_dead": fields[4] == "1",
            "pane_dead_status": dead_status,
            "activity_timestamp": activity,
            "pane_width": width,
            "pane_height": height,
        }

    def topology_owned(self, project: Project, token: str) -> tuple[str, tuple[Any, ...]]:
        identity, _, topology = self.capture_owned(project, token)
        return identity, topology

    def restore(self, project: Project, token: str, snapshot: dict[str, Any]) -> str:
        if self.exists(project.session):
            raise WorkError(f"sessão já existe: {project.session}")
        windows = self._validate_snapshot(project, snapshot)
        first = windows[0]
        first_panes = first["panes"]
        result = self.run(
            "new-session", "-d", "-P", "-F", "#{session_id} #{window_id} #{pane_id}",
            "-s", project.session, "-n", first["name"], "-c", first_panes[0]["cwd"],
        )
        match = SESSION_RECORD.fullmatch(result.stdout.rstrip("\n"))
        if match is None:
            raise WorkError("tmux retornou IDs inválidos ao restaurar sessão")
        identity, first_window, first_pane = match.groups()
        window_ids: list[str] = []
        try:
            self._mark(identity, project, token)
            self._install_detach_hook(identity, project.slug)
            for wi, window in enumerate(windows):
                panes = window["panes"]
                if wi == 0:
                    window_id, initial_pane = first_window, first_pane
                else:
                    created = self.run(
                        "new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}",
                        "-t", identity, "-n", window["name"], "-c", panes[0]["cwd"],
                    )
                    parsed = WINDOW_RECORD.fullmatch(created.stdout.rstrip("\n"))
                    if parsed is None:
                        raise WorkError("tmux retornou IDs inválidos ao restaurar janela")
                    window_id, initial_pane = parsed.groups()
                window_ids.append(window_id)
                pane_ids = [initial_pane, *([""] * (len(panes) - 1))]
                for pi in range(len(panes) - 1, 0, -1):
                    pane = panes[pi]
                    pane_id = self.run(
                        "split-window", "-d", "-P", "-F", "#{pane_id}",
                        "-t", initial_pane, "-c", pane["cwd"],
                    ).stdout.rstrip("\n")
                    if not PANE_ID.fullmatch(pane_id):
                        raise WorkError("tmux retornou ID de pane inválido ao restaurar")
                    pane_ids[pi] = pane_id
                self.run("select-layout", "-t", window_id, self._restore_layout(window["layout"], pane_ids))
                for pi, pane in enumerate(panes):
                    if pane["name"]:
                        self.run("select-pane", "-t", pane_ids[pi], "-T", pane["name"])
                focused = next(index for index, pane in enumerate(panes) if pane["focus"])
                self.run("select-pane", "-t", pane_ids[focused])
            focused_window = next(index for index, window in enumerate(windows) if window["focus"])
            self.run("select-window", "-t", window_ids[focused_window])
        except BaseException:
            self._kill_if_same(identity)
            raise
        return identity

    def _validate_snapshot(self, project: Project, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        if snapshot.get("project") != project.slug or snapshot.get("session") != project.session:
            raise WorkError("snapshot não corresponde ao projeto/sessão configurado")
        translation = snapshot.get("cwd_translation", {"translated": False, "count": 0})
        if not isinstance(translation, dict) or set(translation) != {"translated", "count"} or type(translation["translated"]) is not bool or type(translation["count"]) is not int or translation["count"] < 0:
            raise WorkError("diagnóstico de tradução de cwd inválido no snapshot")
        windows = snapshot.get("windows")
        if not isinstance(windows, list) or not windows:
            raise WorkError("snapshot não contém janelas")
        names: set[str] = set()
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"name", "layout", "focus", "panes"}:
                raise WorkError("estrutura de janela inválida no snapshot")
            name = window["name"]
            if not isinstance(name, str) or not name or CONTROL.search(name) or any(char in name for char in ".:") or name in names:
                raise WorkError("nome de janela inválido no snapshot")
            names.add(name)
            if not isinstance(window["layout"], str) or not window["layout"] or CONTROL.search(window["layout"]):
                raise WorkError("layout inválido no snapshot")
            if not isinstance(window["focus"], bool):
                raise WorkError("foco de janela inválido no snapshot")
            panes = window["panes"]
            if not isinstance(panes, list) or not panes:
                raise WorkError("panes inválidos no snapshot")
            for pane in panes:
                if not isinstance(pane, dict) or set(pane) not in ({"cwd", "name", "focus"}, {"cwd", "name", "focus", "cwd_translated"}):
                    raise WorkError("estrutura de pane inválida no snapshot")
                pane["cwd"] = str(self._snapshot_cwd(pane["cwd"]))
                translated = pane.get("cwd_translated", False)
                if type(translated) is not bool:
                    raise WorkError("diagnóstico de tradução de cwd inválido no snapshot")
                pane["cwd_translated"] = translated
                if not isinstance(pane["name"], str) or CONTROL.search(pane["name"]):
                    raise WorkError("nome de pane inválido no snapshot")
                if not isinstance(pane["focus"], bool):
                    raise WorkError("foco de pane inválido no snapshot")
            if sum(pane["focus"] for pane in panes) != 1:
                raise WorkError("foco de panes inválido no snapshot")
            self._validate_normalized_layout(window["layout"], len(panes))
        if sum(window["focus"] for window in windows) != 1:
            raise WorkError("foco de janelas inválido no snapshot")
        translated_count = sum(pane["cwd_translated"] for window in windows for pane in window["panes"])
        if translation != {"translated": translated_count > 0, "count": translated_count}:
            raise WorkError("diagnóstico de tradução de cwd inconsistente no snapshot")
        return windows

    def _normalize_layout(self, layout: str, pane_ids: list[str]) -> str:
        mapping = {pane_id.removeprefix("%"): str(index) for index, pane_id in enumerate(pane_ids)}
        return self._map_layout(layout, mapping)

    def _restore_layout(self, layout: str, pane_ids: list[str]) -> str:
        mapping = {str(index): pane_id.removeprefix("%") for index, pane_id in enumerate(pane_ids)}
        return self._map_layout(layout, mapping)

    def _validate_normalized_layout(self, layout: str, count: int) -> None:
        if "," not in layout or layout[:4] != self._layout_checksum(layout[5:]):
            raise WorkError("checksum de layout inválido no snapshot")
        identifiers = [match.group(2) for match in LAYOUT_LEAF.finditer(layout[5:])]
        if len(identifiers) != count or set(identifiers) != {str(index) for index in range(count)}:
            raise WorkError("ordem de panes inválida no layout do snapshot")

    def _map_layout(self, layout: str, mapping: dict[str, str]) -> str:
        if len(layout) < 6 or layout[4] != ",":
            raise WorkError("layout tmux inválido")
        body = layout[5:]
        found: list[str] = []

        def replace(match: re.Match[str]) -> str:
            identifier = match.group(2)
            found.append(identifier)
            if identifier not in mapping:
                raise WorkError("layout tmux não corresponde aos panes")
            return match.group(1) + mapping[identifier]

        remapped = LAYOUT_LEAF.sub(replace, body)
        if len(found) != len(mapping) or set(found) != set(mapping):
            raise WorkError("layout tmux não corresponde aos panes")
        return f"{self._layout_checksum(remapped)},{remapped}"

    def _layout_checksum(self, body: str) -> str:
        checksum = 0
        for value in body.encode("utf-8"):
            checksum = ((checksum >> 1) | ((checksum & 1) << 15)) + value
        return f"{checksum & 0xffff:04x}"

    def _snapshot_cwd(self, value: Any) -> Path:
        if not isinstance(value, str):
            raise WorkError("cwd inválido no snapshot")
        path = Path(value)
        if not path.is_absolute():
            raise WorkError("cwd relativo recusado no snapshot")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise WorkError(f"cwd do snapshot não existe: {path}") from exc
        if not resolved.is_dir():
            raise WorkError(f"cwd do snapshot não é diretório: {resolved}")
        return resolved

    def _ids(self, target: str, command: str, format_value: str, pattern: re.Pattern[str], label: str) -> list[str]:
        result = self.run(command, "-t", target, "-F", format_value)
        values = result.stdout.splitlines()
        if not values or any(pattern.fullmatch(value) is None for value in values):
            raise WorkError(f"tmux retornou lista de IDs de {label} inválida")
        return values

    def _field(self, target: str, format_value: str, label: str) -> str:
        result = self.run("display-message", "-p", "-t", target, format_value)
        value = result.stdout
        if not value.endswith("\n"):
            raise WorkError(f"tmux retornou {label} sem terminação válida")
        return value[:-1]

    def _safe_text(self, value: str, label: str) -> str:
        if CONTROL.search(value):
            raise WorkError(f"tmux retornou controles em {label}")
        return value

    def _flag(self, value: str, label: str) -> bool:
        if value not in {"0", "1"}:
            raise WorkError(f"tmux retornou {label} inválido")
        return value == "1"

    def _kill_if_same(self, identity: str) -> None:
        self.run("kill-session", "-t", identity, check=False)

    def attach_id(self, project: Project, token: str, identity: str, window: str | None = None) -> None:
        if self.owned_identity(project, token) != identity:
            raise WorkError("sessão divergiu antes do attach")
        target = identity
        if window is not None:
            if not isinstance(window, str) or not window or CONTROL.search(window) or any(char in window for char in ".:"):
                raise WorkError("nome de janela inválido")
            result = self.run("list-windows", "-t", identity, "-F", "#{window_id}\t#{window_name}")
            matches = []
            for line in result.stdout.splitlines():
                fields = line.split(FIELD_SEPARATOR)
                if len(fields) != 2 or not WINDOW_ID.fullmatch(fields[0]) or CONTROL.search(fields[1]):
                    raise WorkError("tmux retornou janela inválida")
                if fields[1] == window:
                    matches.append(fields[0])
            if len(matches) != 1:
                raise WorkError(f"janela target inexistente ou ambígua: {window}")
            target = f"{identity}:{matches[0]}"
        self._validate_socket()
        try:
            os.execvp(self.base[0], [*self.base, "attach-session", "-t", target])
        except OSError as exc:
            raise WorkError(f"não foi possível anexar à sessão: {exc}") from exc

    def attach_visual(self, project: Project, token: str, visual_id: str, window: str | None = None) -> None:
        identity, _ = self.ensure_visual_group(project, token, visual_id, window)
        window_id = None if window is None else self._window_id(identity, window)
        self._validate_socket()
        arguments = [*self.base, "attach-session", "-t", identity]
        if window_id is not None:
            arguments.extend((";", "switch-client", "-t", f"{identity}:{window_id}"))
        try:
            os.execvp(self.base[0], arguments)
        except OSError as exc:
            raise WorkError(f"não foi possível anexar ao grupo visual: {exc}") from exc

    def _window_id(self, identity: str, window: str | None) -> str:
        if window is None:
            value = self._field(identity, "#{window_id}", "ID da janela")
            if WINDOW_ID.fullmatch(value) is None:
                raise WorkError("tmux retornou ID de janela inválido")
            return value
        if not isinstance(window, str) or not window or CONTROL.search(window) or any(char in window for char in ".:"):
            raise WorkError("nome de janela inválido")
        result = self.run("list-windows", "-t", identity, "-F", "#{window_id}\t#{window_name}")
        matches = []
        for line in result.stdout.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 2 or WINDOW_ID.fullmatch(fields[0]) is None or CONTROL.search(fields[1]):
                raise WorkError("tmux retornou janela inválida")
            if fields[1] == window:
                matches.append(fields[0])
        if len(matches) != 1:
            raise WorkError(f"janela target inexistente ou ambígua: {window}")
        return matches[0]

    def kill_id(self, session_id: str) -> None:
        if not SESSION_ID.fullmatch(session_id):
            raise WorkError("ID de sessão inválido")
        self.run("kill-session", "-t", session_id)


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
