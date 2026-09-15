"""Regression for #31: every event type published anywhere in the backend
must have a WebSocket bridge subscriber, or EventBus.publish() silently
drops it and open UI views never learn about the change.

`scan_progress` / `scan_complete` are intentionally excluded — per the
comment in backend/api/library.py's start_scan, the frontend polls
GET /api/library/scan-fuzzy/{job_id} for those instead of listening on the
WebSocket.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"

# Matches `<something>.publish(` followed by a string-literal first arg,
# e.g. `self.event_bus.publish(\n    "download_progress",`. Calls that
# forward a variable (like the scan-job progress-tracking bus's
# `await self._inner.publish(event_type, data)`) are intentionally not
# matched — they aren't the origin of the literal event name.
PUBLISH_CALL_RE = re.compile(r"\.publish\(\s*[\"'](\w+)[\"']")

POLLED_NOT_BRIDGED = {"scan_progress", "scan_complete"}


def _published_event_types() -> set[str]:
    names: set[str] = set()
    for path in BACKEND_DIR.rglob("*.py"):
        text = path.read_text()
        names.update(PUBLISH_CALL_RE.findall(text))
    return names - POLLED_NOT_BRIDGED


class TestEventBridge:
    def test_every_published_event_type_is_bridged_to_websocket(self):
        published = _published_event_types()
        # Sanity check the grep itself actually found something, so a
        # regex/path typo can't silently pass this test.
        assert "download_complete" in published
        assert "album_status_changed" in published

        app = create_app(db_path=":memory:")
        bridged = {
            event_type
            for event_type, handlers in app.state.event_bus._subscribers.items()
            if handlers
        }

        missing = published - bridged
        assert not missing, (
            f"published but not subscribed on the WebSocket bridge: {missing}"
        )
