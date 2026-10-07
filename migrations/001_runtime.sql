PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;

CREATE TABLE IF NOT EXISTS projects(project_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, project_kind TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY, engine TEXT NOT NULL, provider TEXT, version TEXT, capabilities TEXT NOT NULL, project_id TEXT NOT NULL, worktree_id TEXT, cwd TEXT NOT NULL, tmux_visual TEXT, session_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, last_heartbeat TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, started_at TEXT, stopped_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL, claimed_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, agent_id TEXT NOT NULL, expires_at TEXT NOT NULL, fencing_token INTEGER NOT NULL, heartbeat_at TEXT NOT NULL, PRIMARY KEY(resource_type, resource_id));
CREATE TABLE IF NOT EXISTS lease_fencing_counters(resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, PRIMARY KEY(resource_type, resource_id));
CREATE TABLE IF NOT EXISTS lease_history(lease_event_id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, agent_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, worktree_id TEXT NOT NULL, path TEXT NOT NULL, agent_id TEXT NOT NULL, shared_readonly INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoffs(handoff_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, agent_id TEXT NOT NULL, format TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, path TEXT NOT NULL, digest TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
