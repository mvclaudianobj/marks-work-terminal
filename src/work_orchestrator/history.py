import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from .errors import WorkError
from .paths import ensure_private_directory, ensure_regular_private_file
from .redaction import redact as redact_text

def redact(value: str, root: Path | None = None, limit: int = 4096) -> str:
    value = redact_text(value, limit=limit)
    if root is not None:
        value = value.replace(str(root.resolve()), "<root>")
        value = re.sub(r"(?<![\w])/(?:[^\s/]+/){1,}[^\s]+", "<path>", value)
    return value[:limit]

def history_path(state: Path) -> Path:
    return state / "history.jsonl"

def append(state: Path, kind: str, payload: dict[str, object], *, root: Path | None = None, enabled: bool = False) -> None:
    if not enabled:
        return
    if not kind or not isinstance(payload, dict):
        raise WorkError("evento de histórico inválido")
    ensure_private_directory(state)
    path = history_path(state)
    if path.exists() or path.is_symlink():
        ensure_regular_private_file(path)
    clean = {key: redact(str(value), root) if isinstance(value, str) else value for key, value in payload.items()}
    line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind, "data": clean}, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    os.chmod(path, 0o600)

def read(state: Path, limit: int = 100) -> list[dict[str, object]]:
    path = history_path(state)
    if not path.exists():
        return []
    ensure_regular_private_file(path)
    return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()[-limit:] if row]
