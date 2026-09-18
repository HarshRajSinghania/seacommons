from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from core.api.main import app
from core.intel.episode_store import save_episode
from core.intel.hypothesis import new_hypothesis, transition
from core.intel.hypothesis_store import save_hypothesis
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_investigations():
    from core.db.models import InvestigationHypothesisDB, MaritimeEpisodeDB
    from core.db.session import session_scope
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
        db.query(MaritimeEpisodeDB).delete()
    yield
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
        db.query(MaritimeEpisodeDB).delete()


def _seed_investigation(*, state="collecting", kind="dark_transit", evidence_stage=None):
    episode_id = f"episode:test:{kind}:{state}:{evidence_stage or 'auto'}"
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id, "episode_family": "gap_episode",
            "subject_ids": ["subj:mmsi:211879870"],
            "related_signal_ids": ["evidence-a"],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "verification_status": "single_source_observed",
        },
    })
    hyp = new_hypothesis(
        f"hyp:v1:{kind}:{episode_id}", kind, ("subj:mmsi:211879870",),
        episode_id=episode_id,
    )
    stage = evidence_stage or (
        "corroborated" if state in {"review_ready", "assessed", "published"} else "derived"
    )
    hyp = replace(
        hyp,
        reason_codes=("SATELLITE_CANDIDATE_IN_REACHABLE_AREA",),
        evidence_links=("evidence-a", "sat:gfw_sar:test"),
        evidence_stage=stage,
    )
    if state != "candidate":
        hyp = transition(hyp, "collecting", actor="test")
    if state in {"review_ready", "assessed", "published"}:
        hyp = transition(hyp, "review_ready", actor="test")
    if state in {"assessed", "published"}:
        hyp = transition(hyp, "assessed", actor="test")
    if state == "published":
        hyp = transition(hyp, "published", actor="test")
    save_hypothesis(hyp)
    return hyp.hypothesis_id


def test_play_catalog_hides_collecting_investigation_until_review_ready():
    hypothesis_id = _seed_investigation()
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert hypothesis_id not in {item["incident_id"] for item in rows}


def test_play_catalog_exposes_review_ready_corroborated_investigation():
    hypothesis_id = _seed_investigation(state="review_ready")
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    row = next(item for item in response.json()["incidents"] if item["incident_id"] == hypothesis_id)
    assert row["domain"] == "investigation"
    assert row["case_type"] == "dark_transit"
    assert row["incident_status"] == "review_ready"
    assert row["evidence_stage"] == "corroborated"
    assert row["geometry"] == {"type": "Point", "coordinates": [14.1, 35.5]}
    assert row["title"] == "Dark transit investigation"


def test_play_catalog_hides_review_ready_derived_investigation():
    hypothesis_id = _seed_investigation(state="review_ready", evidence_stage="derived")
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert hypothesis_id not in {item["incident_id"] for item in rows}


def test_play_catalog_hides_unadvanced_candidate_noise():
    hypothesis_id = _seed_investigation(state="candidate")
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert hypothesis_id not in {item["incident_id"] for item in rows}


def test_play_collecting_investigation_timeline_is_not_public():
    hypothesis_id = _seed_investigation()
    response = TestClient(app).get(
        f"/api/v1/play/incidents/{hypothesis_id}/timeline"
    )
    assert response.status_code == 404


def test_play_review_ready_timeline_exposes_evidence_not_vessel_identity():
    hypothesis_id = _seed_investigation(state="review_ready")
    response = TestClient(app).get(
        f"/api/v1/play/incidents/{hypothesis_id}/timeline"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["domain"] == "investigation"
    assert payload["incident_status"] == "review_ready"
    types = [item["type"] for item in payload["timeline"]]
    assert "hypothesis" in types
    assert "211879870" not in response.text
