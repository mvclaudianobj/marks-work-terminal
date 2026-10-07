import fcntl
import os
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .redaction import redact


LOG_LIMIT = 64 * 1024
def _validate_descriptor(descriptor: int, path: Path) -> None:
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise OSError(f"arquivo de log inseguro: {path}")


@contextmanager
def _log_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise OSError(f"falha ao abrir lock de log: {lock_path}") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise OSError(f"lock de log inseguro: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise OSError(f"falha ao adquirir lock de log: {lock_path}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def append_log(path: Path, message: str, *, limit: int = LOG_LIMIT) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _log_lock(path):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            _validate_descriptor(descriptor, path)
            os.fchmod(descriptor, 0o600)
            payload = f"{datetime.now().astimezone().isoformat()} {redact(message, limit=limit)}\n".encode("utf-8", "replace")
            if len(payload) > limit:
                payload = payload[:limit]
            current_size = os.fstat(descriptor).st_size
            if current_size + len(payload) > limit:
                os.ftruncate(descriptor, 0)
            payload = payload[: max(0, limit - os.fstat(descriptor).st_size)]
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            if os.fstat(descriptor).st_size > limit:
                os.ftruncate(descriptor, limit)
        finally:
            os.close(descriptor)
