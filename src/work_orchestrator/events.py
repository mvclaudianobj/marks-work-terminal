import json
import re
import time
from typing import Any

from .errors import WorkError


EVENT_FIELDS = {
    "project", "session", "pane", "run", "sequence", "kind", "state", "timestamp",
    "message", "source", "pane_pid", "current_command", "pane_dead", "pane_dead_status",
    "activity_timestamp", "pane_bytes", "pane_lines", "event_key", "window", "window_name",
    "engine", "confidence", "command_class",
}
IDENTIFIER = re.compile(r"\A[A-Za-z0-9_.$:%/@+-]{1,128}\Z")
KINDS = {"state", "completion", "failure", "input", "heartbeat"}
SIGNAL_FIELDS = {"project", "session", "pane", "run", "sequence", "kind", "state", "timestamp", "source", "window", "window_name", "engine", "confidence", "command_class", "activity_timestamp", "event_key"}
SIGNAL_STATES = {"working", "waiting_user", "phase_started", "completed", "failed"}
MONITOR_EVENT_FIELDS = {"project", "session", "pane", "run", "sequence", "kind", "state", "timestamp", "source", "window", "window_name", "engine", "confidence", "command_class", "activity_timestamp", "event_key", "pane_dead", "pane_dead_status"}


def validate_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - EVENT_FIELDS:
        raise WorkError("evento possui campos desconhecidos")
    required = ("project", "session", "pane", "run", "sequence", "kind")
    if any(key not in value for key in required):
        raise WorkError("evento incompleto")
    for key in ("project", "session", "pane", "run"):
        if not isinstance(value[key], str) or not IDENTIFIER.fullmatch(value[key]):
            raise WorkError(f"evento.{key} inválido")
    if type(value["sequence"]) is not int or not 0 <= value["sequence"] <= 2**63 - 1:
        raise WorkError("evento.sequence inválido")
    if value["kind"] not in KINDS:
        raise WorkError("evento.kind inválido")
    for key in ("state", "message", "source", "window", "window_name", "engine", "confidence", "command_class"):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value[key])):
            raise WorkError(f"evento.{key} inválido")
    for key in ("pane_pid", "pane_dead_status", "activity_timestamp", "pane_bytes", "pane_lines"):
        if key in value and (type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1):
            raise WorkError(f"evento.{key} inválido")
    if "pane_dead" in value and type(value["pane_dead"]) is not bool:
        raise WorkError("evento.pane_dead inválido")
    if "current_command" in value and (not isinstance(value["current_command"], str) or len(value["current_command"]) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in value["current_command"])):
        raise WorkError("evento.current_command inválido")
    if "timestamp" in value and (type(value["timestamp"]) is not int or not 0 <= value["timestamp"] <= 2**63 - 1):
        raise WorkError("evento.timestamp inválido")
    if "event_key" in value and (not isinstance(value["event_key"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["event_key"])):
        raise WorkError("evento.event_key inválido")
    result = dict(value)
    result.setdefault("timestamp", time.time_ns())
    return result


def encode_event(value: dict[str, Any]) -> bytes:
    return (json.dumps(validate_event(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decode_event(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise WorkError("evento não é JSON válido") from exc
    return validate_event(value)


def decode_signal(line: str) -> dict[str, Any]:
    event = decode_event(line)
    if set(event) - SIGNAL_FIELDS or event.get("kind") != "state" or event.get("state") not in SIGNAL_STATES or event.get("source") != "explicit-signal" or event.get("confidence") != "explicit":
        raise WorkError("sinal explícito possui schema inválido")
    required = {"project", "session", "pane", "run", "sequence", "kind", "state", "timestamp", "source", "window", "window_name", "confidence", "command_class"}
    if not required.issubset(event):
        raise WorkError("sinal explícito incompleto")
    return event


def validate_monitor_event(value: dict[str, Any]) -> dict[str, Any]:
    event = validate_event(value)
    if set(event) - MONITOR_EVENT_FIELDS:
        raise WorkError("evento do monitor possui campos desconhecidos")
    return event
