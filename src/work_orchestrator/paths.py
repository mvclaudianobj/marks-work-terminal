import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkError


SLUG = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _absolute_xdg(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkError(f"{label} deve ser absoluto")
    return path


def _runtime_is_safe(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(details.st_mode)
        and stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.geteuid()
        and stat.S_IMODE(details.st_mode) & 0o022 == 0
    )


def _system_runtime() -> Path:
    return Path("/run/user") / str(os.geteuid())


def ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
    except OSError as exc:
        raise WorkError(f"não foi possível preparar diretório privado {path}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise WorkError(f"diretório privado inseguro: {path}")
    if details.st_uid != os.geteuid():
        raise WorkError(f"diretório privado pertence a outro usuário: {path}")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise WorkError(f"não foi possível proteger diretório {path}: {exc}") from exc


def ensure_regular_private_file(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise WorkError(f"arquivo inseguro ou inacessível {path}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise WorkError(f"arquivo inseguro: {path}")
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077 or details.st_nlink != 1:
        raise WorkError(f"owner, permissões ou links inseguros: {path}")
    return details


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    runtime: Path

    @classmethod
    def discover(cls) -> "Paths":
        home = Path.home()
        config_home = _absolute_xdg(os.environ.get("XDG_CONFIG_HOME", home / ".config"), "XDG_CONFIG_HOME")
        state_home = _absolute_xdg(os.environ.get("XDG_STATE_HOME", home / ".local/state"), "XDG_STATE_HOME")
        runtime_value = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_value:
            runtime_home = _absolute_xdg(runtime_value, "XDG_RUNTIME_DIR")
            if not _runtime_is_safe(runtime_home):
                raise WorkError(f"XDG_RUNTIME_DIR inseguro: {runtime_home}")
        else:
            system_runtime = _system_runtime()
            runtime_home = system_runtime if _runtime_is_safe(system_runtime) else state_home / "runtime"
        return cls(
            config_home / "work-orchestrator" / "projects",
            state_home / "work-orchestrator",
            runtime_home / "work-orchestrator",
        )

    @property
    def socket(self) -> Path:
        return self.runtime / "tmux.sock"

    @property
    def legacy_socket(self) -> Path:
        return self.state.parent / "runtime" / "work-orchestrator" / "tmux.sock"

    @property
    def runtime_diagnostic(self) -> dict[str, object]:
        legacy = self.legacy_socket
        different = legacy != self.socket
        try:
            details = legacy.lstat() if different else None
        except OSError:
            details = None
        active = bool(
            details
            and stat.S_ISSOCK(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and details.st_uid == os.geteuid()
            and stat.S_IMODE(details.st_mode) & 0o077 == 0
        )
        return {
            "canonical_socket": str(self.socket),
            "legacy_fallback": {
                "path": str(legacy),
                "different": different,
                "active": active,
                "conflict": different and active,
            },
        }

    def ensure(self) -> None:
        ensure_private_directory(self.config)
        ensure_private_directory(self.state)
        ensure_private_directory(self.runtime)
        ensure_private_directory(self.state / "workspaces")


def validate_slug(value: str) -> str:
    if not SLUG.fullmatch(value):
        raise WorkError("slug inválido: use 1-63 caracteres minúsculos, números e hífens")
    return value


def safe_child(parent: Path, name: str) -> Path:
    candidate = parent / name
    if candidate.parent.resolve() != parent.resolve():
        raise WorkError("caminho derivado escapou do diretório permitido")
    return candidate
