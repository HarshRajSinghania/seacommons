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
    assert "Civil + state SAR fleet" in response.text
    assert "/api/v1/operator/ingestion/sar-fleet" in response.text
    assert "current fixes only" in response.text


def test_operator_dashboard_inline_javascript_compiles(monkeypatch, tmp_path):
    import shutil
    import subprocess

    import pytest
    from core.api.main import app
    from core.config import config

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard JavaScript syntax validation")

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    response = TestClient(app).get(
        "/api/v1/operator/ingestion/dashboard",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert response.status_code == 200
    start = response.text.index("<script>") + len("<script>")
    end = response.text.index("</script>", start)

    script = tmp_path / "operator-dashboard.js"
    script.write_text(response.text[start:end], encoding="utf-8")
    checked = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


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


def test_operator_sar_fleet_is_private(monkeypatch):
    from core.api.main import app
    from core.config import config

    monkeypatch.setattr(config, "OPERATOR_GATEWAY_SECRET", "operator-secret")
    client = TestClient(app)

    denied = client.get("/api/v1/operator/ingestion/sar-fleet")
    assert denied.status_code == 401

    allowed = client.get(
        "/api/v1/operator/ingestion/sar-fleet",
        headers={"x-seacommons-operator-gateway": "operator-secret"},
    )
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["type"] == "FeatureCollection"
    assert payload["meta"]["map_position_policy"] == "live_only_10m"

def test_public_status_live_count_uses_requested_window(monkeypatch):
    from datetime import datetime, timezone

    import core.api.routes.status as status_route
    from core.api.main import app

    captured = {}

    def fake_public_signal_collection(**kwargs):
        captured.update(kwargs)
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [14.5, 35.9]},
                    "properties": {
                        "id": "humanitarian-now",
                        "main_category": "humanitarian",
                        "maritime_domain": "sar",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "tier": "operational",
                    },
                }
            ],
        }

    monkeypatch.setattr(status_route, "public_signal_collection", fake_public_signal_collection)
    status_route._status_cache = None

    response = TestClient(app).get("/api/v1/status?hours=24")
    assert response.status_code == 200
    assert captured["days"] == 1
    assert captured["mode"] == "all"
    assert captured["since"]
    assert response.json()["live"] == {
        "total": 1,
        "operational": 1,
        "humanitarian": 1,
        "maritime": 0,
    }
