# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import sys
import threading
import time
import types

from core.intel.source_registry import source_registry
from core.vessels.aisstream import AISStreamClient


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_repeated_connection_failure_reports_into_source_registry(monkeypatch):
    # Real bug this fixes: AIS never reported into the same health system
    # every other source uses, so a fully dead feed (connects, subscribes,
    # zero data every cycle -- the exact upstream AISstream failure mode)
    # went unnoticed for 7+ hours. This is the "connects but never gets
    # data" shape: recv() always raises (a timeout), never a connect()
    # failure, matching what was observed live.
    class _FakeWs:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def send(self, *a, **kw):
            pass
        def recv(self, timeout=None):
            raise TimeoutError("timed out in 60.0s")

    # websockets isn't installed in every dev environment this test suite
    # runs in (it's a server-only runtime dependency) -- inject a fake
    # module tree so `import websockets.sync.client as ws_sync` inside
    # _run() resolves without needing the real package.
    fake_client_module = types.ModuleType("websockets.sync.client")
    fake_client_module.connect = lambda *a, **kw: _FakeWs()
    fake_sync_module = types.ModuleType("websockets.sync")
    fake_sync_module.client = fake_client_module
    fake_websockets_module = types.ModuleType("websockets")
    fake_websockets_module.sync = fake_sync_module
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets_module)
    monkeypatch.setitem(sys.modules, "websockets.sync", fake_sync_module)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", fake_client_module)
    monkeypatch.setattr("core.vessels.aisstream.time.sleep", lambda s: None)

    client = AISStreamClient("dummy-key", label="Test Region")
    thread = threading.Thread(target=client._run, daemon=True)
    thread.start()
    try:
        source = None
        assert _wait_for(lambda: (
            source_registry.get("aisstream_test_region") is not None
            and source_registry.get("aisstream_test_region").consecutive_errors >= 2
        ))
        source = source_registry.get("aisstream_test_region")
        assert source.source_type == "ais"
        assert source.status in ("degraded", "offline")
        assert "timed out" in (source.last_error or "")
    finally:
        client.stop()


class _Registry:
    def __init__(self):
        self.rows = []

    def upsert(self, mmsi, **kwargs):
        self.rows.append((mmsi, kwargs))


def _position_message(mmsi="247123456", lat=35.1, lon=15.2):
    return {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": mmsi, "ShipName": "TEST"},
        "Message": {"PositionReport": {
            "Latitude": lat, "Longitude": lon, "Sog": 7.2, "Cog": 91.0,
            "TrueHeading": 90, "NavigationalStatus": 0,
        }},
    }


def test_aisstream_position_report_emits_provider_observation():
    seen = []
    client = AISStreamClient("key", on_observation=seen.append)
    client._handle(_position_message(), _Registry())
    assert len(seen) == 1
    assert seen[0].provider == "aisstream"
    assert seen[0].upstream_source == "aisstream"
    assert seen[0].mmsi == "247123456"


def test_fused_mode_callback_can_disable_direct_legacy_writes(monkeypatch):
    from core.vessels import ais_bus

    seen = []
    published = []
    registry = _Registry()
    monkeypatch.setattr(ais_bus, "publish", published.append)

    client = AISStreamClient(
        "key", on_observation=seen.append, publish_legacy=False,
    )
    client._handle(_position_message(), registry)

    assert len(seen) == 1
    assert registry.rows == []
    assert published == []


def test_aisstream_exposes_provider_health_contract():
    client = AISStreamClient("key")
    health = client.health()
    assert health.provider == "aisstream"
    assert health.connected is False
    assert health.messages_received == 0


def test_aisstream_stop_emits_provider_health_callback():
    seen = []
    client = AISStreamClient("key", on_health=seen.append)
    client.stop()
    assert seen
    assert seen[-1].provider == "aisstream"
    assert seen[-1].connected is False


def test_ngo_socket_does_not_override_primary_provider_health(monkeypatch):
    from core.vessels import aisstream

    created = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            created.append(self)
        def start(self):
            pass

    health = lambda _state: None
    old_client, old_ngo_client = aisstream._client, aisstream._ngo_client
    monkeypatch.setattr(aisstream, "AISStreamClient", FakeClient)
    try:
        aisstream.start(
            "primary-key", ngo_api_key="ngo-key",
            on_observation=lambda _obs: None, on_health=health,
        )
        assert len(created) == 2
        assert created[0].kwargs["on_health"] is health
        assert created[1].kwargs["on_health"] is None
    finally:
        aisstream._client = old_client
        aisstream._ngo_client = old_ngo_client


def test_protocol_ping_watchdog_is_disabled_but_application_liveness_remains(monkeypatch):
    captured = {}
    client = AISStreamClient("dummy-key", label="Ping Contract")

    class _FakeWs:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def send(self, *_args, **_kwargs):
            pass
        def recv(self, timeout=None):
            raise AssertionError("stop flag should prevent recv in this contract test")

    def connect(*args, **kwargs):
        captured.update(kwargs)
        client._stop.set()
        return _FakeWs()

    fake_client_module = types.ModuleType("websockets.sync.client")
    fake_client_module.connect = connect
    fake_sync_module = types.ModuleType("websockets.sync")
    fake_sync_module.client = fake_client_module
    fake_websockets_module = types.ModuleType("websockets")
    fake_websockets_module.sync = fake_sync_module
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets_module)
    monkeypatch.setitem(sys.modules, "websockets.sync", fake_sync_module)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", fake_client_module)

    client._run()
    client.stop()

    assert captured["open_timeout"] == 15
    assert "ping_interval" in captured
    assert captured["ping_interval"] is None

def test_dedicated_ngo_stream_updates_registry_without_duplicate_bus_publish(monkeypatch):
    from core.vessels import ais_bus

    registry = _Registry()
    published = []
    monkeypatch.setattr(ais_bus, "publish", published.append)

    client = AISStreamClient(
        "key",
        publish_legacy=False,
        update_registry=True,
    )
    client._handle(_position_message(mmsi="258479000"), registry)

    assert len(registry.rows) == 1
    assert registry.rows[0][0] == "258479000"
    assert published == []


def test_ngo_stream_reuses_primary_key_when_override_is_unset(monkeypatch):
    from core.vessels import aisstream

    created = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            created.append(self)

        def start(self):
            pass

    old_client, old_ngo_client = aisstream._client, aisstream._ngo_client
    monkeypatch.setattr(aisstream, "AISStreamClient", FakeClient)
    try:
        aisstream.start("primary-key", ngo_api_key="")
        assert len(created) == 2
        assert created[0].args[0] == "primary-key"
        assert created[1].args[0] == "primary-key"
        assert created[1].kwargs["mmsi_filter"]
        assert created[1].kwargs["publish_legacy"] is False
        assert created[1].kwargs["update_registry"] is True
    finally:
        aisstream._client = old_client
        aisstream._ngo_client = old_ngo_client
