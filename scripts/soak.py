"""Soak test: run the demo stack under load and watch the collector (nightly CI, Phase 7).

    uv run python scripts/soak.py --minutes 60

Needs the running stack (`make demo`). Adds both demo sources if they aren't there, then for
the whole run samples the collector container's memory every 30 s and reads its per-cycle log
lines (`event: collector_cycle`, with `duration_ms`). It fails (exit 1) if:

- memory keeps growing: the last fifth of the run averages more than --max-growth (default
  25%) above the second fifth (the first fifth is warm-up);
- cycles get slow: the 95th percentile exceeds --max-p95-ms (default 5000);
- any cycle fails.

A summary is printed, and appended to $GITHUB_STEP_SUMMARY when it's set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime

import httpx

SOURCES = [
    {
        "name": "Demo shop (PostgreSQL)",
        "engine": "postgres",
        "host": "demo-postgres",
        "port": 5432,
        "database": "shop",
        "username": "querypulse_monitor",
        "password": "monitor",
        "tls_mode": "disable",
    },
    {
        "name": "Demo shop (MongoDB)",
        "engine": "mongodb",
        "host": "mongo",
        "port": 27017,
        "database": "shop",
        "username": "",
        "password": "",
        "tls_mode": "disable",
    },
]
_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
}


def ensure_sources(url: str, password: str) -> None:
    with httpx.Client(base_url=url, timeout=30) as client:
        client.post(
            "/api/auth/login", json={"username": "admin", "password": password}
        ).raise_for_status()
        existing = client.get("/api/sources").json()
        for source in SOURCES:
            if not any(
                s["engine"] == source["engine"] and s["host"] == source["host"] for s in existing
            ):
                client.post("/api/sources", json=source).raise_for_status()


def container(service: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "ps", "-q", service], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def memory_bytes(container_id: str) -> int:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container_id],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.match(r"\s*([\d.]+)\s*([KMG]i?B|B)", out, re.I)
    if not match:
        raise RuntimeError(f"can't read memory from {out!r}")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def cycles(since: str) -> list[dict[str, object]]:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "logs",
            "--no-color",
            "--no-log-prefix",
            "--since",
            since,
            "collector",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for line in out.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            entry = json.loads(line[start:])
        except ValueError:
            continue
        if entry.get("event") == "collector_cycle":
            found.append(entry)
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--password", default="admin-demo-password")
    parser.add_argument("--max-growth", type=float, default=0.25)
    parser.add_argument("--max-p95-ms", type=float, default=5000)
    args = parser.parse_args()

    ensure_sources(args.url, args.password)
    collector = container("collector")
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = time.monotonic() + args.minutes * 60
    samples: list[int] = []
    while time.monotonic() < deadline:
        samples.append(memory_bytes(collector))
        time.sleep(30)
    samples.append(memory_bytes(collector))

    seen = cycles(started)
    durations = sorted(float(c["duration_ms"]) for c in seen)  # type: ignore[arg-type]
    failed = sum(1 for c in seen if not c.get("ok"))
    fifth = max(1, len(samples) // 5)
    baseline = statistics.mean(samples[fifth : 2 * fifth] or samples)
    final = statistics.mean(samples[-fifth:])
    growth = (final - baseline) / baseline if baseline else 0.0
    p95 = durations[int(0.95 * (len(durations) - 1))] if durations else 0.0
    problems = []
    if growth > args.max_growth:
        problems.append(f"memory grew {growth:.0%} (limit {args.max_growth:.0%})")
    if p95 > args.max_p95_ms:
        problems.append(f"cycle p95 {p95:.0f} ms (limit {args.max_p95_ms:.0f} ms)")
    if failed:
        problems.append(f"{failed} failed cycle(s)")
    if not durations:
        problems.append("no collector cycles were logged")

    mib = 1024**2
    p50 = statistics.median(durations) if durations else 0.0
    slowest = max(durations, default=0.0)
    lines = [
        f"## Soak test ({args.minutes:g} min)",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Collector memory, after warm-up | {baseline / mib:.1f} MiB |",
        f"| Collector memory, end | {final / mib:.1f} MiB ({growth:+.0%}) |",
        f"| Collector memory, peak | {max(samples) / mib:.1f} MiB |",
        f"| Cycles | {len(durations)} ({failed} failed) |",
        f"| Cycle time p50 / p95 / max | {p50:.0f} / {p95:.0f} / {slowest:.0f} ms |",
        "",
        "**Result:** " + ("passed" if not problems else "failed: " + "; ".join(problems)),
    ]
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
