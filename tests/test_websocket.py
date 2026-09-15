"""Tests for the WebSocket ConnectionManager and the /api/ws bridge.

Test Plan: WebSocket

Scenario: connect() accepts and registers a connection
  Given a ConnectionManager and a fake websocket
  When connect() is called
  Then the websocket is accepted and added to .connections

Scenario: disconnect() removes a connection
  Given a ConnectionManager with connected websockets
  When disconnect() is called for one of them
  Then only the other connections remain

Scenario: broadcast() delivers the JSON envelope to every connection
  Given a ConnectionManager with multiple connected websockets
  When broadcast() is called with an event type and data
  Then every connection receives {"type": ..., "data": ...} as JSON text

Scenario: broadcast() prunes a connection that fails to send
  Given a ConnectionManager with a healthy and a failing connection
  When broadcast() is called
  Then the failing connection is removed and the healthy one still receives
  the message

Scenario: broadcast() with a non-JSON-serializable payload raises
  Given a ConnectionManager with a connected websocket
  When broadcast() is called with a payload json.dumps cannot encode
  Then the exception propagates out of broadcast() itself (json.dumps runs
  before the per-connection try/except), which is a known silent-failure
  mode when reached through EventBus.publish (see backend/services/event_bus.py)

Scenario: create_app()'s bridge delivers a subscribed event type over /api/ws
  Given a running app and a client connected to /api/ws
  When a bridged event type (e.g. "download_progress") is published on
  app.state.event_bus
  Then the client receives the frame

Scenario: create_app()'s bridge ignores event types outside the subscribed set
  Given a running app and a client connected to /api/ws
  When an event type not in the create_app() bridge tuple (e.g.
  "scan_progress") is published, followed by a bridged event type
  Then the first (and only) frame the client receives is the bridged one
"""

import json
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from backend.api.websocket import ConnectionManager, manager
from backend.main import create_app


class FakeWebSocket:
    """Minimal stand-in for a Starlette WebSocket: async accept()/send_text()."""

    def __init__(self):
        self.accepted = False
        self.sent: list[str] = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, message: str):
        self.sent.append(message)


class RaisingWebSocket(FakeWebSocket):
    """A connection whose send_text always fails, as if the client dropped."""

    async def send_text(self, message: str):
        raise RuntimeError("connection closed")


@pytest.fixture(autouse=True)
def clean_manager():
    """`manager` (backend/api/websocket.py) is a module-level singleton
    shared by every app instance created in this process. Clear it before
    and after each test so connections registered here (directly, or via
    create_app()'s real /api/ws route) don't leak into other test modules
    that also call create_app()."""
    manager.connections.clear()
    yield
    manager.connections.clear()


class TestConnectionManagerConnectDisconnect:
    async def test_connect_accepts_and_registers(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()

        await mgr.connect(ws)

        assert ws.accepted is True
        assert mgr.connections == [ws]

    async def test_disconnect_removes_connection(self):
        mgr = ConnectionManager()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        await mgr.connect(ws1)
        await mgr.connect(ws2)

        mgr.disconnect(ws1)

        assert mgr.connections == [ws2]

    async def test_disconnect_of_unknown_connection_is_a_no_op(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()

        mgr.disconnect(ws)  # never connected

        assert mgr.connections == []


class TestConnectionManagerBroadcast:
    async def test_broadcast_sends_json_envelope_to_every_connection(self):
        mgr = ConnectionManager()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        await mgr.connect(ws1)
        await mgr.connect(ws2)

        await mgr.broadcast("download_progress", {"item_id": "1", "progress": 50})

        expected = json.dumps(
            {"type": "download_progress", "data": {"item_id": "1", "progress": 50}}
        )
        assert ws1.sent == [expected]
        assert ws2.sent == [expected]

    async def test_broadcast_prunes_failed_connection_but_delivers_to_others(self):
        mgr = ConnectionManager()
        good = FakeWebSocket()
        bad = RaisingWebSocket()
        await mgr.connect(good)
        await mgr.connect(bad)

        await mgr.broadcast("download_complete", {"item_id": "2"})

        assert bad not in mgr.connections
        assert mgr.connections == [good]
        assert len(good.sent) == 1

    async def test_broadcast_of_non_json_serializable_payload_raises(self):
        """json.dumps() (backend/api/websocket.py:ConnectionManager.broadcast)
        runs BEFORE the per-connection try/except, so a payload it can't
        encode raises straight out of broadcast() instead of being caught
        and skipped per-connection like a dead socket would be.

        This is a known silent-failure mode when broadcast() is reached via
        the create_app() bridge: EventBus.publish() (backend/services/
        event_bus.py) wraps each handler call in try/except and only logs
        the exception, so a bad payload here silently drops the event
        instead of surfacing to whoever called publish().
        """
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        await mgr.connect(ws)

        with pytest.raises(TypeError):
            await mgr.broadcast("download_progress", {"when": datetime.now()})

        assert ws.sent == []
        assert ws in mgr.connections  # broadcast blew up before pruning ran


class TestWebSocketBridgeIntegration:
    """Exercises the real /api/ws route wired up by create_app(), including
    the for-loop in backend/main.py that subscribes a fixed set of event
    types to manager.broadcast.

    These use starlette's TestClient (not httpx's ASGITransport, which
    doesn't support websockets) as a sync context manager so `client.portal`
    is available to run the (async) event_bus.publish() call on the same
    event loop the ASGI app itself is running on.
    """

    def test_bridge_delivers_a_subscribed_event_type(self):
        app = create_app(db_path=":memory:")
        with TestClient(app) as client:
            with client.websocket_connect("/api/ws") as ws:
                client.portal.call(
                    app.state.event_bus.publish,
                    "download_progress",
                    {"item_id": "1", "progress": 42},
                )
                frame = ws.receive_json()

        assert frame == {
            "type": "download_progress",
            "data": {"item_id": "1", "progress": 42},
        }

    def test_bridge_ignores_event_types_outside_the_subscribed_set(self):
        """ "scan_progress" is not one of the event types create_app() wires
        into the WebSocket bridge (see the for-loop over event_type in
        backend/main.py:create_app). Publish it first, then publish a
        bridged event: if the first frame the client receives is the
        bridged one, "scan_progress" produced no frame at all."""
        app = create_app(db_path=":memory:")
        with TestClient(app) as client:
            with client.websocket_connect("/api/ws") as ws:
                client.portal.call(
                    app.state.event_bus.publish, "scan_progress", {"progress": 1}
                )
                client.portal.call(
                    app.state.event_bus.publish,
                    "download_progress",
                    {"item_id": "2"},
                )
                frame = ws.receive_json()

        assert frame == {"type": "download_progress", "data": {"item_id": "2"}}
