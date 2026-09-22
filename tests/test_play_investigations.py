from __future__ import annotations

import json
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
    from core.api.routes import play as play_routes
    from core.db.models import (
        IntelEventDB,
        InvestigationHypothesisDB,
        MaritimeEpisodeDB,
        RadioAISAssociationDB,
        SourceObservationDB,
    )
    from core.db.session import session_scope

    play_routes._play_catalog_cache.clear()
    play_routes._play_counts_cache.clear()
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
        db.query(MaritimeEpisodeDB).delete()
        db.query(RadioAISAssociationDB).filter(
            RadioAISAssociationDB.observation_id.like("obs:play-%")
        ).delete(synchronize_session=False)
        db.query(SourceObservationDB).filter(
            SourceObservationDB.observation_id.like("obs:play-%")
        ).delete(synchronize_session=False)
        db.query(IntelEventDB).filter(
            IntelEventDB.id.in_(("legacy-evidence-play-test", "play-radio-evidence"))
        ).delete(synchronize_session=False)
    yield
    play_routes._play_catalog_cache.clear()
    play_routes._play_counts_cache.clear()
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
        db.query(MaritimeEpisodeDB).delete()
        db.query(RadioAISAssociationDB).filter(
            RadioAISAssociationDB.observation_id.like("obs:play-%")
        ).delete(synchronize_session=False)
        db.query(SourceObservationDB).filter(
            SourceObservationDB.observation_id.like("obs:play-%")
        ).delete(synchronize_session=False)
        db.query(IntelEventDB).filter(
            IntelEventDB.id.in_(("legacy-evidence-play-test", "play-radio-evidence"))
        ).delete(synchronize_session=False)


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


def test_play_catalog_excludes_collecting_investigation():
    # docs/nextstep spec sec 4: candidate/collecting are internal Live
    # investigation state, never automatically published into Play -- Play
    # must not become an unfiltered detector log.
    hypothesis_id = _seed_investigation()
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert all(item["incident_id"] != hypothesis_id for item in rows)


def test_play_catalog_exposes_review_ready_corroborated_investigation():
    hypothesis_id = _seed_investigation(state="review_ready")
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    row = next(item for item in response.json()["incidents"] if item.get("hypothesis_id") == hypothesis_id)
    assert row["episode_id"] == row["incident_id"]
    assert row["domain"] == "maritime"
    assert row["main_category"] == "maritime"
    assert row["investigation"] is True
    assert row["case_type"] == "dark_transit"
    assert row["incident_status"] == "review_ready"
    assert row["evidence_stage"] == "corroborated"
    assert row["geometry"] == {"type": "Point", "coordinates": [14.1, 35.5]}
    assert row["title"] == "Dark transit investigation"
    assert row["review_boundary_crossed"] is True


def test_play_catalog_keeps_review_ready_derived_investigation_with_stage():
    hypothesis_id = _seed_investigation(state="review_ready", evidence_stage="derived")
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    row = next(item for item in rows if item.get("hypothesis_id") == hypothesis_id)
    assert row["episode_id"] == row["incident_id"]
    assert row["incident_status"] == "review_ready"
    assert row["evidence_stage"] == "derived"
    assert row["review_boundary_crossed"] is True


def test_play_catalog_excludes_unadvanced_candidate():
    hypothesis_id = _seed_investigation(state="candidate")
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert all(item["incident_id"] != hypothesis_id for item in rows)


def test_play_collecting_investigation_timeline_not_available():
    # Same promotion boundary applies to the single-incident timeline
    # endpoint: a collecting hypothesis has no public Play surface at all,
    # so it falls through to the generic-maritime/humanitarian lookup and
    # 404s rather than leaking an internal investigation.
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
    assert payload["domain"] == "maritime"
    assert payload["main_category"] == "maritime"
    assert payload["investigation"] is True
    assert payload["incident_status"] == "review_ready"
    types = [item["type"] for item in payload["timeline"]]
    assert "episode" in types
    assert payload["episode_id"] == payload["incident_id"]
    assert payload["hypothesis_id"] == hypothesis_id
    assert "211879870" not in response.text


def test_play_keeps_legacy_hypothesis_without_episode_using_evidence_fallback():
    from dataclasses import replace

    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(IntelEventDB(
            id="legacy-evidence-play-test",
            timestamp_utc=now.isoformat(),
            type="ais_anomaly",
            severity="medium",
            lat=36.25,
            lon=14.75,
            title="Legacy AIS anomaly",
            text="",
            url="",
            source="mda",
            linked_mmsi="",
            meta={"anomaly_type": "position_jump"},
        ))

    hyp = new_hypothesis(
        "hyp:v1:position_spoofing:legacy-play-test",
        "position_spoofing",
        ("subj:legacy-redacted",),
        episode_id=None,
    )
    hyp = replace(
        hyp,
        evidence_links=("legacy-evidence-play-test",),
        evidence_stage="derived",
        reason_codes=("POSITION_JUMP",),
    )
    # Must cross the public promotion boundary (review_ready+) to appear in
    # Play at all -- a bare candidate/collecting legacy row would now be
    # correctly excluded, same as any other hypothesis.
    hyp = transition(hyp, "collecting", actor="test")
    hyp = transition(hyp, "review_ready", actor="test")
    save_hypothesis(hyp)

    client = TestClient(app)
    rows = client.get("/api/v1/play/incidents?limit=500").json()["incidents"]
    row = next(item for item in rows if item["incident_id"] == hyp.hypothesis_id)
    assert row["archive_source"] == "legacy_hypothesis"
    assert row["episode_present"] is False
    assert row["geometry"] == {"type": "Point", "coordinates": [14.75, 36.25]}

    timeline = client.get(
        f"/api/v1/play/incidents/{hyp.hypothesis_id}/timeline"
    )
    assert timeline.status_code == 200
    payload = timeline.json()
    assert payload["timeline"][0]["geometry"] == {
        "type": "Point", "coordinates": [14.75, 36.25]
    }
    assert "legacy-evidence-play-test" not in timeline.text


def _seed_rejected_hypothesis(*, preserve: bool) -> str:
    suffix = "preserved" if preserve else "private"
    hyp = new_hypothesis(
        f"hyp:v1:dark_transit:rejected-{suffix}",
        "dark_transit",
        ("subj:redacted",),
    )
    reasons = ["COUNTER_EVIDENCE_RESOLVED"]
    if preserve:
        reasons.append("PRESERVE_NEGATIVE_CASE")
    hyp = replace(
        hyp,
        reason_codes=tuple(reasons),
        evidence_links=("negative-evidence-a", "negative-evidence-b"),
        evidence_stage="assessed",
        explicit_review_done=preserve,
    )
    hyp = transition(hyp, "collecting", actor="test")
    hyp = transition(hyp, "rejected", actor="test")
    save_hypothesis(hyp)
    return hyp.hypothesis_id


def test_play_rejected_hypothesis_is_private_by_default():
    hypothesis_id = _seed_rejected_hypothesis(preserve=False)
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert all(item["incident_id"] != hypothesis_id for item in rows)


def test_play_explicitly_preserved_negative_case_is_public():
    hypothesis_id = _seed_rejected_hypothesis(preserve=True)
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    row = next(item for item in rows if item["incident_id"] == hypothesis_id)
    assert row["archive_decision"] == "rejected"
    assert row["incident_status"] == "rejected"


def test_play_timeline_includes_strong_radio_evidence_without_mmsi_leak():
    from core.db.models import IntelEventDB, RadioAISAssociationDB, SourceObservationDB
    from core.db.session import session_scope
    from core.intel.hypothesis_store import get_hypothesis

    now = datetime.now(timezone.utc)
    hypothesis_id = _seed_investigation(state="review_ready")
    with session_scope() as db:
        db.add(IntelEventDB(
            id="play-radio-evidence", timestamp_utc=now.isoformat(),
            type="ais_anomaly", severity="medium", lat=35.5, lon=14.1,
            title="AIS gap", text="", url="", source="mda",
            linked_mmsi="211879870", meta={"anomaly_type": "gap"},
        ))
        payload = {
            "category": "distress", "mmsi": "211879870",
            "nature_code": "fire", "nature_description": "Fire, explosion",
            "format": "distress",
        }
        db.add(SourceObservationDB(
            observation_id="obs:play-radio-1", service="maritime", lane="safety",
            observation_type="dsc_message", source_name="radio_receiver:test",
            source_policy="structured_remote_radio_decoder", source_id="dsc:play-radio-1",
            source_url="", observed_at=now.isoformat(), raw_payload_hash="a" * 64,
            raw_payload_ref="", lat=35.5, lon=14.1, subject_refs=[],
            provenance={"frequency_hz": 2187500, "structured_payload": json.dumps(payload)},
        ))
        db.add(RadioAISAssociationDB(
            observation_id="obs:play-radio-1", mmsi="211879870",
            match_status="strong", confidence=0.95, distance_km=2.1,
            ais_observed_at=now.isoformat(), episode_eligible=True,
        ))

    hyp = get_hypothesis(hypothesis_id)
    assert hyp is not None
    hyp = replace(
        hyp,
        evidence_links=("play-radio-evidence", "sat:gfw_sar:test"),
    )
    save_hypothesis(hyp)

    response = TestClient(app).get(f"/api/v1/play/incidents/{hypothesis_id}/timeline")
    assert response.status_code == 200
    payload = response.json()
    assert payload["radio_count"] == 1
    radio = next(item for item in payload["timeline"] if item["type"] == "radio")
    assert radio["properties"]["match_status"] == "strong"
    assert radio["properties"]["nature_description"] == "Fire, explosion"
    assert "211879870" not in response.text
