import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .errors import WorkError
from .events import IDENTIFIER, SIGNAL_STATES
from .locking import integration_lock
from .model import ENGINES
from .paths import ensure_regular_private_file
from .store import append_observation, read_private_bounded_text, write_bytes_atomic


SCHEMA = "work.events.v1"
PRODUCER = "work-monitor"
EVENT_TYPES = {"work.monitor.started", "work.run.changed", "work.window.observed", "work.window.signal", "work.monitor.stopped"}
TOP_LEVEL_FIELDS = {"schema", "event_id", "type", "producer", "project", "session", "run", "sequence", "timestamp", "subject", "data"}
SUBJECT_FIELDS = {"window", "window_name", "pane"}
COMMAND_CLASSES = {"shell", "engine", "other", "unknown"}
LIFECYCLE_REASONS = {"work.monitor.started": {"monitor_started", "first_observation"}, "work.monitor.stopped": {"normal"}}
DATA_FIELDS = {
    "work.monitor.started": {"reason"},
    "work.run.changed": {"reason", "previous_run"},
    "work.window.observed": {"engine", "command_class", "activity_timestamp", "pane_dead", "pane_dead_status"},
    "work.window.signal": {"state", "confidence", "engine"},
    "work.monitor.stopped": {"reason"},
}
MAX_JOURNAL_BYTES = 1024 * 1024


def integration_events_path(state: Path, slug: str) -> Path:
    return state / "integration-events" / f"{slug}.jsonl"


def integration_sequence_path(state: Path, slug: str) -> Path:
    return state / "integration-events" / f"{slug}.sequence"


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise WorkError(f"evento de integração possui {label} inválido")
    return value


def _optional_nonnegative_int(value: Any, label: str) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise WorkError(f"evento de integração possui {label} inválido")
    return value


def validate_integration_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TOP_LEVEL_FIELDS:
        raise WorkError("evento de integração possui campos top-level inválidos")
    if value.get("schema") != SCHEMA or value.get("producer") != PRODUCER or value.get("type") not in EVENT_TYPES:
        raise WorkError("evento de integração possui contrato inválido")
    for key in ("project", "session", "run"):
        _identifier(value.get(key), key)
    if type(value.get("sequence")) is not int or not 0 <= value["sequence"] <= 2**63 - 1:
        raise WorkError("evento de integração possui sequence inválido")
    if type(value.get("timestamp")) is not int or not 0 <= value["timestamp"] <= 2**63 - 1:
        raise WorkError("evento de integração possui timestamp inválido")
    subject = value.get("subject")
    if not isinstance(subject, dict) or set(subject) - SUBJECT_FIELDS:
        raise WorkError("evento de integração possui subject inválido")
    for key, item in subject.items():
        _identifier(item, f"subject.{key}")
    event_type = value["type"]
    data = value.get("data")
    if not isinstance(data, dict) or set(data) != DATA_FIELDS[event_type] and not (event_type in {"work.window.observed", "work.window.signal"} and set(data) == DATA_FIELDS[event_type] - {"engine"}):
        raise WorkError("evento de integração possui data inválido")
    if event_type in {"work.monitor.started", "work.monitor.stopped"}:
        if subject or data.get("reason") not in LIFECYCLE_REASONS[event_type]:
            raise WorkError("evento lifecycle inválido")
    elif event_type == "work.run.changed":
        if subject or data.get("reason") != "identity_changed":
            raise WorkError("evento run.changed inválido")
        previous_run = data.get("previous_run")
        if previous_run is not None:
            _identifier(previous_run, "data.previous_run")
    elif event_type == "work.window.observed":
        if set(subject) != SUBJECT_FIELDS or data.get("command_class") not in COMMAND_CLASSES or type(data.get("pane_dead")) is not bool:
            raise WorkError("evento window.observed inválido")
        _optional_nonnegative_int(data.get("activity_timestamp"), "data.activity_timestamp")
        _optional_nonnegative_int(data.get("pane_dead_status"), "data.pane_dead_status")
        if "engine" in data and data["engine"] not in ENGINES:
            raise WorkError("evento de integração possui data.engine inválido")
    else:
        if set(subject) != SUBJECT_FIELDS or data.get("state") not in SIGNAL_STATES or data.get("confidence") != "explicit":
            raise WorkError("evento window.signal inválido")
        if "engine" in data and data["engine"] not in ENGINES:
            raise WorkError("evento de integração possui data.engine inválido")
    event_id = value.get("event_id")
    if not isinstance(event_id, str) or len(event_id) != 64 or any(char not in "0123456789abcdef" for char in event_id):
        raise WorkError("evento de integração possui event_id inválido")
    identity = {key: item for key, item in value.items() if key != "event_id"}
    expected = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if event_id != expected:
        raise WorkError("evento de integração possui event_id inconsistente")
    return dict(value)


def build_integration_event(event_type: str, project: str, session: str, run: str, sequence: int, subject: dict[str, Any], data: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any]:
    event = {"schema": SCHEMA, "event_id": "", "type": event_type, "producer": PRODUCER, "project": project, "session": session, "run": run, "sequence": sequence, "timestamp": time.time_ns() if timestamp is None else timestamp, "subject": dict(subject), "data": dict(data)}
    event["event_id"] = hashlib.sha256(json.dumps({key: item for key, item in event.items() if key != "event_id"}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return validate_integration_event(event)


def append_integration_event(state: Path, runtime: Path, slug: str, event_type: str, session: str, run: str, subject: dict[str, Any], data: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any]:
    path = integration_events_path(state, slug)
    sequence_path = integration_sequence_path(state, slug)
    with integration_lock(runtime, slug):
        try:
            maximum = int(read_private_bounded_text(sequence_path, 64).strip())
        except (FileNotFoundError, WorkError, ValueError):
            maximum = -1
        for source in (path.with_suffix(path.suffix + ".1"), path):
            if not source.exists() and not source.is_symlink():
                continue
            for line in read_private_bounded_text(source, MAX_JOURNAL_BYTES).splitlines():
                try:
                    maximum = max(maximum, validate_integration_event(json.loads(line))["sequence"])
                except (json.JSONDecodeError, WorkError) as exc:
                    raise WorkError("journal de integração contém evento inválido") from exc
        sequence = maximum + 1
        event = build_integration_event(event_type, slug, session, run, sequence, subject, data, timestamp=timestamp)
        payload = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            append_observation(path, payload, max_bytes=MAX_JOURNAL_BYTES)
            write_bytes_atomic(sequence_path, f"{sequence}\n".encode("ascii"))
        except OSError as exc:
            raise WorkError("falha segura ao publicar evento de integração") from exc
        return event


def read_integration_events(state: Path, runtime: Path, slug: str, *, after: int = -1, limit: int = 100) -> list[dict[str, Any]]:
    if type(after) is not int or after < -1 or type(limit) is not int or not 1 <= limit <= 10000:
        raise WorkError("filtros de eventos de integração inválidos")
    path = integration_events_path(state, slug)
    events: list[dict[str, Any]] = []
    with integration_lock(runtime, slug):
        for source in (path.with_suffix(path.suffix + ".1"), path):
            if not source.exists() and not source.is_symlink():
                continue
            ensure_regular_private_file(source)
            for line in read_private_bounded_text(source, MAX_JOURNAL_BYTES).splitlines():
                try:
                    event = validate_integration_event(json.loads(line))
                except (json.JSONDecodeError, WorkError):
                    raise WorkError("journal de integração contém evento inválido")
                if event["sequence"] > after:
                    events.append(event)
                    if len(events) >= limit:
                        return events
    return events


def latest_integration_run(state: Path, runtime: Path, slug: str) -> str | None:
    events = read_integration_events(state, runtime, slug, limit=10000)
    return events[-1]["run"] if events else None
