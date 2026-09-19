from __future__ import annotations

from fastapi.testclient import TestClient


def test_operator_ingestion_gateway_fails_closed_without_secret(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get("/api/v1/operator/ingestion/overview")
    assert response.status_code == 401


def test_internal_proxy_secret_does_not_unlock_operator_ingestion(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    monkeypatch.setattr(config, "INTERNAL_PROXY_SECRET", "internal-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/overview",
        headers={"x-seacommons-internal": "internal-secret"},
    )
    assert response.status_code == 401


def test_operator_ingestion_overview_works_with_gateway_secret(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/overview?hours=24",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["counts"]) == {
        "source_observations",
        "parsed_events",
        "ingested_signals",
    }
    assert "acquisition" in payload
    assert "sources" in payload


def test_operator_observation_surface_never_claims_inline_raw_payload(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/observations?limit=10",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    for row in response.json()["observations"]:
        assert "raw_payload" not in row
        assert "raw_payload_hash" in row
        assert "raw_payload_ref" in row
