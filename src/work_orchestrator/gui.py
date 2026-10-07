import logging
import json
import hashlib
import os
import re
import shlex
import stat
import subprocess
import threading
import time
import unicodedata
import pwd
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .cli import init_project
from .errors import WorkError
from .monitor import Monitor
from .locking import project_lock
from .logging_utils import LOG_LIMIT, append_log, redact
from .paths import Paths, ensure_private_directory, validate_slug
from .service import Service
from .runtime import ENGINES, Runtime


ZENITY = "/usr/bin/zenity"
TERMINATOR = "/usr/bin/terminator"
LOGGER = logging.getLogger(__name__)
TERMINAL_REMOVED_ENVIRONMENT = frozenset({
    "PYTHONHOME",
    "PYTHONPATH",
    "WORK_EXECUTABLE",
    "WORK_ORCHESTRATOR_EXECUTABLE",
    "WORK_ORCHESTRATOR_TESTING",
    "WORK_TMUX_SOCKET",
})


def _run_zenity(arguments: Sequence[str]) -> str | None:
    result = subprocess.run(
        [ZENITY, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=300,
    )
    if result.returncode in {1, 5}:
        return None
    if result.returncode != 0:
        raise WorkError(f"Zenity falhou com código {result.returncode}")
    return result.stdout.rstrip("\n")


def show_error(message: str) -> None:
    try:
        subprocess.run(
            [ZENITY, "--error", "--title=Work Orchestrator", f"--text={message}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        LOGGER.error("não foi possível exibir a mensagem de erro")


def suggested_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:63].rstrip("-")
    return slug or "novo-projeto"


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def project_state(item: dict[str, Any]) -> str:
    if item.get("error"):
        return "erro"
    if item.get("active"):
        state = "ativo"
    elif item.get("snapshot"):
        state = "salvo/restaurável"
    else:
        state = "novo"
    legacy = item.get("runtime", {}).get("legacy_fallback", {})
    if legacy.get("conflict"):
        return f"{state}; conflito de runtime legado"
    return state


def sort_projects(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            not bool(item.get("active")),
            -_timestamp(item.get("snapshot_timestamp")),
            str(item.get("name") or item.get("slug") or "").casefold(),
        ),
    )


def _work_executable() -> Path:
    override = os.environ.get("WORK_EXECUTABLE")
    candidate = Path(override).expanduser() if override else Path(__file__).resolve().parents[2] / "bin" / "work"
    if not candidate.is_absolute():
        raise WorkError("WORK_EXECUTABLE deve ser absoluto")
    try:
        details = candidate.lstat()
    except (OSError, RuntimeError) as exc:
        raise WorkError("executável work não foi encontrado") from exc
    allowed_owners = {os.geteuid(), 0}
    if (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)
            or details.st_uid not in allowed_owners
            or stat.S_IMODE(details.st_mode) & 0o022
            or not stat.S_IXUSR & details.st_mode):
        raise WorkError(f"executável work inseguro: {candidate}")
    return candidate


def _root_launch_identity() -> tuple[pwd.struct_passwd, Path] | None:
    if os.geteuid() != 0:
        return None
    try:
        target = pwd.getpwnam("marcos")
    except KeyError as exc:
        raise WorkError("usuário marcos não existe para launch root-like") from exc
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    if os.environ.get("WORK_ALLOW_ROOT_LAUNCH") != "marcos" or os.environ.get("WORK_TARGET_UID") != str(target.pw_uid):
        raise WorkError("launch root-like exige allowlist explícita de marcos")
    if runtime != Path("/run/user") / str(target.pw_uid):
        raise WorkError("runtime root-like não está allowlisted para marcos")
    try:
        details = runtime.lstat()
    except OSError as exc:
        raise WorkError("runtime root-like inacessível") from exc
    if (stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != target.pw_uid or stat.S_IMODE(details.st_mode) & 0o022):
        raise WorkError("runtime root-like inseguro")
    return target, runtime


def _drop_to_user(uid: int, gid: int) -> None:
    os.initgroups("marcos", gid)
    os.setgid(gid)
    os.setuid(uid)


def _terminal_environment(paths: Paths) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in TERMINAL_REMOVED_ENVIRONMENT
        and not key.startswith("WORK_")
        and key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    identity = _root_launch_identity()
    if identity is not None:
        target, runtime = identity
        environment.update({
            "HOME": target.pw_dir,
            "USER": target.pw_name,
            "LOGNAME": target.pw_name,
            "XDG_RUNTIME_DIR": str(runtime),
        })
    else:
        environment["XDG_RUNTIME_DIR"] = str(paths.runtime.parent)
    return environment


def _write_gui_log(paths: Paths, message: str) -> None:
    try:
        append_log(paths.state / "logs" / "gui.log", message)
    except OSError as exc:
        LOGGER.error("não foi possível registrar falha do launcher: %s", exc)


def _monitor_terminator(paths: Paths, process: subprocess.Popen[str], marker: Path | None = None, config_path: Path | None = None) -> None:
    try:
        try:
            _, stderr = process.communicate()
        except (OSError, subprocess.SubprocessError) as exc:
            _write_gui_log(paths, f"falha ao monitorar Terminator: {type(exc).__name__}: {exc}")
            return
        if process.returncode:
            detail = stderr.strip() if stderr else "sem stderr"
            _write_gui_log(paths, f"Terminator retornou código {process.returncode}: {detail}")
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)
        if config_path is not None:
            config_path.unlink(missing_ok=True)


def _claim_visual(paths: Paths, workspace: str, slug: str, visual_id: str) -> Path | None:
    directory = paths.runtime / "visual"
    ensure_private_directory(directory)
    digest = hashlib.sha256(f"{workspace}\0{slug}\0{visual_id}".encode("utf-8")).hexdigest()
    marker = directory / f"{slug}-{digest}.claim"
    for attempt in range(2):
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        except FileExistsError:
            details = marker.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
                raise WorkError(f"claim visual inseguro: {marker}")
            if time.time() - details.st_mtime < 30:
                return None
            marker.unlink()
            if attempt == 0:
                continue
            return None
        except OSError as exc:
            raise WorkError(f"não foi possível reservar visual: {exc}") from exc
        else:
            os.close(descriptor)
            return marker
    return None


def _claim_visual_tabs(paths: Paths, workspace: str, slug: str) -> Path | None:
    directory = paths.runtime / "visual"
    ensure_private_directory(directory)
    digest = hashlib.sha256(f"{workspace}\0{slug}\0terminator-tabs".encode("utf-8")).hexdigest()
    marker = directory / f"{slug}-{digest}.tabs.claim"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    except FileExistsError:
        details = marker.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
            raise WorkError(f"claim visual inseguro: {marker}")
        if time.time() - details.st_mtime < 30:
            return None
        marker.unlink()
        return _claim_visual_tabs(paths, workspace, slug)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("tabs\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    return marker


def _terminal_command(slug: str, visual_id: str, window: str | None) -> str:
    arguments = [str(_work_executable()), "open", slug, "--visual-id", visual_id]
    if window is not None:
        arguments.extend(("--window", window))
    return shlex.join(arguments)


def _configobj_quote(value: str) -> str:
    # ConfigObj aceita aspas triplas para strings com aspas/caracteres especiais.
    # Limitação conhecida: um valor contendo a sequência literal `"""` quebraria
    # este escaping; dado que os valores vêm de slug validado, root resolvido e
    # comando montado via shlex.join, essa sequência é considerada impossível
    # na prática e não é tratada aqui.
    return f'"""{value}"""'


def _configobj_list_item(value: str) -> str:
    if any(char in value for char in "\x00\r\n"):
        raise WorkError("item de lista ConfigObj inválido")
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise WorkError("item de lista ConfigObj não pode ser serializado")


def build_terminator_layout(
    tabs: Sequence[dict[str, Any]],
    slug: str,
    profile: str = "default",
    window_title: str | None = None,
) -> str:
    validate_slug(slug)
    if not tabs or not isinstance(profile, str) or not profile or any(char in profile for char in "\x00\r\n"):
        raise WorkError("layout Terminator inválido")
    if window_title is not None and (
        not isinstance(window_title, str)
        or not window_title
        or any(char in window_title for char in "\x00\r\n")
        or '"""' in window_title
    ):
        raise WorkError("título de janela Terminator inválido")
    normalized: list[dict[str, Any]] = []
    for tab in tabs:
        required = {"id", "title", "root", "command", "window"}
        if not isinstance(tab, dict) or set(tab) != required:
            raise WorkError("tab Terminator inválida")
        if not all(isinstance(tab[key], str) and tab[key] for key in {"id", "title", "root"}):
            raise WorkError("tab Terminator inválida")
        if not isinstance(tab["command"], str):
            raise WorkError("tab Terminator inválida")
        if tab["window"] is not None and not isinstance(tab["window"], str):
            raise WorkError("janela Terminator inválida")
        normalized.append(tab)
    lines: list[str] = []
    lines.append("[layouts]")
    lines.append(f"  [[{slug}]]")
    lines.append("    [[[window]]]")
    lines.append("      type = Window")
    lines.append('      parent = ""')
    lines.append("      order = 0")
    if window_title is not None:
        lines.append(f"      title = {_configobj_quote(window_title)}")
    multi = len(normalized) > 1
    if multi:
        lines.append("    [[[notebook]]]")
        lines.append("      type = Notebook")
        lines.append("      parent = window")
        lines.append("      order = 0")
        labels = ", ".join(_configobj_list_item(tab["title"]) for tab in normalized)
        lines.append(f"      labels = {labels}")
    for index, tab in enumerate(normalized):
        lines.append(f"    [[[term{index}]]]")
        lines.append("      type = Terminal")
        lines.append(f"      parent = {'notebook' if multi else 'window'}")
        lines.append(f"      order = {index}")
        lines.append(f"      profile = {profile}")
        lines.append(f"      directory = {_configobj_quote(tab['root'])}")
        lines.append(f"      command = {_configobj_quote(tab['command'])}")
        lines.append(f"      title = {_configobj_quote(tab['title'])}")
    return "\n".join(lines) + "\n"


def _write_terminator_config(paths: Paths, slug: str, content: str) -> Path:
    directory = paths.runtime / "terminator"
    ensure_private_directory(directory)
    for _ in range(8):
        path = directory / f"layout-{os.getpid()}-{time.time_ns()}.conf"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise WorkError("não foi possível criar layout Terminator privado")


def open_terminator_tabs(
    paths: Paths,
    slug: str,
    name: str,
    tabs: Sequence[dict[str, Any]],
    *,
    workspace: str = "default",
    profile: str = "default",
) -> bool:
    validate_slug(slug)
    validate_slug(workspace)
    normalized = []
    for tab in tabs:
        value = dict(tab)
        validate_slug(value["id"])
        value["command"] = _terminal_command(slug, value["id"], value["window"])
        normalized.append(value)
    if not normalized:
        return False
    marker = _claim_visual_tabs(paths, workspace, slug)
    if marker is None:
        return False
    try:
        content = build_terminator_layout(normalized, slug, profile, f"Projeto: {name}")
        config_path = _write_terminator_config(paths, slug, content)
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    popen_options = {}
    identity = _root_launch_identity()
    if identity is not None:
        popen_options["preexec_fn"] = lambda: _drop_to_user(identity[0].pw_uid, identity[0].pw_gid)
    argv = [TERMINATOR, "-u", "-g", str(config_path), "-l", slug]
    try:
        process = subprocess.Popen(
            argv,
            env=_terminal_environment(paths),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            **popen_options,
        )
    except OSError as exc:
        marker.unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)
        _write_gui_log(paths, f"falha ao iniciar Terminator: {type(exc).__name__}: {exc}")
        raise
    threading.Thread(target=_monitor_terminator, args=(paths, process, marker, config_path), daemon=True).start()
    return True


def select_project(
    items: Sequence[dict[str, Any]],
    *,
    title: str = "Carregar projeto",
    text: str = "Selecione um projeto",
) -> str | None:
    rows: list[str] = []
    for item in sort_projects(items):
        slug = str(item.get("slug", ""))
        rows.extend((
            slug,
            str(item.get("name") or slug),
            project_state(item),
            str(item.get("snapshot_timestamp") or "—"),
            str(item.get("root") or "—"),
        ))
    if not rows:
        _run_zenity(("--info", "--title=Work Orchestrator", "--text=Nenhum projeto configurado."))
        return None
    return _run_zenity((
        "--list",
        f"--title={title}",
        f"--text={text}",
        "--hide-column=1",
        "--print-column=1",
        "--column=Slug",
        "--column=Projeto",
        "--column=Estado",
        "--column=Último snapshot",
        "--column=Caminho",
        "--width=1000",
        "--height=560",
        "--",
        *rows,
    ))


def save_main() -> int:
    try:
        paths = Paths.discover()
        service = Service(paths)
        items = [item for item in service.projects() if not item.get("error") and item.get("active")]
        slug = select_project(items, title="Salvar projeto", text="Selecione um projeto tmux ativo")
        if slug is None:
            return 0
        saved = service.save(validate_slug(slug))
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        _run_zenity(("--info", "--title=Projeto salvo", f"--text=Snapshot salvo em {timestamp}\n{saved}"))
        return 0
    except (WorkError, OSError, ValueError) as exc:
        show_error(str(exc))
        return 2


def workspace_main() -> int:
    try:
        paths = Paths.discover()
        service = Service(paths)
        choice = _run_zenity((
            "--list", "--radiolist", "--title=Workspace", "--text=Escolha uma operação",
            "--column=Escolher", "--column=Operação", "TRUE", "Salvar", "FALSE", "Status", "FALSE", "Restaurar", "FALSE", "Abrir",
            "--hide-header", "--print-column=2", "--width=520", "--height=260",
        ))
        if choice is None:
            return 0
        name = _run_zenity(("--entry", "--title=Workspace", "--text=Nome do workspace", "--entry-text=default"))
        if name is None:
            return 0
        validate_slug(name)
        if choice == "Salvar":
            path = service.workspace_save(name, include_inactive=True)
            _run_zenity(("--info", "--title=Workspace salvo", f"--text={path}"))
            return 0
        if choice == "Status":
            result = service.workspace_status(name)
            _run_zenity(("--info", "--title=Status do workspace", f"--text={json.dumps(result, ensure_ascii=False)}"))
            return 0
        decision = _run_zenity(("--question", "--title=Restaurar workspace", "--text=Restaurar sessões e abrir exatamente uma janela Terminator por projeto visual?"))
        if decision is None:
            return 0
        result = service.workspace_restore(name, dry_run=False, visual="terminator-tabs")
        _run_zenity(("--info", "--title=Resultado do workspace", f"--text={json.dumps(result, ensure_ascii=False)}"))
        return 0
    except (WorkError, OSError, ValueError) as exc:
        show_error(str(exc))
        return 2


def open_terminator(
    paths: Paths,
    slug: str,
    name: str,
    root: Path,
    *,
    visual_id: str = "main",
    title: str | None = None,
    window: str | None = None,
    tmux: Any | None = None,
    session: str | None = None,
    workspace: str = "default",
    check_existing: bool = True,
) -> bool:
    validate_slug(slug)
    validate_slug(visual_id)
    validate_slug(workspace)
    if check_existing and tmux is not None and hasattr(tmux, "has_attached_client"):
        try:
            existing = tmux.has_attached_client(session or f"work-{slug}", window)
        except TypeError:
            existing = tmux.has_attached_client(session or f"work-{slug}")
        if existing:
            return False
    command = _terminal_command(slug, visual_id, window)
    window_title = title or (f"Work — {slug}" if name == slug else f"Work — {name} ({slug})")
    try:
        resolved = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkError(f"diretório raiz não existe: {root}") from exc
    if not resolved.is_dir():
        raise WorkError(f"raiz não é diretório: {resolved}")
    marker = _claim_visual(paths, workspace, slug, visual_id)
    if marker is None:
        return False
    try:
        content = build_terminator_layout(
            [{"id": visual_id, "title": window_title, "root": str(resolved), "command": command, "window": window}],
            slug,
            window_title=f"Projeto: {name}",
        )
        config_path = _write_terminator_config(paths, slug, content)
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    try:
        popen_options = {}
        identity = _root_launch_identity()
        if identity is not None:
            popen_options["preexec_fn"] = lambda: _drop_to_user(identity[0].pw_uid, identity[0].pw_gid)
        process = subprocess.Popen(
            [TERMINATOR, "-u", "-g", str(config_path), "-l", slug],
            env=_terminal_environment(paths),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            **popen_options,
        )
    except OSError as exc:
        marker.unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)
        _write_gui_log(paths, f"falha ao iniciar Terminator: {type(exc).__name__}: {exc}")
        raise
    threading.Thread(target=_monitor_terminator, args=(paths, process, marker, config_path), daemon=True).start()
    return True


def _monitor_state(value: dict[str, Any]) -> str:
    return "ativo" if value.get("active") else "inativo"


def select_monitor_project(items: Sequence[dict[str, Any]]) -> str | None:
    rows: list[str] = []
    for item in sort_projects(items):
        slug = str(item.get("slug", ""))
        rows.extend((
            slug,
            str(item.get("name") or slug),
            project_state(item),
            _monitor_state(item.get("monitor", {})),
            str(item.get("root") or "—"),
        ))
    if not rows:
        _run_zenity(("--info", "--title=Monitorar Projeto", "--text=Nenhum projeto configurado."))
        return None
    return _run_zenity((
        "--list",
        "--title=Monitorar Projeto",
        "--text=Selecione um projeto",
        "--hide-column=1",
        "--print-column=1",
        "--column=Slug",
        "--column=Projeto",
        "--column=Sessão",
        "--column=Monitor",
        "--column=Caminho",
        "--width=1000",
        "--height=560",
        "--",
        *rows,
    ))


def _monitor_action(state: dict[str, Any]) -> str | None:
    return _run_zenity((
        "--list",
        "--radiolist",
        "--title=Monitorar Projeto",
        f"--text=Monitor: {_monitor_state(state)}",
        "--hide-column=2",
        "--print-column=2",
        "--column=Selecionar",
        "--column=Ação",
        "--column=Descrição",
        "--",
        "TRUE",
        "status",
        "Mostrar estado e último evento",
        "FALSE",
        "start",
        "Iniciar monitor se estiver inativo",
        "FALSE",
        "stop",
        "Parar monitor ativo",
    ))


def _open_monitor_log(paths: Paths) -> Any:
    raise WorkError("abertura direta do log do monitor não é permitida")


def _monitor_process_log(paths: Paths, process: subprocess.Popen[bytes]) -> None:
    try:
        stream = process.stdout
        if stream is None:
            return
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if total < LOG_LIMIT:
                kept = chunk[:LOG_LIMIT - total]
                chunks.append(kept)
                total += len(kept)
        process.wait()
        if not chunks:
            return
        text = b"".join(chunks).decode("utf-8", "replace")
        append_log(paths.state / "logs" / "monitor.log", text)
    except (OSError, subprocess.SubprocessError) as exc:
        _write_gui_log(paths, f"falha ao monitorar monitor: {type(exc).__name__}: {exc}")


def start_monitor(paths: Paths, slug: str) -> dict[str, Any]:
    monitor = Monitor(paths)
    with project_lock(paths.runtime, slug):
        before = monitor.status(slug)
        if before["active"]:
            return before
        work_executable = _work_executable()
        try:
            process = subprocess.Popen(
                [str(work_executable), "monitor", "start", slug, "--interval", "5"],
                env=_terminal_environment(paths),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            _write_gui_log(paths, f"falha ao iniciar monitor: {type(exc).__name__}: {exc}")
            raise
        threading.Thread(target=_monitor_process_log, args=(paths, process), daemon=True).start()
        for _ in range(10):
            state = monitor.status(slug)
            if state["active"]:
                return state
            import time
            time.sleep(0.05)
        raise WorkError("monitor não confirmou o marker após o spawn")


def _show_monitor_status(state: dict[str, Any]) -> None:
    latest = state.get("latest") or {}
    details = f"Projeto: {state.get('project')}\nMonitor: {_monitor_state(state)}"
    if latest:
        details += f"\nEstado: {latest.get('state', 'desconhecido')}"
    _run_zenity(("--info", "--title=Status do monitor", f"--text={details}"))


def monitor_main() -> int:
    try:
        paths = Paths.discover()
        service = Service(paths)
        monitor = Monitor(paths)
        items = service.projects()
        for item in items:
            if not item.get("error"):
                try:
                    item["monitor"] = monitor.status(str(item["slug"]))
                except (WorkError, OSError, ValueError) as exc:
                    item["monitor"] = {"active": False, "error": str(exc)}
        slug = select_monitor_project(items)
        if slug is None:
            return 0
        slug = validate_slug(slug)
        state = monitor.status(slug)
        action = _monitor_action(state)
        if action is None:
            return 0
        if action == "status":
            _show_monitor_status(monitor.status(slug))
            return 0
        if action == "start":
            started = start_monitor(paths, slug)
            if state["active"]:
                _run_zenity(("--info", "--title=Monitorar Projeto", "--text=O monitor já está ativo; nenhum processo foi duplicado."))
            else:
                _show_monitor_status(started)
            return 0
        if action == "stop":
            monitor.stop(slug)
            _show_monitor_status(monitor.status(slug))
            return 0
        raise WorkError("ação de monitor inválida")
    except (WorkError, OSError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.error("launcher de monitor falhou: %s", exc)
        show_error(str(exc))
        return 2


def _entry(title: str, text: str, value: str) -> str | None:
    return _run_zenity(("--entry", f"--title={title}", f"--text={text}", f"--entry-text={value}"))


def _open_project_visuals(paths: Paths, service: Service, item: dict[str, Any], workspace: str = "default") -> None:
    slug = validate_slug(str(item["slug"]))
    name = str(item.get("name") or slug)
    visuals = sorted(item.get("visuals") or [], key=lambda visual: visual["order"])
    if len(visuals) <= 1:
        open_terminator(paths, slug, name, Path(str(item["root"])))
        return
    service.open(slug, attach=False)
    tabs = []
    for visual in visuals:
        window = visual.get("attach_target", {}).get("window")
        service.prepare_visual(slug, visual["id"], window)
        tabs.append({
            "id": visual["id"],
            "title": visual.get("title") or f"{name} — {visual['id']}",
            "root": str(Path(str(visual["root"])).expanduser().resolve(strict=True)),
            "command": "",
            "window": window,
        })
    open_terminator_tabs(paths, slug, name, tabs, workspace=workspace)


def load_main() -> int:
    try:
        paths = Paths.discover()
        service = Service(paths)
        items = service.projects()
        slug = select_project(items)
        if slug is None:
            return 0
        slug = validate_slug(slug)
        item = next((candidate for candidate in items if candidate.get("slug") == slug), None)
        if item is None or item.get("error"):
            raise WorkError(str(item.get("error")) if item else "projeto selecionado não encontrado")
        legacy = item.get("runtime", {}).get("legacy_fallback", {})
        if legacy.get("conflict"):
            _run_zenity((
                "--warning",
                "--title=Conflito de runtime legado",
                "--text=Há um runtime legado ativo. A sessão canônica será aberta sem bloqueio.",
            ))
        _open_project_visuals(paths, service, item)
        return 0
    except (WorkError, OSError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.error("launcher de carregamento falhou: %s", exc)
        show_error(str(exc))
        return 2


def new_main() -> int:
    try:
        selected = _run_zenity(("--file-selection", "--directory", "--title=Diretório do novo projeto"))
        if selected is None:
            return 0
        root = Path(selected).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkError(f"caminho selecionado não é diretório: {root}")
        default_name = root.name or "Novo projeto"
        name = _entry("Novo projeto", "Nome amigável", default_name)
        if name is None:
            return 0
        if not name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise WorkError("nome do projeto deve ser texto não vazio sem controles")
        slug = _entry("Novo projeto", "Slug", suggested_slug(name))
        if slug is None:
            return 0
        slug = validate_slug(slug)
        layout = _run_zenity((
            "--list",
            "--radiolist",
            "--title=Layout inicial",
            "--text=Escolha um layout sem comandos automáticos",
            "--hide-column=2",
            "--print-column=2",
            "--column=Selecionar",
            "--column=Layout",
            "--column=Descrição",
            "--",
            "TRUE",
            "recommended",
            "Recomendado: 6 janelas independentes / 6 panes shell",
            "FALSE",
            "minimal",
            "Mínimo: 1 janela / 1 pane",
        ))
        if layout is None:
            return 0
        paths = Paths.discover()
        paths.ensure()
        init_project(paths, slug, root, name.strip(), layout)
        if layout == "recommended":
            service = Service(paths)
            item = next((candidate for candidate in service.projects() if candidate.get("slug") == slug), None)
            if item is None or item.get("error"):
                raise WorkError(str(item.get("error")) if item else "projeto criado não encontrado")
            _open_project_visuals(paths, service, item)
        else:
            open_terminator(paths, slug, name.strip(), root)
        return 0
    except (WorkError, OSError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.error("launcher de criação falhou: %s", exc)
        show_error(str(exc))
        return 2


def edit_main() -> int:
    try:
        paths = Paths.discover()
        service = Service(paths)
        slug = select_project(service.projects(), title="Editar projeto", text="Selecione o projeto a editar")
        if slug is None:
            return 0
        details = service.edit_details(validate_slug(slug))
        summary = _run_zenity((
            "--info",
            "--title=Editar projeto",
            f"--text=Slug: {details['slug']}\nSessão: {details['session']}\nSomente nome, raiz e política podem ser alterados.",
        ))
        if summary is None:
            return 0
        name = _entry("Editar projeto", "Nome amigável", details["name"])
        if name is None:
            return 0
        root_action = _run_zenity((
            "--list",
            "--radiolist",
            "--title=Diretório raiz",
            f"--text=Raiz atual: {details['root']}",
            "--hide-column=2",
            "--print-column=2",
            "--column=Selecionar",
            "--column=Ação",
            "--column=Descrição",
            "--",
            "TRUE",
            "keep",
            "Manter diretório atual",
            "FALSE",
            "choose",
            "Escolher outro diretório",
        ))
        if root_action is None:
            return 0
        root = details["root"]
        if root_action == "choose":
            selected = _run_zenity((
                "--file-selection",
                "--directory",
                "--title=Novo diretório raiz",
                f"--filename={details['root']}/",
            ))
            if selected is None:
                return 0
            root = selected
        elif root_action != "keep":
            raise WorkError("ação de diretório inválida")
        policy_rows: list[str] = []
        for policy, description in (("always", "Sempre executar"), ("prompt", "Perguntar"), ("never", "Nunca executar")):
            policy_rows.extend(("TRUE" if policy == details["command_policy"] else "FALSE", policy, description))
        policy = _run_zenity((
            "--list",
            "--radiolist",
            "--title=Política de comandos",
            "--text=Escolha a política padrão",
            "--hide-column=2",
            "--print-column=2",
            "--column=Selecionar",
            "--column=Política",
            "--column=Descrição",
            "--",
            *policy_rows,
        ))
        if policy is None:
            return 0
        changes = []
        for label, old, new in (("Nome", details["name"], name), ("Raiz", details["root"], root), ("Política", details["command_policy"], policy)):
            if old != new:
                changes.append(f"{label}: {old} → {new}")
        if not changes:
            return 0
        confirmed = _run_zenity((
            "--question",
            "--title=Confirmar edição",
            f"--text=Projeto: {details['slug']}\nSessão (inalterada): {details['session']}\n\n" + "\n".join(changes),
            "--ok-label=Salvar",
            "--cancel-label=Cancelar",
        ))
        if confirmed is None:
            return 0
        service.edit(
            details["slug"],
            name=name if name != details["name"] else None,
            root=root if root != details["root"] else None,
            command_policy=policy if policy != details["command_policy"] else None,
            expected_fingerprint=details["fingerprint"],
        )
        return 0
    except (WorkError, OSError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.error("launcher de edição falhou: %s", exc)
        show_error(str(exc))
        return 2


AGENT_MODES = ("prepare", "start")
AGENT_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "XDG_RUNTIME_DIR")
AGENT_PATH = "/usr/local/bin:/usr/bin:/bin"


def _agent_executable_candidates(engine: str) -> tuple[Path, ...]:
    if engine not in ENGINES:
        raise WorkError("engine inválida")
    variable = os.environ.get(f"WORK_AGENT_{engine.upper()}")
    if variable:
        return (Path(variable).expanduser(),)
    return (Path.home() / ".local" / "bin" / engine, Path("/usr/local/bin") / engine, Path("/usr/bin") / engine)


def _resolve_agent_executable(engine: str) -> Path:
    allowed_owners = {os.geteuid(), 0}
    target_uid = os.environ.get("WORK_TARGET_UID")
    if target_uid and target_uid.isdecimal():
        allowed_owners.add(int(target_uid))
    for candidate in _agent_executable_candidates(engine):
        if not candidate.is_absolute():
            continue
        try:
            details = candidate.lstat()
        except OSError:
            continue
        if (stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)
                and details.st_uid in allowed_owners
                and not stat.S_IMODE(details.st_mode) & 0o022
                and stat.S_IXUSR & details.st_mode):
            return candidate
    raise WorkError(f"executável allowlisted não encontrado para {engine}")


def _agent_environment() -> dict[str, str]:
    identity = _root_launch_identity()
    environment = {key: value for key, value in os.environ.items() if key in AGENT_ENVIRONMENT}
    environment["PATH"] = AGENT_PATH
    if identity is not None:
        target, runtime = identity
        environment.update({"HOME": target.pw_dir, "USER": target.pw_name, "LOGNAME": target.pw_name, "XDG_RUNTIME_DIR": str(runtime)})
    else:
        environment.setdefault("HOME", str(Path.home()))
        environment.setdefault("USER", pwd.getpwuid(os.getuid()).pw_name)
        environment.setdefault("LOGNAME", environment["USER"])
    return environment


def _agent_metadata(root: Path) -> dict[str, str]:
    branch = "não-git"
    state = "não-git"
    git = root / ".git"
    if git.exists():
        branch_result = subprocess.run(["/usr/bin/git", "-C", str(root), "branch", "--show-current"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
        status_result = subprocess.run(["/usr/bin/git", "-C", str(root), "status", "--porcelain=v1"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
        branch = redact(branch_result.stdout.strip() or "detached", limit=120)
        state = "conflitos/alterações" if status_result.stdout.strip() else "limpo"
    return {"root": str(root), "worktree": str(root), "branch": branch, "state": state}


def _agent_log_handle(runtime: Runtime, agent_id: str):
    directory = runtime.root / "agent-logs"
    ensure_private_directory(directory)
    path = directory / f"{agent_id}.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)


def _agent_argv(engine: str) -> list[str]:
    return [str(_resolve_agent_executable(engine))]


def _agent_confirmation(metadata: dict[str, str], engine: str, argv: Sequence[str], mode: str, policy: str, agent_id: str) -> bool:
    display = "\n".join((
        f"Engine: {engine}",
        f"Executável: {redact(argv[0], limit=200)}",
        f"CWD: {metadata['worktree']}",
        f"Root: {metadata['root']}",
        f"Branch: {metadata['branch']}",
        f"Estado: {metadata['state']}",
        f"Modo: {mode}",
        f"Política: {policy}",
        f"Identity: {agent_id}",
    ))
    return _run_zenity(("--question", "--title=Confirmar agente", f"--text={display}", "--ok-label=Confirmar", "--cancel-label=Cancelar")) is not None


def prepare_agent(root: Path, engine: str, mode: str, path: str = ".", shared_readonly: bool = False, confirm: bool = True, state_home: Path | None = None) -> dict[str, Any] | None:
    if engine not in ENGINES or mode not in AGENT_MODES:
        raise WorkError("opção de agente inválida")
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise WorkError("root do projeto não é diretório")
    runtime = Runtime(root, state_home)
    target = runtime._safe_path(path)
    agent = runtime.register_agent(engine, worktree_id=str(root), cwd=target)
    handoff = runtime.write_handoff(agent["agent_id"], {"engine": engine, "project_id": runtime.project_id, "session_id": agent["session_id"], "cwd": str(target), "mode": mode})
    bundle = runtime.compile_context()
    claim_id = runtime.acquire_claim(str(root), str(target), agent["agent_id"], shared_readonly=shared_readonly)
    lease = None
    argv = _agent_argv(engine) if mode == "start" else None
    metadata = _agent_metadata(root)
    metadata["cwd"] = str(target)
    try:
        if mode == "start":
            lease = runtime.acquire_lease("project", str(root), agent["agent_id"], ttl=300)
            if confirm and not _agent_confirmation(metadata, engine, argv or (), mode, "shared-readonly" if shared_readonly else "write-claim", agent["agent_id"]):
                raise WorkError("início cancelado; claim e lease liberados")
            handle = _agent_log_handle(runtime, agent["agent_id"])
            try:
                process = subprocess.Popen(argv or (), cwd=str(target), env=_agent_environment(), stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            finally:
                handle.close()
            runtime.agent_state(agent["agent_id"], "started")
            return {"agent": agent, "handoff": handoff, "context": bundle, "claim_id": claim_id, "lease": lease, "argv": argv, "process": process, "metadata": metadata}
        if confirm and _run_zenity(("--question", "--title=Preparar agente", f"--text=Claim preparado para {metadata['cwd']}\nIdentity: {agent['agent_id']}\nManter preparação?", "--ok-label=Manter", "--cancel-label=Desfazer")) is None:
            raise WorkError("preparação cancelada; claim liberado")
        return {"agent": agent, "handoff": handoff, "context": bundle, "claim_id": claim_id, "lease": lease, "argv": argv, "metadata": metadata}
    except Exception:
        if lease:
            runtime.release_lease("project", str(root), agent["agent_id"], lease["fencing_token"])
        runtime.release_claim(claim_id, agent["agent_id"])
        raise


def agent_main() -> int:
    try:
        paths = Paths.discover()
        items = Service(paths).projects()
        slug = select_project(items)
        if slug is None:
            return 0
        item = next((candidate for candidate in items if candidate.get("slug") == validate_slug(slug)), None)
        if not item or item.get("error"):
            raise WorkError("projeto selecionado inválido")
        engine = _run_zenity(("--list", "--radiolist", "--title=Engine", "--text=Escolha a engine", "--hide-column=2", "--print-column=2", "--column=Usar", "--column=Engine", "TRUE", "markscode", "FALSE", "claude", "FALSE", "opencode", "FALSE", "codex"))
        if engine is None:
            return 0
        mode = _run_zenity(("--list", "--radiolist", "--title=Modo", "--text=Preparar ou iniciar", "--hide-column=2", "--print-column=2", "--column=Usar", "--column=Modo", "TRUE", "prepare", "FALSE", "start"))
        if mode is None:
            return 0
        path = _entry("Escopo do agente", "Caminho relativo ao root", ".")
        if path is None:
            return 0
        shared = _run_zenity(("--question", "--title=Política de claim", "--text=Permitir somente leitura compartilhada para caminhos sobrepostos?", "--ok-label=Shared-readonly", "--cancel-label=Claim de escrita")) is not None
        result = prepare_agent(Path(str(item["root"])), engine, mode, path, shared_readonly=shared)
        if result and mode == "prepare":
            _run_zenity(("--info", "--title=Agente preparado", f"--text=Identity: {result['agent']['agent_id']}\nClaim: {result['claim_id']}\nContexto compilado."))
        return 0
    except (WorkError, OSError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.error("launcher de agente falhou: %s", redact(str(exc)))
        show_error(redact(str(exc)))
        return 2


def handoff_main() -> int:
    return agent_main()


def context_main() -> int:
    return agent_main()


def menu_main() -> int:
    try:
        choice = _run_zenity((
            "--list", "--radiolist", "--title=Work Orchestrator", "--text=Escolha uma ação",
            "--column=Escolher", "--column=Ação",
            "TRUE", "Carregar", "FALSE", "Criar", "FALSE", "Editar", "FALSE", "Monitor",
            "FALSE", "Salvar", "FALSE", "Workspace", "FALSE", "Agent",
            "--hide-header", "--print-column=2", "--width=520", "--height=320",
        ))
        if choice is None:
            return 0
        dispatch = {
            "Carregar": load_main,
            "Criar": new_main,
            "Editar": edit_main,
            "Monitor": monitor_main,
            "Salvar": save_main,
            "Workspace": workspace_main,
            "Agent": agent_main,
        }
        action = dispatch.get(choice)
        if action is None:
            raise WorkError(f"ação desconhecida: {choice}")
        return action()
    except (WorkError, OSError, subprocess.SubprocessError, ValueError) as exc:
        show_error(str(exc))
        return 2
