import base64
import json
import os
import sys


def main() -> None:
    try:
        value = json.loads(base64.urlsafe_b64decode(sys.argv[1].encode("ascii")))
        if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
            raise ValueError
        os.execvp(value[0], value)
    except (IndexError, ValueError, UnicodeError, json.JSONDecodeError, OSError) as exc:
        print(f"work-orchestrator: comando não executado: {exc}", file=sys.stderr)
        raise SystemExit(127) from None


if __name__ == "__main__":
    main()
