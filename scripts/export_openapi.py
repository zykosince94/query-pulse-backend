"""Write the API contract to openapi.json, or check that the committed file is current.

    python scripts/export_openapi.py          # regenerate
    python scripts/export_openapi.py --check  # exit 1 if openapi.json is stale (CI)

The spec is generated from code, so it needs no database or real secrets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import SecretStr

from query_pulse.config import Settings
from query_pulse.main import create_app

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        session_secret=SecretStr("x" * 32),
        fernet_key=SecretStr(Fernet.generate_key().decode()),
        admin_password=SecretStr("unused-placeholder"),
    )
    spec = create_app(settings).openapi()
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        current = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
        if current != rendered:
            print("openapi.json is stale. Run: python scripts/export_openapi.py", file=sys.stderr)
            return 1
        print("openapi.json is up to date.")
        return 0
    SPEC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {SPEC_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
