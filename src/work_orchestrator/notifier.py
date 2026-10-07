import os
import subprocess
import sys
from typing import Callable

from .logging_utils import redact


NOTIFY_SEND = "/usr/bin/notify-send"


def notify(title: str, body: str, *, runner: Callable[..., object] | None = None, timeout: float = 3.0) -> bool:
    argv = [NOTIFY_SEND, "--urgency=normal", "--app-name=work-orchestrator", redact(title), redact(body)]
    call = runner or subprocess.run
    try:
        result = call(argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, env={"PATH": "/usr/bin:/bin", "DISPLAY": os.environ.get("DISPLAY", ""), "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")})
        return getattr(result, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify_or_log(title: str, body: str, *, runner: Callable[..., object] | None = None) -> bool:
    if notify(title, body, runner=runner):
        return True
    print(f"{redact(title)}: {redact(body)}", file=sys.stderr)
    return False
