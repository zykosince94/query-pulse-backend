"""QP-AC-04 (backend half): the committed openapi.json matches the code."""

from scripts.export_openapi import SPEC_PATH, render


def test_committed_openapi_is_current() -> None:
    assert SPEC_PATH.exists(), "Run: python scripts/export_openapi.py"
    assert SPEC_PATH.read_text(encoding="utf-8") == render(), (
        "openapi.json is stale. Run: python scripts/export_openapi.py"
    )
