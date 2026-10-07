import re


REDACTED = "[REDACTED]"
_INPUT_LIMIT = 8192
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_URL_CREDENTIALS = re.compile(
    r"(?is)([a-z][a-z0-9+.-]{0,31}://)([^\s:/@]{1,512})(?::([^\s/@]{0,2048}))?@(?=[a-z0-9._~%-])"
)
_AUTH = re.compile(
    r"(?is)(?<![\w-])(authorization|proxy-authorization|bearer|basic)(?![\w-])"
    r"(\s*(?::|=|\s+)\s*)(?:(basic|bearer)\s+)?([^\s,;]{1,8192})"
)
_SECRET = re.compile(
    r"(?is)(?<![\w-])"
    r"(client[\s_-]?secret|access[\s_-]?token|private[\s_-]?key|token|secret|password|passwd|passphrase|"
    r"(?:x-)?api[\s_-]?(?:key|token)|apikey)"
    r"(?![\w-])(\s*(?::|=|\s+)\s*)"
    r"(?:\"([^\"\r\n]{0,8192})\"|'([^'\r\n]{0,8192})'|([^\s,;]{1,8192}))"
)
_PRIVATE_KEY = re.compile(r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
_SUSPICIOUS_ASSIGNMENT = re.compile(r"(?i)(?:credential|auth|key|login|session)[\s_-]?[a-z0-9-]*\s*[:=]")
_OPAQUE = re.compile(r"(?i)(?:[a-z0-9+/]{32,}={0,2}|[a-f0-9]{40,})")


def redact(text: str, *, limit: int = 240) -> str:
    value = text[:_INPUT_LIMIT]
    value = _PRIVATE_KEY.sub(REDACTED, value)
    value = _URL_CREDENTIALS.sub(lambda match: f"{match.group(1)}{REDACTED}@", value)
    value = _AUTH.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3) + ' ' if match.group(3) else ''}{REDACTED}",
        value,
    )

    def replace_secret(match: re.Match[str]) -> str:
        prefix = match.group(1) + match.group(2)
        if match.group(3) is not None:
            return f'{prefix}"{REDACTED}"'
        if match.group(4) is not None:
            return f"{prefix}'{REDACTED}'"
        return f"{prefix}{REDACTED}"

    value = _SECRET.sub(replace_secret, value)
    value = _CONTROL.sub(" ", value)
    return " ".join(value.split())[:limit]


def sanitize_pane_title(text: str, *, limit: int = 120) -> str:
    if not isinstance(text, str) or not text or len(text) > limit or _CONTROL.search(text):
        return ""
    clean = redact(text, limit=limit)
    if clean != " ".join(text.split()) or REDACTED in clean:
        return ""
    if _SUSPICIOUS_ASSIGNMENT.search(clean) or _OPAQUE.search(clean):
        return ""
    return clean
