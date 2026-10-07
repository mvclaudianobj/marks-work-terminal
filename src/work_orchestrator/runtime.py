import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

from .errors import WorkError
from .paths import ensure_private_directory, ensure_regular_private_file


ENGINES = {"markscode", "claude", "opencode", "codex"}
LOCK_ORDER = ("workspace", "project", "agent", "task")
MAX_FENCING_TOKEN = 9223372036854775807


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value):
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items() if str(k).lower() not in {"prompt", "token", "scrollback", "observed_commands", "commands"}}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?is)(prompt|scrollback|observed[_ -]?commands|commands)\s*[=:]\s*.*?(?=\s+(?:prompt|scrollback|observed[_ -]?commands|commands)\s*[=:]|$)", r"\1=[REDACTED]", value)
        value = re.sub(r"(?i)(bearer\s+|token\s*[=:]\s*|password\s*[=:]\s*|api[_-]?key\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
        return value[:2000]
    return value


def json_text(value) -> str:
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _uuid(value=None) -> str:
    return str(value or uuid.uuid4())


class Runtime:
    def __init__(self, project_root: str | os.PathLike[str], state_home: str | os.PathLike[str] | None = None):
        self.worktree = Path(project_root).expanduser().resolve(strict=True)
        if not self.worktree.is_dir():
            raise WorkError("project root não é diretório")
        home = Path.home()
        state = Path(state_home or os.environ.get("XDG_STATE_HOME", home / ".local/state")).expanduser()
        if not state.is_absolute():
            raise WorkError("XDG_STATE_HOME deve ser absoluto")
        self.base = state / "work-orchestrator"
        ensure_private_directory(self.base)
        self.registry_path = self.base / "projects.json"
        self.project_id = self._project_id()
        self.root = self.base / "runtime" / self.project_id
        ensure_private_directory(self.base / "runtime")
        ensure_private_directory(self.root)
        self.db_path = self.root / "control.sqlite3"
        self._init_db()

    def _project_id(self) -> str:
        lock_path = self.registry_path.with_suffix(".lock")
        lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(lock_path, 0o600)
        with lock_path.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                data = {}
                if self.registry_path.exists():
                    ensure_regular_private_file(self.registry_path)
                    data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                key = str(self.worktree)
                project_id = data.get(key)
                if not project_id:
                    project_id = str(uuid.uuid4())
                    data[key] = project_id
                    temporary = self.registry_path.with_name(self.registry_path.name + ".tmp")
                    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.registry_path)
                return project_id
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS lease_fencing_counters(resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, PRIMARY KEY(resource_type, resource_id))")
            connection.execute("CREATE TABLE IF NOT EXISTS lease_history(lease_event_id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, agent_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, created_at TEXT NOT NULL)")
            rows = connection.execute("SELECT resource_type, resource_id, fencing_token FROM leases").fetchall()
            for row in rows:
                self._validate_fencing_token(row["fencing_token"])
                connection.execute("INSERT INTO lease_fencing_counters(resource_type, resource_id, fencing_token) VALUES(?,?,?) ON CONFLICT(resource_type, resource_id) DO UPDATE SET fencing_token=MAX(lease_fencing_counters.fencing_token, excluded.fencing_token)", (row["resource_type"], row["resource_id"], row["fencing_token"]))
            counters = connection.execute("SELECT fencing_token FROM lease_fencing_counters").fetchall()
            for row in counters:
                self._validate_fencing_token(row["fencing_token"], allow_zero=True)
            connection.execute("INSERT OR IGNORE INTO projects(project_id, root, project_kind, created_at, updated_at) VALUES(?,?,?,?,?)", (self.project_id, str(self.worktree), "git" if (self.worktree / ".git").exists() else "non-git", now(), now()))
            connection.execute("COMMIT")
        finally:
            connection.close()
        os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def status(self) -> dict:
        with self.connect() as connection:
            counts = {}
            for table in ("agents", "tasks", "leases", "claims", "handoffs", "events", "artifacts", "snapshots"):
                counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            project = dict(connection.execute("SELECT * FROM projects WHERE project_id=?", (self.project_id,)).fetchone())
        return {"project_id": self.project_id, "worktree": str(self.worktree), "db": str(self.db_path), "git": project["project_kind"] == "git", "counts": counts}

    def register_agent(self, engine: str, provider=None, version=None, capabilities=None, worktree_id=None, cwd=None, tmux_visual=None) -> dict:
        if engine not in ENGINES:
            raise WorkError("engine inválida")
        agent_id, session_id = _uuid(), _uuid()
        with self.transaction() as connection:
            connection.execute("INSERT INTO agents(agent_id, engine, provider, version, capabilities, project_id, worktree_id, cwd, tmux_visual, session_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (agent_id, engine, provider, version, json_text(capabilities or []), self.project_id, worktree_id, str(Path(cwd or self.worktree).resolve()), tmux_visual, session_id, "registered", now(), now()))
            connection.execute("INSERT INTO sessions(session_id, agent_id) VALUES(?,?)", (session_id, agent_id))
        return self.agent(agent_id)

    def agent(self, agent_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            raise WorkError("agente não encontrado")
        result = dict(row)
        result["capabilities"] = json.loads(result["capabilities"])
        return result

    def agents(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM agents ORDER BY created_at")]

    def agent_state(self, agent_id, status):
        if status not in {"started", "stopped", "registered"}:
            raise WorkError("estado de agente inválido")
        with self.transaction() as connection:
            updated = connection.execute("UPDATE agents SET status=?, updated_at=? WHERE agent_id=?", (status, now(), agent_id)).rowcount
            if not updated:
                raise WorkError("agente não encontrado")
            if status == "started":
                connection.execute("UPDATE sessions SET started_at=?, stopped_at=NULL WHERE agent_id=(SELECT agent_id FROM agents WHERE agent_id=?)", (now(), agent_id))
            if status == "stopped":
                connection.execute("UPDATE sessions SET stopped_at=? WHERE agent_id=?", (now(), agent_id))
        return self.agent(agent_id)

    def heartbeat(self, agent_id):
        with self.transaction() as connection:
            updated = connection.execute("UPDATE agents SET last_heartbeat=?, updated_at=? WHERE agent_id=?", (now(), now(), agent_id)).rowcount
            if not updated:
                raise WorkError("agente não encontrado")
        return self.agent(agent_id)

    @staticmethod
    def _validate_fencing_token(token, allow_zero=False):
        minimum = 0 if allow_zero else 1
        if type(token) is not int or not minimum <= token <= MAX_FENCING_TOKEN:
            raise WorkError("fencing_token inválido")
        return token

    def create_task(self, title, description="", priority=0):
        task_id = _uuid()
        with self.transaction() as connection:
            connection.execute("INSERT INTO tasks(task_id, project_id, title, description, priority, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)", (task_id, self.project_id, title[:300], redact(description)[:4000], priority, "open", now(), now()))
        return task_id

    def claim_task(self, task_id, agent_id):
        with self.transaction() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row or row["status"] != "open":
                raise WorkError("tarefa indisponível")
            connection.execute("UPDATE tasks SET status='claimed', claimed_by=?, updated_at=? WHERE task_id=?", (agent_id, now(), task_id))
        return task_id

    def release_task(self, task_id, agent_id):
        with self.transaction() as connection:
            updated = connection.execute("UPDATE tasks SET status='open', claimed_by=NULL, updated_at=? WHERE task_id=? AND claimed_by=?", (now(), task_id, agent_id)).rowcount
            if not updated:
                raise WorkError("tarefa não pertence ao agente")

    def acquire_lease(self, resource_type, resource_id, agent_id, ttl=60):
        if resource_type not in LOCK_ORDER or ttl <= 0:
            raise WorkError("lease inválida")
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM leases WHERE resource_type=? AND resource_id=?", (resource_type, resource_id)).fetchone()
            if row and row["expires_at"] > now() and row["agent_id"] != agent_id:
                raise WorkError("recurso já possui lease ativa")
            if row:
                self._validate_fencing_token(row["fencing_token"])
            connection.execute("INSERT OR IGNORE INTO lease_fencing_counters(resource_type, resource_id, fencing_token) VALUES(?,?,0)", (resource_type, resource_id))
            updated = connection.execute("UPDATE lease_fencing_counters SET fencing_token=fencing_token+1 WHERE resource_type=? AND resource_id=? AND fencing_token<?", (resource_type, resource_id, MAX_FENCING_TOKEN)).rowcount
            if not updated:
                raise WorkError("fencing_token esgotado")
            token = connection.execute("SELECT fencing_token FROM lease_fencing_counters WHERE resource_type=? AND resource_id=?", (resource_type, resource_id)).fetchone()[0]
            self._validate_fencing_token(token)
            connection.execute("INSERT OR REPLACE INTO leases(resource_type, resource_id, agent_id, expires_at, fencing_token, heartbeat_at) VALUES(?,?,?,?,?,?)", (resource_type, resource_id, agent_id, expires, token, now()))
            connection.execute("INSERT INTO lease_history(resource_type, resource_id, agent_id, fencing_token, created_at) VALUES(?,?,?,?,?)", (resource_type, resource_id, agent_id, token, now()))
        return {"resource_type": resource_type, "resource_id": resource_id, "agent_id": agent_id, "expires_at": expires, "fencing_token": token}

    def heartbeat_lease(self, resource_type, resource_id, agent_id, fencing_token, ttl=60):
        self._validate_fencing_token(fencing_token)
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.transaction() as connection:
            updated = connection.execute("UPDATE leases SET expires_at=?, heartbeat_at=? WHERE resource_type=? AND resource_id=? AND agent_id=? AND fencing_token=? AND expires_at>?", (expires, now(), resource_type, resource_id, agent_id, fencing_token, now())).rowcount
            if not updated:
                raise WorkError("lease inexistente, expirada ou fencing inválido")
        return {"resource_type": resource_type, "resource_id": resource_id, "agent_id": agent_id, "expires_at": expires, "fencing_token": fencing_token}

    def recover_stale_leases(self):
        with self.transaction() as connection:
            return connection.execute("DELETE FROM leases WHERE expires_at<=?", (now(),)).rowcount

    def release_lease(self, resource_type, resource_id, agent_id, fencing_token):
        self._validate_fencing_token(fencing_token)
        with self.transaction() as connection:
            updated = connection.execute("UPDATE leases SET expires_at=?, heartbeat_at=? WHERE resource_type=? AND resource_id=? AND agent_id=? AND fencing_token=?", ("1970-01-01T00:00:00Z", now(), resource_type, resource_id, agent_id, fencing_token)).rowcount
            if not updated:
                raise WorkError("lease não pertence ao agente ou fencing inválido")

    def acquire_claim(self, worktree_id, path, agent_id, shared_readonly=False):
        target = self._safe_path(path)
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM claims WHERE project_id=? AND worktree_id=?", (self.project_id, worktree_id)).fetchall()
            overlaps = []
            for row in rows:
                existing = Path(row["path"])
                if target == existing or target in existing.parents or existing in target.parents:
                    overlaps.append(row)
            if overlaps and (not shared_readonly or any(not row["shared_readonly"] for row in overlaps)):
                raise WorkError("claim em conflito; caminhos sobrepostos exigem shared-readonly")
            claim_id = _uuid()
            connection.execute("INSERT INTO claims(claim_id, project_id, worktree_id, path, agent_id, shared_readonly, created_at) VALUES(?,?,?,?,?,?,?)", (claim_id, self.project_id, worktree_id, str(target), agent_id, int(shared_readonly), now()))
        return claim_id

    def release_claim(self, claim_id, agent_id):
        with self.transaction() as connection:
            updated = connection.execute("DELETE FROM claims WHERE claim_id=? AND agent_id=?", (claim_id, agent_id)).rowcount
            if not updated:
                raise WorkError("claim não pertence ao agente")

    def _safe_path(self, path):
        candidate = (self.worktree / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        try:
            candidate.relative_to(self.worktree)
        except ValueError as exc:
            raise WorkError("path fora do project root") from exc
        return candidate

    def write_handoff(self, agent_id, body, fmt="json"):
        clean = redact(body)
        handoff_id = _uuid()
        with self.transaction() as connection:
            connection.execute("INSERT INTO handoffs(handoff_id, project_id, agent_id, format, body, created_at) VALUES(?,?,?,?,?,?)", (handoff_id, self.project_id, agent_id, fmt, json_text(clean) if fmt == "json" else str(clean)[:12000], now()))
        return handoff_id

    def read_handoffs(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM handoffs ORDER BY created_at DESC")]

    def append_event(self, event_id, kind, metadata):
        with self.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO events(event_id, project_id, kind, metadata, created_at) VALUES(?,?,?,?,?)", (event_id, self.project_id, kind, json_text(metadata), now()))
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row)

    def timeline(self, limit=100):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,))]

    def compile_context(self, limit=20, max_chars=12000):
        files = []
        for pattern in ("README*.md", "TODO*.md"):
            files.extend(sorted(path for path in self.worktree.glob(pattern) if path.is_file()))
        files = files[:limit]
        decisions = []
        tasks = []
        with self.connect() as connection:
            decisions = [dict(row) for row in connection.execute("SELECT decision_id, title, body FROM decisions ORDER BY created_at DESC LIMIT 20")]
            tasks = [dict(row) for row in connection.execute("SELECT task_id, title, status FROM tasks ORDER BY updated_at DESC LIMIT 20")]
        handoffs = self.read_handoffs()[:5]
        bundle = {"project_id": self.project_id, "root": str(self.worktree), "files": [str(path.relative_to(self.worktree)) for path in files], "decisions": decisions, "tasks": tasks, "handoffs": handoffs}
        encoded = json_text(bundle)
        while len(encoded) > max_chars and (bundle["handoffs"] or bundle["decisions"] or bundle["tasks"] or bundle["files"]):
            if bundle["handoffs"]:
                bundle["handoffs"].pop()
            elif bundle["decisions"]:
                bundle["decisions"].pop()
            elif bundle["tasks"]:
                bundle["tasks"].pop()
            else:
                bundle["files"].pop()
            encoded = json_text(bundle)
        if len(encoded) > max_chars:
            bundle = {"project_id": self.project_id, "root": str(self.worktree)}
        return bundle


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(project_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, project_kind TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY, engine TEXT NOT NULL, provider TEXT, version TEXT, capabilities TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(project_id), worktree_id TEXT, cwd TEXT NOT NULL, tmux_visual TEXT, session_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, last_heartbeat TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(agent_id), started_at TEXT, stopped_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), title TEXT NOT NULL, description TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL, claimed_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, agent_id TEXT NOT NULL, expires_at TEXT NOT NULL, fencing_token INTEGER NOT NULL, heartbeat_at TEXT NOT NULL, PRIMARY KEY(resource_type, resource_id));
CREATE TABLE IF NOT EXISTS lease_fencing_counters(resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, PRIMARY KEY(resource_type, resource_id));
CREATE TABLE IF NOT EXISTS lease_history(lease_event_id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, agent_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, worktree_id TEXT NOT NULL, path TEXT NOT NULL, agent_id TEXT NOT NULL, shared_readonly INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(project_id, worktree_id, path, agent_id));
CREATE TABLE IF NOT EXISTS handoffs(handoff_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, agent_id TEXT NOT NULL, format TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, path TEXT NOT NULL, digest TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
"""
