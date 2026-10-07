import os
import subprocess
import sys
from pathlib import Path

from .errors import WorkError
from .logging_utils import append_log
from .paths import Paths, validate_slug


def _checkout_work() -> Path:
    executable = Path(__file__).resolve().parents[2] / "bin" / "work"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WorkError(f"executável work não encontrado no checkout: {executable}")
    return executable


def detach_main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(values) != 1:
            raise WorkError("hook detach exige exatamente um slug")
        paths = Paths.discover()
        paths.ensure()
        slug = validate_slug(values[0])
        subprocess.run([str(_checkout_work()), "save", slug], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
        return 0
    except (WorkError, OSError, ValueError, subprocess.SubprocessError) as exc:
        try:
            paths = locals().get("paths")
            if isinstance(paths, Paths):
                append_log(paths.state / "logs" / "hook-detach.log", f"falha no detach: {type(exc).__name__}: {exc}")
        except OSError:
            pass
        return 2
