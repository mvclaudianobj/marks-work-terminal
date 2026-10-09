import fcntl
import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from .errors import WorkError
from .paths import ensure_private_directory, _root_launch_target_uid, effective_uid


LOCK_ORDER = "workspace -> project -> session"


def _lock_path(runtime: Path, name: str) -> Path:
    return runtime / "locks" / name


def lock_names(slug: str, socket: Path, session: str) -> tuple[str, str]:
    identity = hashlib.sha256(f"{socket}\0{session}".encode("utf-8")).hexdigest()
    return f"project-{slug}.lock", f"session-{identity}.lock"


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        details = os.fstat(descriptor)
        expected_uid = effective_uid()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != expected_uid or stat.S_IMODE(details.st_mode) & 0o077:
            raise WorkError(f"lock inseguro: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except WorkError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise WorkError(f"falha ao adquirir lock {path}: {exc}") from exc
    try:
        yield
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@contextmanager
def project_lock(runtime: Path, slug: str) -> Iterator[None]:
    directory = runtime / "locks"
    ensure_private_directory(directory, uid=_root_launch_target_uid())
    with _lock(_lock_path(runtime, f"project-{slug}.lock")):
        yield


@contextmanager
def integration_lock(runtime: Path, slug: str) -> Iterator[None]:
    directory = runtime / "locks"
    ensure_private_directory(directory, uid=_root_launch_target_uid())
    with _lock(_lock_path(runtime, f"integration-{slug}.lock")):
        yield


@contextmanager
def session_lock(runtime: Path, socket: Path, session: str) -> Iterator[None]:
    directory = runtime / "locks"
    ensure_private_directory(directory, uid=_root_launch_target_uid())
    identity = hashlib.sha256(f"{socket}\0{session}".encode("utf-8")).hexdigest()
    with _lock(_lock_path(runtime, f"session-{identity}.lock")):
        yield


@contextmanager
def project_locks(runtime: Path, slug: str, socket: Path, session: str) -> Iterator[None]:
    """Acquire the project and session locks in the global order.

    Aggregated operations must acquire workspace_lock first. Autosave must
    release these locks before starting an aggregated workspace operation.
    """
    with ExitStack() as stack:
        stack.enter_context(project_lock(runtime, slug))
        stack.enter_context(session_lock(runtime, socket, session))
        yield


@contextmanager
def observability_lock(runtime: Path, slug: str) -> Iterator[None]:
    directory = runtime / "locks"
    ensure_private_directory(directory, uid=_root_launch_target_uid())
    with _lock(_lock_path(runtime, f"observability-{slug}.lock")):
        yield
