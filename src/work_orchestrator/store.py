import json
import hashlib
import fcntl
import os
import tempfile
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import WorkError
from .paths import ensure_private_directory, ensure_regular_private_file, effective_uid


SCHEMA = 2


def snapshot_path(state: Path, slug: str) -> Path:
    return state / "snapshots" / f"{slug}.json"


def token_path(state: Path, slug: str) -> Path:
    return state / "ownership" / f"{slug}.token"


def observability_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.jsonl"


def observability_pid_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.pid"


def observability_sequence_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.sequence"


def observability_dedup_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.dedup"


def observability_signal_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.signals.jsonl"


def observability_signal_processing_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.signals.processing"


def observability_agentic_state_path(state: Path, slug: str) -> Path:
    return state / "observability" / f"{slug}.agentic-state.json"


def _identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (details.st_dev, details.st_ino, details.st_uid, details.st_gid, details.st_mode, details.st_size, details.st_mtime_ns, details.st_ctime_ns, details.st_nlink)


def append_observation(path: Path, payload: bytes, max_bytes: int = 1024 * 1024, expected_fingerprint: str | None = None, rotate: bool = True) -> None:
    ensure_private_directory(path.parent)
    if len(payload) > 16384 or not payload.endswith(b"\n"):
        raise WorkError("evento de observabilidade excede o limite")
    current_details = None
    if path.exists() or path.is_symlink():
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            current_details = os.fstat(descriptor)
            if not stat.S_ISREG(current_details.st_mode) or current_details.st_uid != effective_uid() or stat.S_IMODE(current_details.st_mode) != 0o600 or current_details.st_nlink != 1:
                raise WorkError(f"arquivo de observabilidade inseguro: {path}")
            current = path.lstat()
            if _identity(current) != _identity(current_details):
                raise WorkError(f"arquivo mudou antes da publicação: {path}")
            if expected_fingerprint is not None:
                payload_before = os.read(descriptor, max_bytes + 1)
                if len(payload_before) > max_bytes:
                    raise WorkError(f"arquivo excede o limite: {path}")
                if _fingerprint(current_details, payload_before) != expected_fingerprint:
                    raise WorkError(f"arquivo mudou antes da publicação: {path}")
        finally:
            os.close(descriptor)
        if current_details.st_size + len(payload) > max_bytes:
            if not rotate:
                raise WorkError(f"arquivo excederia o limite: {path}")
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.exists() or rotated.is_symlink():
                ensure_regular_private_file(rotated)
                rotated.unlink()
            os.replace(path, rotated)
            _fsync_directory(path.parent)
            current_details = None
    elif expected_fingerprint is not None:
        raise WorkError(f"arquivo mudou antes da publicação: {path}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        details = os.fstat(descriptor)
        if current_details is not None and _identity(details) != _identity(current_details):
            raise WorkError(f"arquivo mudou antes da publicação: {path}")
        if not stat.S_ISREG(details.st_mode) or details.st_uid != effective_uid() or stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1:
            raise WorkError(f"arquivo de observabilidade inseguro: {path}")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(after.st_mode) or after.st_uid != effective_uid() or stat.S_IMODE(after.st_mode) != 0o600 or after.st_nlink != 1 or _identity(current) != _identity(after) or after.st_size != details.st_size + len(payload):
            raise WorkError(f"arquivo mudou durante a publicação: {path}")
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def append_spool(path: Path, payload: bytes, max_bytes: int = 1024 * 1024) -> None:
    append_observation(path, payload, max_bytes=max_bytes, rotate=False)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rename_private_file(source: Path, destination: Path) -> None:
    ensure_private_directory(source.parent)
    if source.parent != destination.parent or destination.exists() or destination.is_symlink():
        raise WorkError("destino de rename privado inválido")
    ensure_regular_private_file(source)
    os.replace(source, destination)
    ensure_regular_private_file(destination)
    _fsync_directory(destination.parent)


def unlink_private_file(path: Path) -> None:
    ensure_regular_private_file(path)
    path.unlink()
    _fsync_directory(path.parent)


@contextmanager
def _publication_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkError(f"falha ao serializar publicação em {path}: {exc}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != effective_uid() or stat.S_IMODE(details.st_mode) & 0o077:
            raise WorkError(f"diretório privado inseguro: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except WorkError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise WorkError(f"falha ao serializar publicação em {path}: {exc}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fingerprint(details: os.stat_result, payload: bytes) -> str:
    if details.st_nlink != 1:
        raise WorkError("arquivo com hard links não é aceito")
    metadata = (
        details.st_dev,
        details.st_ino,
        details.st_uid,
        details.st_gid,
        stat.S_IMODE(details.st_mode),
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        details.st_nlink,
    )
    digest = hashlib.sha256(repr(metadata).encode("ascii") + b"\0" + payload).hexdigest()
    return f"v1:{digest}"


def _read_private_bytes(path: Path, *, max_bytes: int | None = None) -> tuple[bytes, str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise WorkError(f"arquivo inexistente: {path}") from exc
    except OSError as exc:
        raise WorkError(f"arquivo inseguro ou inacessível {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != effective_uid() or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1:
            raise WorkError(f"owner, tipo, permissões ou links inseguros: {path}")
        if max_bytes is not None and before.st_size > max_bytes:
            raise WorkError(f"arquivo excede o limite: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(None if max_bytes is None else max_bytes + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except WorkError:
        raise
    except FileNotFoundError as exc:
        raise WorkError(f"arquivo inexistente: {path}") from exc
    except OSError as exc:
        raise WorkError(f"arquivo inseguro ou inacessível {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    identity_before = _identity(before)
    identity_after = _identity(after)
    identity_current = _identity(current)
    if identity_before != identity_after or identity_after != identity_current:
        raise WorkError(f"arquivo mudou durante a leitura: {path}")
    if max_bytes is not None and len(payload) > max_bytes:
        raise WorkError(f"arquivo excede o limite: {path}")
    if not stat.S_ISREG(current.st_mode) or current.st_uid != effective_uid() or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1:
        raise WorkError(f"owner, tipo, permissões ou links inseguros: {path}")
    return payload, _fingerprint(after, payload), after


def read_private_bytes(path: Path, *, max_bytes: int | None = None) -> tuple[bytes, str]:
    payload, fingerprint, _ = _read_private_bytes(path, max_bytes=max_bytes)
    return payload, fingerprint


def read_private_bounded_text_with_fingerprint(path: Path, max_bytes: int) -> tuple[str, str]:
    try:
        payload, fingerprint, _ = _read_private_bytes(path, max_bytes=max_bytes)
        return payload.decode("utf-8"), fingerprint
    except UnicodeError as exc:
        raise WorkError(f"não foi possível ler {path}: {exc}") from exc


def _conditional_publish(path: Path, temporary: Path, expected_fingerprint: str) -> None:
    _, current_fingerprint = read_private_bytes(path)
    if current_fingerprint != expected_fingerprint:
        raise WorkError("configuração mudou concorrentemente; nenhuma alteração foi publicada")
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def write_bytes_atomic(
    path: Path,
    payload: bytes,
    *,
    exclusive: bool = False,
    expected_fingerprint: str | None = None,
) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary_text = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_text)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        with _publication_lock(path.parent):
            if exclusive:
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except FileExistsError as exc:
                    raise WorkError(f"arquivo já existe: {path}") from exc
                temporary.unlink()
                _fsync_directory(path.parent)
            elif expected_fingerprint is not None:
                _conditional_publish(path, temporary, expected_fingerprint)
            else:
                if path.exists() or path.is_symlink():
                    ensure_regular_private_file(path)
                os.replace(temporary, path)
                _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    try:
        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        write_bytes_atomic(path, payload)
    except WorkError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WorkError(f"não foi possível gravar {path}: {exc}") from exc


def read_private_text(path: Path) -> str:
    try:
        payload, _ = read_private_bytes(path)
        return payload.decode("utf-8")
    except UnicodeError as exc:
        raise WorkError(f"não foi possível ler {path}: {exc}") from exc


def read_private_bounded_text(path: Path, max_bytes: int) -> str:
    try:
        payload, _ = read_private_bytes(path, max_bytes=max_bytes)
        return payload.decode("utf-8")
    except UnicodeError as exc:
        raise WorkError(f"não foi possível ler {path}: {exc}") from exc


def read_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        raise WorkError(f"snapshot inexistente: {path}")
    try:
        value = json.loads(read_private_text(path))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise WorkError(f"snapshot inválido: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise WorkError("schema de snapshot incompatível")
    return value
