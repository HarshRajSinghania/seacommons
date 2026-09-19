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


def test_operator_funnel_exposes_full_evidence_ladder(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/funnel?hours=24",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [stage["id"] for stage in payload["stages"]] == [
        "raw", "normalized", "derived", "episodes", "hypotheses",
        "corroborated", "review_ready", "live",
    ]
    assert "hypothesis_states" in payload["diagnostics"]
    assert "episode_verification" in payload["diagnostics"]


def test_operator_pipeline_map_documents_gap_semantics(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/pipeline-map",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    chains = response.json()["chains"]
    ais = next(chain for chain in chains if chain["id"] == "ais_position_integrity")
    assert any("reappearance" in value for value in ais["raw_observations"])
    assert any("canonical dark-gap detector" in value for value in ais["derived_processors"])


def test_operator_case_surface_labels_hypotheses_not_illegality_findings(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/cases?limit=5",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "not findings of illegality" in payload["note"].lower()
    for case in payload["cases"]:
        assert "illegal_activity_status" in case
        assert "evidence" in case
        assert "blockers" in case


def test_operator_funnel_excludes_legacy_unclassified_from_corroborated(monkeypatch):
    from datetime import datetime, timezone

    from core.api.main import app
    from core.config import config
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
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

    response = TestClient(app).get(
        "/api/v1/operator/ingestion/funnel?hours=24",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    stages = {stage["id"]: stage["count"] for stage in response.json()["stages"]}
    assert stages["episodes"] == 1
    assert stages["corroborated"] == 1


def test_operator_overall_exposes_all_time_corpus(monkeypatch):
    import core.api.routes.operator_ingestion as operator_route
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    operator_route._overall_cache = None
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/overall",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "all_time"
    assert [stage["id"] for stage in payload["stages"]] == [
        "raw", "normalized", "derived", "episodes", "hypotheses",
        "corroborated", "review_ready",
    ]
    assert set(payload["sensor_activity"]) >= {
        "ais_fixes", "radio_bursts", "radio_events", "satellite_observations", "source_names",
    }
    assert set(payload["breakdowns"]) >= {
        "raw_by_source", "raw_observation_types", "normalized_event_types",
        "episode_families", "hypothesis_types", "hypothesis_states",
    }
