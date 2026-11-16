"""Save the demo's evidence bundles as the AI evaluation set (evals/bundles/).

    uv run python scripts/save_eval_bundles.py --url http://127.0.0.1:8000

Needs the running demo stack with both demo sources added and their findings raised. Takes
the bundle of the open finding for each planted problem (P1-P4, M1-M3) exactly as the API
would send it to a provider, so it holds no literals or credentials, and stores it with the
terms a right answer names.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "bundles"

# Planted problem -> (rule, the terms a right explanation contains; any synonym counts).
CASES: dict[str, tuple[str, list[list[str]]]] = {
    "P1": (
        "PG-SEQSCAN-HOT",
        [
            ["index"],
            ["customer_id"],
            ["sequential scan", "seq scan", "full scan", "every row", "all rows"],
        ],
    ),
    "P2": ("PG-SORT-NO-INDEX", [["index"], ["created_at"], ["sort"]]),
    "P3": (
        "PG-LEADING-WILDCARD",
        [["wildcard", "leading %", "like"], ["trigram", "pg_trgm", "gin"]],
    ),
    "P4": (
        "PG-UNUSED-INDEX",
        [["unused", "never used", "zero scans", "no scans", "0 scans"], ["drop index"]],
    ),
    "M1": (
        "MDB-COLLSCAN",
        [["index"], ["customerid"], ["collection scan", "collscan", "every document"]],
    ),
    "M2": ("MDB-BLOCKING-SORT", [["sort"], ["placedat"], ["index"]]),
    "M3": ("MDB-EXAMINED-RATIO", [["regex"], ["anchor", "prefix", "^"]]),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--password", default="admin-demo-password")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=30) as client:
        login = client.post(
            "/api/auth/login", json={"username": "admin", "password": args.password}
        )
        login.raise_for_status()
        findings = client.get("/api/findings", params={"status": "open"}).json()["findings"]
        OUT.mkdir(parents=True, exist_ok=True)
        for problem, (rule, terms) in CASES.items():
            finding = next((f for f in findings if f["rule_id"] == rule), None)
            if finding is None:
                sys.exit(
                    f"No open {rule} finding for {problem}: is the demo running with both sources?"
                )
            bundle = client.get(f"/api/findings/{finding['id']}/ai/bundle").json()
            case = {
                "problem": problem,
                "rule_id": rule,
                "expected_terms": terms,
                "bundle": bundle["document"],
            }
            path = OUT / f"{problem}.json"
            path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            print(f"saved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
