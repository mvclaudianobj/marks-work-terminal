import json
import re
from pathlib import Path

from .config import config_path, parse_project
from .errors import WorkError
from .model import POLICIES, Project
from .paths import Paths
from .store import read_private_bytes, write_bytes_atomic


FIELDS = ("name", "root", "command_policy")
PROJECT_HEADER = re.compile(r"^[ \t]*\[[ \t]*project[ \t]*\][ \t]*(?:#.*)?(?:\r?\n)?$")
ANY_HEADER = re.compile(r"^[ \t]*\[")
ASSIGNMENT = re.compile(r"^([ \t]*)(name|root|command_policy)([ \t]*=[ \t]*)(.*?)(\r?\n)?$")


def _suffix(value: str, field: str) -> str:
    if value.startswith(('"""', "'''")) or not value.startswith(("\"", "'")):
        raise WorkError(f"sintaxe ambígua em project.{field}; use string TOML simples em uma linha")
    quote = value[0]
    escaped = False
    end = None
    for index, char in enumerate(value[1:], 1):
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            end = index + 1
            break
        escaped = False
    if end is None:
        raise WorkError(f"sintaxe ambígua em project.{field}")
    suffix = value[end:]
    if suffix.strip() and not suffix.lstrip().startswith("#"):
        raise WorkError(f"sintaxe ambígua em project.{field}")
    return suffix


def patch_project(
    paths: Paths,
    project: Project,
    payload: bytes,
    fingerprint: str,
    *,
    name: str | None = None,
    root: str | None = None,
    command_policy: str | None = None,
) -> tuple[Project, str]:
    replacements = {key: value for key, value in (("name", name), ("root", root), ("command_policy", command_policy)) if value is not None}
    if not replacements:
        raise WorkError("informe ao menos um campo para edição")
    if command_policy is not None and command_policy not in POLICIES:
        raise WorkError("project.command_policy deve ser always, prompt ou never")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkError("configuração não é UTF-8") from exc
    lines = text.splitlines(keepends=True)
    headers = [index for index, line in enumerate(lines) if PROJECT_HEADER.fullmatch(line)]
    if len(headers) != 1:
        raise WorkError("tabela [project] ausente ou ambígua")
    start = headers[0] + 1
    end = next((index for index in range(start, len(lines)) if ANY_HEADER.match(lines[index])), len(lines))
    positions: dict[str, int] = {}
    for index in range(start, end):
        match = ASSIGNMENT.fullmatch(lines[index])
        if match is None:
            continue
        field = match.group(2)
        if field in positions:
            raise WorkError(f"atribuição ambígua para project.{field}")
        positions[field] = index
    newline = "\r\n" if "\r\n" in text and "\n" in text else "\n"
    missing: list[str] = []
    for field, value in replacements.items():
        rendered = json.dumps(value, ensure_ascii=False)
        if field not in positions:
            missing.append(f"{field} = {rendered}{newline}")
            continue
        index = positions[field]
        match = ASSIGNMENT.fullmatch(lines[index])
        if match is None:
            raise WorkError(f"atribuição ambígua para project.{field}")
        ending = match.group(5) or ""
        suffix = _suffix(match.group(4), field)
        lines[index] = f"{match.group(1)}{field}{match.group(3)}{rendered}{suffix}{ending}"
    if missing:
        lines[end:end] = missing
    candidate = "".join(lines).encode("utf-8")
    validated = parse_project(paths, project.slug, candidate)
    if validated.slug != project.slug or validated.session != project.session:
        raise WorkError("edição alteraria slug ou sessão")
    path = config_path(paths, project.slug)
    write_bytes_atomic(path, candidate, expected_fingerprint=fingerprint)
    _, new_fingerprint = read_private_bytes(path)
    return validated, new_fingerprint
