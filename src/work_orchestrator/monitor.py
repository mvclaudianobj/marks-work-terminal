import hashlib
import json
import math
import os
import signal
import time
from pathlib import Path
from typing import Any

from .config import load_project
from .errors import WorkError
from .events import decode_event, encode_event
from .locking import observability_lock, project_locks
from .model import Project
from .notifier import notify_or_log
from .paths import Paths, validate_slug
from .store import append_observation, observability_dedup_path, observability_path, observability_pid_path, observability_sequence_path, read_private_bounded_text, read_private_bounded_text_with_fingerprint, read_snapshot, snapshot_path, write_atomic, write_bytes_atomic
from .tmux import Tmux
from .workspace import WorkspaceService


TMUX_FORMAT = "\t".join(("#{session_id}", "#{@work-orchestrator-project}", "#{@work-orchestrator-token}", "#{pane_id}", "#{pane_dead}", "#{pane_dead_status}", "#{pane_activity}", "#{pane_width}", "#{pane_height}"))


def _safe_field(value: str, limit: int = 128) -> str | None:
    value = value.strip()
    if not value or len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


class Monitor:
    def __init__(self, paths: Paths | None = None, tmux: Tmux | None = None) -> None:
        self.paths = paths or Paths.discover()
        self.paths.ensure()
        self.tmux = tmux or Tmux(self.paths)
        self._signatures: dict[str, tuple[Any, ...]] = {}
        self._workspace_signatures: dict[str, str] = {}

    def _stable_signature(self, topology: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(
            (window[1], window[2], window[3], tuple((pane[1], pane[2], pane[3], pane[4], pane[5]) for pane in window[4]))
            for window in topology
        )

    def _snapshot_signature(self, snapshot: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(
            (window["name"], window.get("layout"), window["focus"], tuple((pane["cwd"], pane.get("name"), pane["focus"]) for pane in window["panes"]))
            for window in snapshot["windows"]
        )

    def _autosave(self, project: Project, token: str) -> bool:
        if not project.monitor.autosave or not hasattr(self.tmux, "capture_owned"):
            return False
        with project_locks(self.paths.runtime, project.slug, self.tmux.socket, project.session):
            _, first, first_topology = self.tmux.capture_owned(project, token)
            first_signature = self._stable_signature(first_topology)
            previous = self._signatures.get(project.slug)
            self._signatures[project.slug] = first_signature
            try:
                stored_signature = self._snapshot_signature(read_snapshot(snapshot_path(self.paths.state, project.slug)))
            except WorkError:
                stored_signature = None
            if previous == first_signature and stored_signature == self._snapshot_signature(first):
                return False
            if previous is None and stored_signature == self._snapshot_signature(first):
                return False
            time.sleep(project.monitor.debounce)
            _, second, second_topology = self.tmux.capture_owned(project, token)
            if first_signature != self._stable_signature(second_topology):
                return False
            write_atomic(snapshot_path(self.paths.state, project.slug), second)
        try:
            from .service import Service

            workspace = WorkspaceService(Service(self.paths, self.tmux))
            aggregate = workspace.save("default", refresh_snapshots=False)
            self._workspace_signatures[project.slug] = str(aggregate)
        except (WorkError, OSError):
            pass
        return True

    def _append(self, project: Project, event: dict[str, Any]) -> bool:
        path = observability_path(self.paths.state, project.slug)
        sequence_path = observability_sequence_path(self.paths.state, project.slug)
        dedup_path = observability_dedup_path(self.paths.state, project.slug)
        with observability_lock(self.paths.runtime, project.slug):
            try:
                maximum = int(sequence_path.read_text(encoding="ascii").strip())
            except (FileNotFoundError, OSError, ValueError):
                maximum = -1
            identity = {key: value for key, value in event.items() if key not in {"sequence", "timestamp", "event_key"}}
            event_key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            event["event_key"] = event_key
            try:
                dedup = json.loads(read_private_bounded_text(dedup_path, 256 * 1024))
            except (FileNotFoundError, WorkError, ValueError, json.JSONDecodeError):
                dedup = {}
            if not isinstance(dedup, dict):
                dedup = {}
            if event_key in dedup and maximum <= dedup[event_key]:
                return False
            previous_state = None
            journal_fingerprint = None
            if path.exists() or path.is_symlink():
                journal, journal_fingerprint = read_private_bounded_text_with_fingerprint(path, 1024 * 1024)
                for line in journal.splitlines():
                    try:
                        old = decode_event(line)
                    except WorkError:
                        continue
                    maximum = max(maximum, old["sequence"])
                    if old.get("pane") == event["pane"] and old.get("run") == event["run"] and old.get("kind") == "state":
                        previous_state = old.get("state")
            event["sequence"] = maximum + 1
            append_observation(path, encode_event(event), expected_fingerprint=journal_fingerprint)
            write_bytes_atomic(sequence_path, f"{event['sequence']}\n".encode("ascii"))
            dedup[event_key] = event["sequence"]
            if len(dedup) > 4096:
                dedup = dict(sorted(dedup.items(), key=lambda item: item[1])[-4096:])
            write_bytes_atomic(dedup_path, (json.dumps(dedup, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
            return previous_state != event.get("state")

    def _notify_state(self, project: Project, state: str) -> None:
        if state in {"inactive_5m", "inactive_10m"}:
            notify_or_log(f"{project.slug}: necessita atenção", "necessita atenção por parada longa")
        elif state in {"completed", "failed"}:
            notify_or_log(f"{project.slug}: finalizou", "finalizou e precisa revisão/acompanhamento/análise")

    def inspect(self, slug: str, *, event_lines: list[str] | None = None) -> list[dict[str, Any]]:
        if event_lines:
            raise WorkError("event_lines externo não é autenticado e foi recusado")
        project = load_project(self.paths, validate_slug(slug))
        token = self.tmux.ownership_token(project)
        identity = self.tmux.owned_identity(project, token)
        try:
            self._autosave(project, token)
        except WorkError:
            pass
        if not hasattr(self.tmux, "capture_owned"):
            return self._inspect_legacy(project, token, identity)
        _, _, topology = self.tmux.capture_owned(project, token)
        events: list[dict[str, Any]] = []
        for window in topology:
            for pane in window[4]:
                state = "quiet"
                event = {"project": project.slug, "session": project.session, "pane": pane[0], "run": identity, "sequence": 0, "kind": "state", "state": state, "source": "tmux-metadata"}
                events.append(event)
                self._append(project, event)
        return events

    def _inspect_legacy(self, project: Project, token: str, identity: str) -> list[dict[str, Any]]:
        result = self.tmux.run("list-panes", "-s", "-t", identity, "-F", TMUX_FORMAT)
        now = int(time.time())
        events: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 9 or fields[0] != identity or fields[1] != project.slug or fields[2] != token:
                raise WorkError("tmux retornou metadados de monitor inválidos")
            metadata = Tmux.parse_monitor_fields(tuple(fields), now)
            pane = metadata["pane"]
            dead = metadata["pane_dead"]
            activity = metadata["activity_timestamp"]
            dead_status = metadata["pane_dead_status"]
            age = max(0, now - activity)
            if dead:
                state = "completed" if dead_status == 0 else "failed"
            elif age >= project.monitor.idle_attention_seconds:
                state = "inactive_10m"
            elif age >= project.monitor.idle_warning_seconds:
                state = "inactive_5m"
            else:
                state = "running" if activity else "quiet"
            if state not in project.monitor.states:
                state = "quiet"
            event = {"project": project.slug, "session": project.session, "pane": pane, "run": identity, "sequence": 0, "kind": "state", "state": state, "source": "tmux-metadata", "pane_dead": dead, "pane_dead_status": dead_status, "activity_timestamp": activity}
            events.append(event)
            if self._append(project, event) and state in {"inactive_5m", "inactive_10m", "completed", "failed"}:
                self._notify_state(project, state)
        return events

    def _process_marker(self, pid: int) -> dict[str, Any]:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start = int(stat_text.rsplit(")", 1)[1].split()[19])
        command = hashlib.sha256(Path(f"/proc/{pid}/cmdline").read_bytes()).hexdigest()
        return {"pid": pid, "start": start, "command": command}

    def _process_alive(self, marker: dict[str, Any]) -> bool:
        try:
            return self._process_marker(int(marker["pid"])) == marker
        except (OSError, ValueError, KeyError, IndexError):
            return False

    def _read_pid_marker(self, path: Path) -> tuple[dict[str, Any], str]:
        text = read_private_bounded_text(path, 4096)
        try:
            marker = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkError("pidfile inválido") from exc
        if not isinstance(marker, dict) or set(marker) != {"pid", "start", "command"}:
            raise WorkError("pidfile inválido")
        if type(marker.get("pid")) is not int or marker["pid"] <= 0:
            raise WorkError("pidfile inválido")
        if type(marker.get("start")) is not int or marker["start"] < 0:
            raise WorkError("pidfile inválido")
        if not isinstance(marker.get("command"), str) or len(marker["command"]) != 64 or any(char not in "0123456789abcdef" for char in marker["command"]):
            raise WorkError("pidfile inválido")
        return marker, hashlib.sha256(text.encode("ascii")).hexdigest()

    def _open_pidfd(self, marker: dict[str, Any]) -> int:
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is None or not hasattr(signal, "pidfd_send_signal"):
            raise WorkError("stop recusado: pidfd não disponível")
        descriptor = pidfd_open(marker["pid"], 0)
        try:
            if not self._process_alive(marker):
                os.close(descriptor)
                return -1
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def start(self, slug: str, *, once: bool = False, interval: float = 5.0, event_fd: int | None = None) -> None:
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 < interval <= 86400:
            raise WorkError("interval deve ser finito, maior que zero e no máximo 86400")
        if event_fd is not None:
            raise WorkError("event_fd externo não é autenticado e foi recusado")
        project = load_project(self.paths, validate_slug(slug))
        pid_path = observability_pid_path(self.paths.state, project.slug)
        pid_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = self._process_marker(os.getpid())
        with observability_lock(self.paths.runtime, project.slug):
            if pid_path.is_symlink():
                raise WorkError("arquivo PID symlink inseguro")
            if pid_path.exists():
                try:
                    old = json.loads(pid_path.read_text(encoding="ascii"))
                except (OSError, ValueError, json.JSONDecodeError):
                    old = None
                if isinstance(old, dict) and self._process_alive(old):
                    raise WorkError("monitor já está ativo")
                pid_path.unlink(missing_ok=True)
            write_bytes_atomic(pid_path, (json.dumps(marker, sort_keys=True) + "\n").encode("ascii"), exclusive=True)
        try:
            while True:
                self.inspect(project.slug)
                if once:
                    return
                time.sleep(interval)
        finally:
            with observability_lock(self.paths.runtime, project.slug):
                try:
                    if json.loads(pid_path.read_text(encoding="ascii")) == marker:
                        pid_path.unlink()
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    pass

    def status(self, slug: str) -> dict[str, Any]:
        project = load_project(self.paths, validate_slug(slug))
        path = observability_path(self.paths.state, project.slug)
        latest = None
        active = False
        with observability_lock(self.paths.runtime, project.slug):
            try:
                journal = read_private_bounded_text(path, 1024 * 1024)
            except WorkError:
                journal = ""
            for line in journal.splitlines():
                try:
                    latest = decode_event(line)
                except WorkError:
                    continue
            try:
                marker, _ = self._read_pid_marker(observability_pid_path(self.paths.state, project.slug))
                active = self._process_alive(marker)
            except (FileNotFoundError, OSError, WorkError, ValueError):
                active = False
        return {"project": project.slug, "active": active, "latest": latest, "path": str(path)}

    def stop(self, slug: str) -> None:
        project = load_project(self.paths, validate_slug(slug))
        path = observability_pid_path(self.paths.state, project.slug)
        with observability_lock(self.paths.runtime, project.slug):
            if not path.exists() or path.is_symlink():
                return
            try:
                marker, fingerprint = self._read_pid_marker(path)
                pidfd = self._open_pidfd(marker)
                if pidfd >= 0:
                    try:
                        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                    finally:
                        os.close(pidfd)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and self._process_alive(marker):
                        time.sleep(0.05)
                current, current_fingerprint = self._read_pid_marker(path)
                if current == marker and current_fingerprint == fingerprint:
                    path.unlink()
            except (FileNotFoundError, ProcessLookupError, OSError, WorkError, ValueError):
                pass
