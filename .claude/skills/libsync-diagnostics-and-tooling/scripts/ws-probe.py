#!/usr/bin/env python3
"""ws-probe.py — connect to the libsync WebSocket and print typed events.

Usage:
    poetry run python .claude/skills/libsync-diagnostics-and-tooling/scripts/ws-probe.py [URL] [--idle-timeout SECONDS]

    URL defaults to ws://localhost:8080/api/ws.
    --idle-timeout N exits 0 after N seconds without an event (useful in
    scripts); default is to run until Ctrl-C.

Requires the `websockets` package — a direct dependency of this project
(pyproject.toml), so `poetry run` always has it.

Read-only: the /api/ws endpoint discards all inbound client messages, so
this probe cannot mutate server state. Event payloads carry no credentials.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

# The ONLY event types create_app() bridges from the internal EventBus to
# the WebSocket (backend/main.py, create_app). Anything else arriving here
# means the bridge list changed — update this script and SKILL.md.
BRIDGED_TYPES = {
    "download_progress",
    "download_complete",
    "download_failed",
    "sync_started",
    "sync_complete",
    "library_updated",
    "token_expired",  # bridged but no backend code publishes it (dead wire)
}


def summarize(data: dict) -> str:
    parts = []
    for key in ("item_id", "title", "artist", "source", "tracks_done",
                "track_count", "bytes_done", "bytes_total", "speed",
                "current_track", "status", "error"):
        if key in data:
            val = str(data[key])
            if len(val) > 40:
                val = val[:37] + "..."
            parts.append(f"{key}={val}")
    if not parts:
        raw = json.dumps(data)
        return raw[:120] + ("..." if len(raw) > 120 else "")
    return " ".join(parts)


async def probe(url: str, idle_timeout: float | None) -> int:
    try:
        import websockets
    except ImportError:
        print("ERROR: the 'websockets' package is not importable.", file=sys.stderr)
        print("Run via: poetry run python scripts/ws-probe.py", file=sys.stderr)
        return 2

    try:
        async with websockets.connect(url) as ws:
            print(f"connected to {url} — waiting for events (Ctrl-C to stop)")
            while True:
                try:
                    if idle_timeout:
                        raw = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
                    else:
                        raw = await ws.recv()
                except asyncio.TimeoutError:
                    print(f"no events for {idle_timeout}s — exiting")
                    return 0
                ts = datetime.now().strftime("%H:%M:%S")
                try:
                    msg = json.loads(raw)
                    etype = msg.get("type", "<no type>")
                    data = msg.get("data", {})
                except (json.JSONDecodeError, AttributeError):
                    print(f"{ts}  MALFORMED (not {{type,data}} JSON): {raw[:120]!r}")
                    continue
                flag = "" if etype in BRIDGED_TYPES else "  <-- NOT in the bridged set!"
                print(f"{ts}  {etype:<20} {summarize(data)}{flag}")
    except (ConnectionRefusedError, OSError) as e:
        print(f"ERROR: cannot connect to {url}: {e}", file=sys.stderr)
        print("Is the backend running? Start it with `make dev-backend`.", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="ws://localhost:8080/api/ws")
    parser.add_argument("--idle-timeout", type=float, default=None)
    args = parser.parse_args()
    try:
        return asyncio.run(probe(args.url, args.idle_timeout)) or 0
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
