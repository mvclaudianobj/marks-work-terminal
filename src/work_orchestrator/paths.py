import os
import pwd
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


def _runtime_is_safe(path: Path, uid: int | None = None) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    expected_uid = uid if uid is not None else os.geteuid()
    return (
        not stat.S_ISLNK(details.st_mode)
        and stat.S_ISDIR(details.st_mode)
        and details.st_uid == expected_uid
        and stat.S_IMODE(details.st_mode) & 0o022 == 0
    )


def _system_runtime() -> Path:
    return Path("/run/user") / str(os.geteuid())


def _root_launch_target_uid() -> int | None:
    if os.geteuid() != 0:
        return None
    allow = os.environ.get("WORK_ALLOW_ROOT_LAUNCH")
    target_uid_str = os.environ.get("WORK_TARGET_UID")
    if not allow or not target_uid_str:
        return None
    try:
        target_uid = int(target_uid_str)
        pw = pwd.getpwnam(allow)
        if pw.pw_uid == target_uid and target_uid > 0:
            return target_uid
    except (ValueError, KeyError):
        pass
    return None


def effective_uid() -> int:
    uid = _root_launch_target_uid()
    return uid if uid is not None else os.geteuid()


def ensure_private_directory(path: Path, uid: int | None = None) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
    except OSError as exc:
        raise WorkError(f"não foi possível preparar diretório privado {path}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise WorkError(f"diretório privado inseguro: {path}")
    expected_uid = uid if uid is not None else os.geteuid()
    if details.st_uid != expected_uid:
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
    if details.st_uid != effective_uid() or stat.S_IMODE(details.st_mode) & 0o077 or details.st_nlink != 1:
        raise WorkError(f"owner, permissões ou links inseguros: {path}")
    return details


def validate_trusted_executable(path: Path, *, checkout_root: Path, allowed_owners: set[int]) -> Path:
    if not path.is_absolute() or not checkout_root.is_absolute():
        raise WorkError("executável e checkout devem ser absolutos")
    if ".." in path.parts or ".." in checkout_root.parts:
        raise WorkError("executável ou checkout contém escape de caminho")
    try:
        resolved_root = checkout_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkError("executável não pertence ao checkout esperado") from exc
    lexical = path
    while True:
        try:
            details = lexical.lstat()
        except OSError as exc:
            raise WorkError(f"componente do checkout inacessível: {lexical}") from exc
        if stat.S_ISLNK(details.st_mode):
            raise WorkError(f"componente do checkout inseguro: {lexical}")
        if lexical == checkout_root:
            break
        if lexical.parent == lexical:
            raise WorkError("executável não pertence ao checkout esperado")
        lexical = lexical.parent
    chain = []
    current = resolved_path
    while True:
        chain.append(current)
        if current == resolved_root:
            break
        current = current.parent
    for current in chain:
        try:
            details = current.lstat()
        except OSError as exc:
            raise WorkError(f"componente do checkout inacessível: {current}") from exc
        if stat.S_ISLNK(details.st_mode) or details.st_uid not in allowed_owners or stat.S_IMODE(details.st_mode) & 0o022:
            raise WorkError(f"componente do checkout inseguro: {current}")
        if current == resolved_path:
            if not stat.S_ISREG(details.st_mode) or not details.st_mode & stat.S_IXUSR or details.st_nlink != 1:
                raise WorkError(f"executável work inseguro: {resolved_path}")
        elif not stat.S_ISDIR(details.st_mode):
            raise WorkError(f"componente do checkout inseguro: {current}")
    return resolved_path


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    runtime: Path

    @classmethod
    def discover(cls) -> "Paths":
        home = Path.home()
        target_uid = _root_launch_target_uid()
        if target_uid is not None:
            try:
                pw = pwd.getpwuid(target_uid)
            except KeyError as exc:
                raise WorkError(f"usuário target uid={target_uid} não encontrado") from exc
            config_home = _absolute_xdg(pw.pw_dir + "/.config", "XDG_CONFIG_HOME")
            state_home = _absolute_xdg(pw.pw_dir + "/.local/state", "XDG_STATE_HOME")
            runtime_home = Path("/run/user") / str(target_uid)
            if not _runtime_is_safe(runtime_home, uid=target_uid):
                raise WorkError(f"runtime root-launch inseguro: {runtime_home}")
        else:
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
            and details.st_uid == effective_uid()
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
        uid = _root_launch_target_uid()
        ensure_private_directory(self.config, uid=uid)
        ensure_private_directory(self.state, uid=uid)
        ensure_private_directory(self.runtime, uid=uid)
        ensure_private_directory(self.state / "workspaces", uid=uid)


def validate_slug(value: str) -> str:
    if not SLUG.fullmatch(value):
        raise WorkError("slug inválido: use 1-63 caracteres minúsculos, números e hífens")
    return value


def safe_child(parent: Path, name: str) -> Path:
    candidate = parent / name
    if candidate.parent.resolve() != parent.resolve():
        raise WorkError("caminho derivado escapou do diretório permitido")
    return candidate
