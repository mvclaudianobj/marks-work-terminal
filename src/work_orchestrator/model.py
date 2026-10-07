from dataclasses import dataclass
from pathlib import Path


POLICIES = {"always", "prompt", "never"}
MONITOR_STATES = {"running", "quiet", "inactive_5m", "inactive_10m", "completed", "failed"}


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
    idle_warning_seconds: int = 300
    idle_attention_seconds: int = 600
    states: tuple[str, ...] = tuple(sorted(MONITOR_STATES))
    completion_keywords: tuple[str, ...] = ()
    candidate_keywords: tuple[str, ...] = ()
    failed_keywords: tuple[str, ...] = ()
