from dataclasses import dataclass
from pathlib import Path


POLICIES = {"always", "prompt", "never"}
ENGINES = {"markscode", "opencode", "codex", "claude"}
COMMAND_CLASSES = {"shell", "engine", "other", "unknown"}
MONITOR_STATES = {"running", "quiet", "unknown", "idle", "working", "inactive_5m", "inactive_10m", "completed", "failed", "waiting_user", "phase_started"}


@dataclass(frozen=True)
class Pane:
    cwd: Path
    name: str | None
    command: tuple[str, ...] | None
    policy: str
    focus: bool


@dataclass(frozen=True)
class Window:
    name: str
    cwd: Path
    layout: str | None
    panes: tuple[Pane, ...]
    focus: bool
    engine: str | None = None


@dataclass(frozen=True)
class Visual:
    id: str
    kind: str
    root: Path
    order: int
    session: str
    window: str | None
    title: str | None = None


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    root: Path
    session: str
    command_policy: str
    cwd_aliases: tuple[tuple[str, Path], ...]
    windows: tuple[Window, ...]
    visuals: tuple[Visual, ...]
    monitor: "MonitorConfig"


@dataclass(frozen=True)
class MonitorConfig:
    autosave: bool = True
    debounce: float = 0.75
    interval: float = 5.0
    history: bool = False
    agentic: bool = False
    notify_working: bool = False
    idle_warning_seconds: int = 300
    idle_attention_seconds: int = 600
    states: tuple[str, ...] = tuple(sorted(MONITOR_STATES))
    completion_keywords: tuple[str, ...] = ()
    candidate_keywords: tuple[str, ...] = ()
    failed_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaneObservation:
    session_id: str
    window_id: str
    window_name: str
    window_index: int
    pane_id: str
    pane_index: int
    activity_timestamp: int | None
    command_class: str
    pane_dead: bool
    pane_dead_status: int | None
