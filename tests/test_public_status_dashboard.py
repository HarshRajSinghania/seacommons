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
