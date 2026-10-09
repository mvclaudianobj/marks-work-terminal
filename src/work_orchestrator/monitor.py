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
from .events import decode_event, decode_signal, encode_event, validate_monitor_event
from .integration import append_integration_event, latest_integration_run
from .locking import observability_lock, project_locks
from .logging_utils import append_log
from .model import PaneObservation, Project
from .notifier import notify_or_log
from .paths import Paths, validate_slug
from .store import append_observation, append_spool, observability_agentic_state_path, observability_dedup_path, observability_path, observability_pid_path, observability_sequence_path, observability_signal_path, observability_signal_processing_path, read_private_bounded_text, read_private_bounded_text_with_fingerprint, read_snapshot, rename_private_file, snapshot_path, unlink_private_file, write_atomic, write_bytes_atomic
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
        self._notification_times: dict[tuple[str, str, str], float] = {}
        self._integration_runs: dict[str, str] = {}
        self._integration_started: set[str] = set()

    def _publish_integration(self, project: Project, identity: str, event_type: str, subject: dict[str, Any], data: dict[str, Any]) -> bool:
        try:
            append_integration_event(self.paths.state, self.paths.runtime, project.slug, event_type, project.session, identity, subject, data)
            return True
        except (WorkError, OSError) as exc:
            try:
                append_log(self.paths.state / "logs" / "integration-events.log", f"falha isolada no sink de integração: {type(exc).__name__}")
            except OSError:
                pass
            return False

    def _begin_integration(self, project: Project, identity: str) -> None:
        previous = self._integration_runs.get(project.slug)
        if previous is None:
            try:
                previous = latest_integration_run(self.paths.state, self.paths.runtime, project.slug)
            except (WorkError, OSError):
                previous = None
        if project.slug not in self._integration_started:
            if self._publish_integration(project, identity, "work.monitor.started", {}, {"reason": "first_observation"}):
                self._integration_started.add(project.slug)
        if previous is not None and previous != identity:
            self._publish_integration(project, identity, "work.run.changed", {}, {"reason": "identity_changed", "previous_run": previous})
        self._integration_runs[project.slug] = identity

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

    def _autosave(self, project: Project, token: str, expected_identity: str) -> bool:
        if not project.monitor.autosave or not hasattr(self.tmux, "capture_owned"):
            return False
        with project_locks(self.paths.runtime, project.slug, self.tmux.socket, project.session):
            first_identity, first, first_topology = self.tmux.capture_owned(project, token, expected_identity)
            if first_identity != expected_identity:
                raise WorkError("sessão mudou antes do autosave")
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
            second_identity, second, second_topology = self.tmux.capture_owned(project, token, expected_identity)
            if second_identity != expected_identity:
                raise WorkError("sessão mudou durante o autosave")
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
        event = validate_monitor_event(event)
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
            previous_state = None
            journal_fingerprint = None
            journals = []
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.exists() or rotated.is_symlink():
                journals.append(read_private_bounded_text(rotated, 1024 * 1024))
            if path.exists() or path.is_symlink():
                journal, journal_fingerprint = read_private_bounded_text_with_fingerprint(path, 1024 * 1024)
                journals.append(journal)
            for journal in journals:
                for line in journal.splitlines():
                    try:
                        old = decode_event(line)
                    except WorkError:
                        continue
                    maximum = max(maximum, old["sequence"])
                    if old.get("pane") == event["pane"] and old.get("run") == event["run"] and old.get("kind") == "state":
                        previous_state = old.get("state")
            if event_key in dedup and maximum <= dedup[event_key] and previous_state == event.get("state"):
                return False
            event["sequence"] = maximum + 1
            append_observation(path, encode_event(event), expected_fingerprint=journal_fingerprint)
            write_bytes_atomic(sequence_path, f"{event['sequence']}\n".encode("ascii"))
            dedup[event_key] = event["sequence"]
            if len(dedup) > 4096:
                dedup = dict(sorted(dedup.items(), key=lambda item: item[1])[-4096:])
            write_bytes_atomic(dedup_path, (json.dumps(dedup, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
            return previous_state != event.get("state")

    def _notify_state(self, project: Project, state: str, window_name: str | None = None, engine: str | None = None) -> bool:
        if window_name is None:
            if state in {"inactive_5m", "inactive_10m"}:
                return notify_or_log(f"{project.slug}: necessita atenção", "necessita atenção por parada longa")
            elif state in {"completed", "failed"}:
                return notify_or_log(f"{project.slug}: finalizou", "finalizou e precisa revisão/acompanhamento/análise")
            return True
        bodies = {
            "working": "está trabalhando",
            "inactive_5m": "está inativa há pelo menos 5 minutos",
            "inactive_10m": "está inativa há pelo menos 10 minutos",
            "completed": "concluiu a atividade",
            "failed": "falhou",
            "waiting_user": "aguarda ação do usuário",
            "phase_started": "iniciou nova fase",
        }
        body = bodies.get(state)
        if body is None or state == "working" and not project.monitor.notify_working:
            return True
        key = (project.slug, window_name, state)
        now = time.monotonic()
        if now - self._notification_times.get(key, -math.inf) < project.monitor.debounce:
            return True
        visual = next((item.title or item.id for item in project.visuals if item.window == window_name), window_name)
        title = " — ".join(part for part in (project.name, visual, engine) if part)
        delivered = notify_or_log(title, body)
        if not delivered:
            return False
        self._notification_times[key] = now
        return True

    def _latest_by_window(self, project: Project, identity: str | None = None) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        path = observability_path(self.paths.state, project.slug)
        for source in (path.with_suffix(path.suffix + ".1"), path):
            try:
                journal = read_private_bounded_text(source, 1024 * 1024)
            except WorkError:
                continue
            for line in journal.splitlines():
                try:
                    event = decode_event(line)
                except WorkError:
                    continue
                window_name = event.get("window_name")
                if isinstance(window_name, str) and (identity is None or event.get("run") == identity):
                    latest[window_name] = event
        return latest

    def _agentic_previous(self, project: Project, identity: str) -> dict[str, dict[str, Any]]:
        path = observability_agentic_state_path(self.paths.state, project.slug)
        try:
            value = json.loads(read_private_bounded_text(path, 256 * 1024))
        except (WorkError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict) or value.get("run") != identity or not isinstance(value.get("windows"), dict):
            return {}
        return {key: item for key, item in value["windows"].items() if isinstance(key, str) and isinstance(item, dict)}

    def _write_agentic_previous(self, project: Project, identity: str, latest: dict[str, dict[str, Any]]) -> None:
        payload = {"run": identity, "windows": latest}
        write_bytes_atomic(observability_agentic_state_path(self.paths.state, project.slug), (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))

    def _deliver(self, project: Project, identity: str, latest: dict[str, dict[str, Any]], window_name: str, engine: str | None) -> None:
        event = latest[window_name]
        delivery = event.get("delivery")
        if delivery not in {"append", "notify"}:
            return
        journal_event = {key: value for key, value in event.items() if key != "delivery"}
        if delivery == "append":
            self._append(project, journal_event)
            event["delivery"] = "notify"
            self._write_agentic_previous(project, identity, latest)
        if not self._notify_state(project, event["state"], window_name, engine):
            return
        event["delivery"] = "notified"
        self._write_agentic_previous(project, identity, latest)

    def _retry_deliveries(self, project: Project, identity: str, latest: dict[str, dict[str, Any]]) -> None:
        windows = {window.name: window for window in project.windows}
        for window_name, event in list(latest.items()):
            if event.get("delivery") in {"append", "notify"} and window_name in windows:
                self._deliver(project, identity, latest, window_name, windows[window_name].engine)

    @staticmethod
    def _aggregate_window(rows: list[PaneObservation]) -> tuple[str, int | None, bool, int | None]:
        command_class = "unknown"
        classes = {row.command_class for row in rows}
        if "engine" in classes:
            command_class = "engine"
        elif "other" in classes:
            command_class = "other"
        elif classes == {"shell"}:
            command_class = "shell"
        activities = [row.activity_timestamp for row in rows if row.activity_timestamp is not None]
        live_engine_rows = [row for row in rows if row.command_class == "engine" and not row.pane_dead]
        dead_rows = [row for row in rows if row.pane_dead]
        dead_engine_rows = [row for row in dead_rows if row.command_class == "engine"]
        decisive_dead_rows = [] if live_engine_rows else dead_engine_rows or (dead_rows if len(dead_rows) == len(rows) else [])
        failed = any(row.pane_dead_status not in {None, 0} for row in decisive_dead_rows)
        dead = bool(decisive_dead_rows)
        return command_class, max(activities) if activities else None, dead, 1 if dead and failed else 0 if dead else None

    def _agentic_state(self, project: Project, previous: dict[str, Any] | None, command_class: str, activity: int | None, dead: bool, dead_status: int | None, now: int) -> str:
        if dead:
            return "completed" if dead_status == 0 else "failed"
        prior_class = previous.get("command_class") if previous else None
        prior_state = previous.get("state") if previous else None
        prior_activity = previous.get("activity_timestamp") if previous else None
        if previous and previous.get("source") == "explicit-signal" and prior_state in {"waiting_user", "phase_started", "completed", "failed"} and prior_class == command_class and prior_activity == activity:
            return prior_state
        if previous and prior_class == "shell" and command_class in {"engine", "other"}:
            return "working"
        if previous and prior_class in {"engine", "other"} and command_class == "shell" and prior_state in {"working", "inactive_5m", "inactive_10m"}:
            return "completed"
        if activity is not None:
            age = max(0, now - activity)
            if age >= project.monitor.idle_attention_seconds:
                return "inactive_10m"
            if age >= project.monitor.idle_warning_seconds:
                return "inactive_5m"
            if previous and prior_activity is not None and activity > prior_activity and prior_state in {"inactive_5m", "inactive_10m"}:
                return "working"
        if command_class in {"engine", "other"}:
            return "working"
        if command_class == "shell":
            return "idle"
        return "unknown"

    def _inspect_agentic(self, project: Project, token: str, identity: str) -> list[dict[str, Any]]:
        observations = self.tmux.observe_owned(project, token, identity)
        self._begin_integration(project, identity)
        latest = self._agentic_previous(project, identity)
        self._retry_deliveries(project, identity, latest)
        grouped: dict[str, list[PaneObservation]] = {}
        for observation in observations:
            grouped.setdefault(observation.window_id, []).append(observation)
        now = int(time.time())
        windows = {window.name: window for window in project.windows}
        events: list[dict[str, Any]] = []
        for observation in observations:
            data = {"command_class": observation.command_class, "activity_timestamp": observation.activity_timestamp, "pane_dead": observation.pane_dead, "pane_dead_status": observation.pane_dead_status}
            engine = windows[observation.window_name].engine
            if engine is not None:
                data["engine"] = engine
            self._publish_integration(project, identity, "work.window.observed", {"window": observation.window_id, "window_name": observation.window_name, "pane": observation.pane_id}, data)
        for window_id, rows in grouped.items():
            first = rows[0]
            command_class, activity, dead, dead_status = self._aggregate_window(rows)
            previous = latest.get(first.window_name)
            state = self._agentic_state(project, previous, command_class, activity, dead, dead_status, now)
            event = {"project": project.slug, "session": project.session, "pane": first.pane_id, "window": window_id, "window_name": first.window_name, "run": identity, "sequence": 0, "kind": "state", "state": state, "source": "tmux-metadata", "confidence": "heuristic", "command_class": command_class}
            engine = windows[first.window_name].engine
            if engine is not None:
                event["engine"] = engine
            if activity is not None:
                event["activity_timestamp"] = activity
            events.append(event)
            changed = previous is None or previous.get("state") != state
            latest[first.window_name] = event
            if state in project.monitor.states and changed:
                event["delivery"] = "append"
                self._write_agentic_previous(project, identity, latest)
                if previous is None:
                    self._append(project, {key: value for key, value in event.items() if key != "delivery"})
                    event["delivery"] = "notified"
                    self._write_agentic_previous(project, identity, latest)
                else:
                    self._deliver(project, identity, latest, first.window_name, engine)
        self._write_agentic_previous(project, identity, latest)
        self._consume_signals(project, identity, latest)
        return events

    def inspect(self, slug: str, *, event_lines: list[str] | None = None) -> list[dict[str, Any]]:
        if event_lines:
            raise WorkError("event_lines externo não é autenticado e foi recusado")
        project = load_project(self.paths, validate_slug(slug))
        token = self.tmux.ownership_token(project)
        identity = self.tmux.owned_identity(project, token)
        try:
            self._autosave(project, token, identity)
        except WorkError:
            pass
        if project.monitor.agentic:
            if not hasattr(self.tmux, "observe_owned"):
                raise WorkError("tmux não oferece observação agêntica metadata-only")
            return self._inspect_agentic(project, token, identity)
        if not hasattr(self.tmux, "capture_owned"):
            return self._inspect_legacy(project, token, identity)
        _, _, topology = self.tmux.capture_owned(project, token, identity)
        events: list[dict[str, Any]] = []
        for window in topology:
            for pane in window[4]:
                state = "quiet"
                event = {"project": project.slug, "session": project.session, "pane": pane[0], "run": identity, "sequence": 0, "kind": "state", "state": state, "source": "tmux-metadata"}
                events.append(event)
                self._append(project, event)
        return events

    def signal(self, slug: str, window_name: str, state: str) -> dict[str, Any]:
        project = load_project(self.paths, validate_slug(slug))
        if not project.monitor.agentic:
            raise WorkError("monitor agentic não está habilitado para o projeto")
        allowed = {"working", "waiting_user", "phase_started", "completed", "failed"}
        if state not in allowed:
            raise WorkError("estado explícito inválido")
        window = next((item for item in project.windows if item.name == window_name), None)
        if window is None:
            raise WorkError("janela não declarada no projeto")
        token = self.tmux.ownership_token(project)
        identity = self.tmux.owned_identity(project, token)
        observations = self.tmux.observe_owned(project, token, identity)
        matching = [item for item in observations if item.window_name == window_name]
        if not matching:
            raise WorkError("janela declarada não existe na sessão owned")
        command_class, activity, _, _ = self._aggregate_window(matching)
        signal_event = {"project": project.slug, "session": project.session, "pane": matching[0].pane_id, "window": matching[0].window_id, "window_name": window_name, "run": identity, "sequence": 0, "kind": "state", "state": state, "source": "explicit-signal", "confidence": "explicit", "command_class": command_class}
        if window.engine is not None:
            signal_event["engine"] = window.engine
        if activity is not None:
            signal_event["activity_timestamp"] = activity
        payload = encode_event(signal_event)
        with observability_lock(self.paths.runtime, project.slug):
            append_spool(observability_signal_path(self.paths.state, project.slug), payload)
        return {"project": project.slug, "window": window_name, "state": state, "queued": True}

    def _consume_signals(self, project: Project, identity: str, latest: dict[str, dict[str, Any]]) -> None:
        path = observability_signal_path(self.paths.state, project.slug)
        processing = observability_signal_processing_path(self.paths.state, project.slug)
        self._retry_deliveries(project, identity, latest)
        with observability_lock(self.paths.runtime, project.slug):
            if processing.exists() or processing.is_symlink():
                payload = read_private_bounded_text(processing, 1024 * 1024)
            else:
                try:
                    read_private_bounded_text(path, 1024 * 1024)
                except WorkError:
                    return
                rename_private_file(path, processing)
                payload = read_private_bounded_text(processing, 1024 * 1024)
        windows = {window.name: window for window in project.windows}
        for line in payload.splitlines():
            try:
                event = decode_signal(line)
            except WorkError:
                continue
            window_name = event.get("window_name")
            window_id = event.get("window")
            if event.get("project") != project.slug or event.get("session") != project.session or event.get("run") != identity or event.get("source") != "explicit-signal" or window_name not in windows or not isinstance(window_id, str):
                continue
            previous = latest.get(window_name)
            if previous is not None and previous.get("state") == event.get("state"):
                continue
            latest[window_name] = event
            data = {"state": event["state"], "confidence": "explicit"}
            if windows[window_name].engine is not None:
                data["engine"] = windows[window_name].engine
            self._publish_integration(project, identity, "work.window.signal", {"window": window_id, "window_name": window_name, "pane": event["pane"]}, data)
            if event["state"] in project.monitor.states:
                event["delivery"] = "append"
                self._write_agentic_previous(project, identity, latest)
                self._deliver(project, identity, latest, window_name, windows[window_name].engine)
        self._write_agentic_previous(project, identity, latest)
        with observability_lock(self.paths.runtime, project.slug):
            if processing.exists() or processing.is_symlink():
                unlink_private_file(processing)

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
        normal = False
        try:
            while True:
                self.inspect(project.slug)
                if once:
                    normal = True
                    return
                time.sleep(interval)
        finally:
            identity = self._integration_runs.get(project.slug)
            if normal and project.monitor.agentic and identity is not None:
                self._publish_integration(project, identity, "work.monitor.stopped", {}, {"reason": "normal"})
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
        result = {"project": project.slug, "active": active, "latest": latest, "path": str(path)}
        if project.monitor.agentic:
            identity = None
            try:
                token = self.tmux.ownership_token(project)
                identity = self.tmux.owned_identity(project, token)
            except WorkError:
                pass
            by_window = self._latest_by_window(project, identity)
            if identity is not None:
                private = self._agentic_previous(project, identity)
                for window_name, event in private.items():
                    current = by_window.get(window_name)
                    if current is None or event.get("timestamp", 0) >= current.get("timestamp", 0):
                        by_window[window_name] = event
            result["agentic"] = True
            result["windows"] = [
                {"window": window.name, "engine": window.engine, "state": by_window.get(window.name, {}).get("state", "unknown"), "source": by_window.get(window.name, {}).get("source")}
                for window in project.windows
            ]
        return result

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
