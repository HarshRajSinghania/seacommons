from __future__ import annotations

from fastapi.testclient import TestClient


def test_public_status_is_aggregate_and_public(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "INTERNAL_PROXY_SECRET", "internal-secret")
    response = TestClient(app).get("/api/v1/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "SeaCommons"
    assert payload["status"] == "operational"
    assert set(payload["live"]) >= {"total", "operational", "humanitarian", "maritime"}
    assert set(payload["pipeline"]) >= {
        "raw_observations",
        "parsed_events",
        "analysis_outputs",
        "maritime_episodes",
        "investigation_hypotheses",
    }
    assert set(payload["sensor_activity"]) >= {
        "ais_fixes",
        "radio_bursts",
        "radio_events",
        "satellite_observations",
    }
    serialized = response.text.lower()
    assert "payload_ref" not in serialized
    assert "receiver_id" not in serialized


def test_api_root_returns_status(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "INTERNAL_PROXY_SECRET", "internal-secret")
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "SeaCommons"


def test_operator_dashboard_requires_gateway(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    client = TestClient(app)
    assert client.get("/api/v1/operator/ingestion/dashboard").status_code == 401
    response = client.get(
        "/api/v1/operator/ingestion/dashboard",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    assert "Operational intelligence field" in response.text
    assert "Raw ingestion" in response.text
    assert "Real investigation chains" in response.text
    assert "All-time corpus" in response.text


def test_operator_overview_includes_public_pipeline_status(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/overview?hours=24",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    status = response.json()["pipeline_status"]
    assert "raw_observations" in status["pipeline"]
    assert "analysis_outputs" in status["pipeline"]


def test_public_status_excludes_legacy_unclassified_from_corroborated(monkeypatch):
    from datetime import datetime, timezone

    import core.api.routes.status as status_route
    from core.api.main import app
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        for episode_id, family in (
            ("recognized-corrob", "dark_transit_episode"),
            ("legacy-unclassified-corrob", "unclassified_episode"),
        ):
            db.add(MaritimeEpisodeDB(
                episode_id=episode_id,
                episode_family=family,
                subject_ids=["211000001"],
                start_at=now,
                end_at=now,
                geometry={"type": "Point", "coordinates": [14.5, 35.9]},
                observation_ids=["obs-1", "obs-2"],
                feature_ids=["event-1", "event-2"],
                independence_groups=["ais_sensor_lineage", "satellite_sensor_lineage"],
                verification_status="multi_source_corroborated",
                behaviour_context={},
                alternative_explanations=[],
                evidence_fingerprint=episode_id,
                method_version="test",
                status="active",
                updated_at=now,
            ))

    status_route._status_cache = None
    response = TestClient(app).get("/api/v1/status")
    assert response.status_code == 200
    pipeline = response.json()["pipeline"]
    assert pipeline["maritime_episodes"] == 1
    assert pipeline["corroborated_episodes"] == 1
