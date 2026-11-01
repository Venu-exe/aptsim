#!/usr/bin/env python3
"""Replay aptsim event streams into a real SIEM or collector.

Reads a session's ``events.jsonl`` (or a file you pass) and either prints
NDJSON or posts each event to an HTTP endpoint. Uses only the standard
library. This is test tooling: it ships logs, nothing else.

Examples
--------
Print the latest run as NDJSON:

    python3 scripts/replay_events.py --dry-run

Splunk HTTP Event Collector:

    python3 scripts/replay_events.py --format splunk \
        --url https://splunk.example:8088 --token "$HEC_TOKEN" \
        --index lab --sourcetype aptsim:json

Elastic/OpenSearch document API:

    python3 scripts/replay_events.py --format elastic \
        --url https://es.example:9200 --token "$ES_TOKEN" \
        --index aptsim-events

Generic collector that accepts one JSON object per request:

    python3 scripts/replay_events.py --format raw --url http://collector:9000/ingest
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAB = ROOT / "lab"


def latest_events(lab: Path) -> Path:
    sessions = sorted(
        (path for path in (lab / "sessions").glob("*/events.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not sessions:
        raise SystemExit(f"no events.jsonl under {lab}/sessions — run the lab first")
    for path in sessions:
        if '"action":"run.completed"' in path.read_text(encoding="utf-8", errors="ignore"):
            return path
    return sessions[0]


def load(path: Path) -> list[dict]:
    events: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"skip line {number}: {exc}", file=sys.stderr)
    return events


def payload(event: dict, fmt: str, index: str, sourcetype: str) -> tuple[bytes, str, str]:
    if fmt == "splunk":
        body = {
            "event": event,
            "time": event.get("ts"),
            "index": index,
            "sourcetype": sourcetype,
        }
        return json.dumps(body).encode(), "application/json", "/services/collector/event"
    if fmt == "elastic":
        body = dict(event)
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            body["@timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"
        return json.dumps(body).encode(), "application/json", f"/{index}/_doc"
    return json.dumps(event).encode(), "application/json", ""


def send(url: str, body: bytes, content_type: str, token: str | None, fmt: str) -> None:
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    if token:
        if fmt == "splunk":
            request.add_header("Authorization", f"Splunk {token}")
        else:
            request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay aptsim events into a SIEM")
    parser.add_argument("--events", type=Path, help="path to events.jsonl (default: latest session)")
    parser.add_argument("--lab", type=Path, default=DEFAULT_LAB, help="lab root")
    parser.add_argument("--format", choices=("raw", "splunk", "elastic"), default="raw")
    parser.add_argument("--url", help="base URL of the collector")
    parser.add_argument("--token", help="HEC or API token (optional)")
    parser.add_argument("--index", default="aptsim-events", help="target index")
    parser.add_argument("--sourcetype", default="aptsim:json", help="Splunk sourcetype")
    parser.add_argument("--rate", type=float, default=0.0, help="seconds to sleep between events")
    parser.add_argument("--limit", type=int, default=0, help="send at most N events")
    parser.add_argument("--dry-run", action="store_true", help="print NDJSON instead of sending")
    args = parser.parse_args()

    path = args.events or latest_events(args.lab)
    events = load(path)
    if args.limit:
        events = events[: args.limit]
    print(f"source : {path}", file=sys.stderr)
    print(f"events : {len(events)}", file=sys.stderr)

    if args.dry_run or not args.url:
        if not args.dry_run and not args.url:
            print("no --url given; printing NDJSON", file=sys.stderr)
        for event in events:
            print(json.dumps(event, separators=(",", ":")))
        return 0

    if not args.url.startswith(("http://", "https://")):
        raise SystemExit("--url must start with http:// or https://")

    sent = 0
    for event in events:
        body, content_type, suffix = payload(event, args.format, args.index, args.sourcetype)
        target = args.url.rstrip("/") + suffix
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                send(target, body, content_type, args.token, args.format)
                last_error = None
                break
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                last_error = exc
                time.sleep(0.5 * attempt)
        if last_error is not None:
            print(f"failed at seq={event.get('seq')}: {last_error}", file=sys.stderr)
            print(f"sent {sent}/{len(events)} before failure", file=sys.stderr)
            return 1
        sent += 1
        if args.rate:
            time.sleep(args.rate)

    print(f"sent : {sent}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
