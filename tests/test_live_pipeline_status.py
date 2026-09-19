from __future__ import annotations

from fastapi.testclient import TestClient


def test_live_pipeline_endpoint_is_public_and_uses_unified_source_families(monkeypatch):
    from core.acquisition import status as acquisition_status
    from core.api.main import app

    acquisition_status._reset_acquisition_status_for_tests()
    monkeypatch.setattr(acquisition_status, "ensure_default_acquisition_status", lambda: None)
    acquisition_status.register_acquisition_status(
        "ais", "AIS", lambda: {"state": "live", "mode": "legacy", "secret": "no"}
    )
    acquisition_status.register_acquisition_status(
        "first_party", "First-party feeds", lambda: {"state": "live"}
    )
    acquisition_status.register_acquisition_status(
        "public_feed", "Public feeds", lambda: {"state": "degraded"}
    )
    acquisition_status.register_acquisition_status(
        "partner", "Partner inputs", lambda: {"state": "offline"}
    )
    acquisition_status.register_acquisition_status(
        "radio",
        "Radio",
        lambda: {
            "state": "live",
            "configured": 1,
            "structured_enabled": True,
            "receivers": [
                {
                    "receiver_id": "med_dsc",
                    "station_label": "Mediterranean DSC",
                    "provider": "kiwisdr",
                    "state": "connected",
                    "channel_kind": "dsc",
                    "frequency_hz": 2_187_500,
                    "mode": "usb",
                    "last_observation_at": "2026-09-07T00:00:00+00:00",
                    "observations_received": 4,
                    "frontend_url": "https://secret.example.org",
                    "source_terms": "private terms",
                    "physical_lineage": "private-lineage",
                }
            ],
        },
    )

    response = TestClient(app).get("/api/v1/live/pipeline")
    assert response.status_code == 200
    payload = response.json()
    families = {source["family"] for source in payload["sources"]}
    assert families == {"ais", "first_party", "public_feed", "partner", "radio"}
    assert next(source for source in payload["sources"] if source["family"] == "ais")["mode"] == "legacy"
    radio = next(source for source in payload["sources"] if source["family"] == "radio")
    assert radio["receivers"][0]["station_label"] == "Mediterranean DSC"
    assert radio["structured_enabled"] is True
    serialized = str(payload).lower()
    for forbidden in (
        "frontend_url", "source_terms", "session_id", "physical_lineage",
        "private-lineage", "secret.example.org", "private terms",
        "mmsi", "imo", "callsign", "transcript", "raw_payload",
    ):
        assert forbidden not in serialized


def test_acquisition_status_fails_closed_on_bad_provider_state(monkeypatch):
    from core.acquisition import status as acquisition_status

    acquisition_status._reset_acquisition_status_for_tests()
    acquisition_status.register_acquisition_status(
        "radio", "Radio", lambda: {"state": "invented", "frontend_url": "https://secret"}
    )
    sources = acquisition_status.acquisition_status_sources()
    assert sources == [{"family": "radio", "label": "Radio", "state": "degraded"}]


def test_radio_acquisition_status_reports_structured_capability_when_remote_is_disabled(monkeypatch):
    from core.config import config
    from core.radio import runtime as radio_runtime
    from core.radio.bridge import radio_acquisition_status

    monkeypatch.setattr(config, "STRUCTURED_RADIO_ENABLED", True)
    monkeypatch.setattr(
        radio_runtime,
        "get_remote_radio_status",
        lambda include_receivers=False: {
            "enabled": False, "configured": 0, "started": 0, "failed": 0, "receivers": [],
        },
    )
    status = radio_acquisition_status()
    assert status["state"] == "disabled"
    assert status["structured_enabled"] is True


def test_radio_status_is_live_when_any_receiver_is_connected(monkeypatch):
    from core.config import config
    from core.radio import runtime as radio_runtime
    from core.radio.bridge import radio_acquisition_status

    monkeypatch.setattr(config, "STRUCTURED_RADIO_ENABLED", True)
    monkeypatch.setattr(radio_runtime, "get_remote_radio_status", lambda include_receivers=False: {
        "enabled": True, "configured": 3, "started": 1, "failed": 2,
        "receivers": [
            {"receiver_id": "one", "state": "connected"},
            {"receiver_id": "two", "state": "disconnected"},
        ],
    })
    status = radio_acquisition_status()
    assert status["state"] == "live"
    assert status["failed"] == 2


def test_public_receiver_catalog_is_bounded_and_hides_endpoints():
    from core.api.main import app
    payload = TestClient(app).get("/api/v1/live/receivers/catalog?zone=central_med&limit=8").json()
    assert payload["zone"] == "central_med"
    assert 1 <= len(payload["receivers"]) <= 8
    assert any(row["network_family"] == "openwebrx" for row in payload["receivers"])
    serialized = str(payload).lower()
    assert "frontend_url" not in serialized
    assert "endpoint" not in serialized
    assert "physical_lineage" not in serialized


def test_acquisition_pipeline_sanitizes_bounded_radio_channel_coverage():
    from core.acquisition import status as acquisition_status

    acquisition_status._reset_acquisition_status_for_tests()
    acquisition_status.register_acquisition_status(
        "radio", "Radio", lambda: {
            "state": "degraded",
            "channels": [
                {
                    "channel_kind": "dsc", "frequency_hz": 2_187_500, "mode": "usb",
                    "desired": 3, "active": 2, "standby": 4, "cooldown": 1,
                    "failovers": 5, "physical_lineage": "secret-lineage",
                    "frontend_url": "https://secret.example.org",
                }
            ] * 20,
        },
    )

    radio = acquisition_status.acquisition_status_sources()[0]
    assert len(radio["channels"]) == 8
    assert radio["channels"][0] == {
        "channel_kind": "dsc", "frequency_hz": 2_187_500, "mode": "usb",
        "desired": 3, "active": 2, "standby": 4, "cooldown": 1, "failovers": 5,
    }
    assert "secret" not in str(radio).lower()


def test_radio_acquisition_status_carries_runtime_channel_coverage(monkeypatch):
    from core.config import config
    from core.radio import runtime as radio_runtime
    from core.radio.bridge import radio_acquisition_status

    monkeypatch.setattr(config, "STRUCTURED_RADIO_ENABLED", True)
    channels = [{
        "channel_kind": "monitor", "frequency_hz": 2_187_500, "mode": "usb",
        "desired": 3, "active": 3, "standby": 5, "cooldown": 0, "failovers": 2,
    }]
    monkeypatch.setattr(radio_runtime, "get_remote_radio_status", lambda include_receivers=False: {
        "enabled": True, "configured": 8, "started": 3, "failed": 0,
        "receivers": [{"receiver_id": "one", "state": "connected"}],
        "channels": channels,
    })

    status = radio_acquisition_status()

    assert status["channels"] == channels


def test_radio_acquisition_status_is_degraded_when_managed_channel_is_under_replicated(monkeypatch):
    from core.config import config
    from core.radio import runtime as radio_runtime
    from core.radio.bridge import radio_acquisition_status

    monkeypatch.setattr(config, "STRUCTURED_RADIO_ENABLED", True)
    monkeypatch.setattr(radio_runtime, "get_remote_radio_status", lambda include_receivers=False: {
        "enabled": True, "configured": 6, "started": 2, "failed": 0,
        "receivers": [
            {"receiver_id": "one", "state": "connected"},
            {"receiver_id": "two", "state": "connected"},
        ],
        "channels": [{
            "channel_kind": "monitor", "frequency_hz": 2_187_500, "mode": "usb",
            "desired": 3, "active": 2, "standby": 3, "cooldown": 1, "failovers": 1,
        }],
    })

    status = radio_acquisition_status()

    assert status["state"] == "degraded"


def test_acquisition_pipeline_exposes_only_safe_decoder_counters():
    from core.acquisition import status as acquisition_status

    acquisition_status._reset_acquisition_status_for_tests()
    acquisition_status.register_acquisition_status(
        "radio",
        "Radio",
        lambda: {
            "state": "live",
            "structured_enabled": True,
            "structured_state": "degraded",
            "decoder": {
                "enabled": True,
                "decoders": 2,
                "frames": 120,
                "decoded": 1,
                "invalid": 7,
                "dropped": 4,
                "errors": 3,
                "queued": 2,
                "worker_alive": True,
                "command": "/secret/decoder",
                "last_payload": "raw audio",
            },
        },
    )

    radio = acquisition_status.acquisition_status_sources()[0]
    assert radio["structured_state"] == "degraded"
    assert radio["decoder"] == {
        "enabled": True,
        "decoders": 2,
        "frames": 120,
        "decoded": 1,
        "invalid": 7,
        "dropped": 4,
        "errors": 3,
        "queued": 2,
        "worker_alive": True,
    }
    serialized = str(radio).lower()
    assert "secret" not in serialized
    assert "raw audio" not in serialized
