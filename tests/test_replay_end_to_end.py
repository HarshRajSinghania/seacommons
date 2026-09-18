# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md M14.6: true end-to-end replay through real pipeline entry
points.

tests/test_replay_scenarios.py (M12) composes the ACTUAL classification/
gate functions across module boundaries, but constructs its inputs
directly rather than driving them through real ingestion/persistence/API
entry points. This file complements that catalogue: raw fixture -> real
ingestion entry point (core.vessels.track_store.on_position,
core.intel.store.intel_store.add) -> real detection/episode/hypothesis
call sites built across M14.1-M14.5 (core.mda.watch, core.live.
vessel_episodes, core.intel.hypothesis_engine/hypothesis_store) ->
real publication policy gate -> the real FastAPI API projection route,
for representative vertical slices of both the Humanitarian and Maritime
sides. It does not re-cover every H1-H6/S1-S10 scenario tested at the
module level elsewhere this session; it proves the pipeline composes end
to end through its real entry points, not just as isolated units.
"""
from __future__ import annotations

import os
import time

os.environ["SEACOMMONS_TRACK_STORE_SYNC"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from core.api.main import app
from core.intel.hypothesis import transition
from core.intel.hypothesis_store import get_hypothesis
from core.intel.store import IntelEvent, intel_store
from core.live.projection import _public_intel_feature
from core.mda.watch import MdaWatch
from core.vessels.track_store import track_store
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    from core.db.models import (
        InvestigationHypothesisDB,
        MaritimeEpisodeDB,
        VesselTrackDB,
    )
    from core.db.session import engine, session_scope

    InvestigationHypothesisDB.__table__.create(bind=engine(), checkfirst=True)
    MaritimeEpisodeDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
        db.query(MaritimeEpisodeDB).delete()
        db.query(VesselTrackDB).delete()
    with intel_store._lock:
        intel_store._events.clear()
        intel_store._seen.clear()
        intel_store._subscribers.clear()
    with track_store._buf_lock:
        track_store._buffer.clear()
    track_store._last.clear()
    track_store._last_write_epoch.clear()
    yield


def _witness(mmsi: str, lat: float, lon: float, minutes_ago: float) -> None:
    track_store.on_position(
        mmsi, mmsi, lat, lon, sog=8.0, nav_status=0,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    track_store._last_write_epoch[mmsi] = 0.0


def test_e2e_isolated_ais_gap_stays_raw_without_materialized_episode():
    """A single-lineage AIS gap stays archived raw, not a MaritimeEpisode."""
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    target = "211879801"
    track_store.on_position(target, target, 37.00, 18.00, sog=8.0, nav_status=0,
                             received_at=datetime.now(timezone.utc))
    track_store._last_write_epoch[target] = 0.0
    track_store._last[target].ts = time.time() - 5400
    for k in range(3):
        w_mmsi = f"21100091{k}"
        _witness(w_mmsi, 37.01, 18.01, minutes_ago=100)
        _witness(w_mmsi, 37.01, 18.01, minutes_ago=40)

    w = MdaWatch()
    assert w.scan_gaps() == 1
    assert w.scan_hypotheses() == 0
    with session_scope() as db:
        rows = db.query(MaritimeEpisodeDB).filter_by(episode_family="gap_episode").all()
        assert rows == []
    assert client.get("/api/v1/live/hypotheses").json()["features"] == []


def test_e2e_independent_gap_corroboration_creates_idempotent_v1_hypothesis():
    """Independent evidence creates one linked v1 hypothesis; replay never duplicates it."""
    from core.db.models import InvestigationHypothesisDB, MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.intel.hypothesis_store import list_hypotheses, save_hypothesis

    target = "211879803"
    intel_store.add(IntelEvent(
        id="e2e-gap-independent", type="ais_anomaly", severity="medium", lat=35.5, lon=14.1,
        title="isolated AIS gap", source="mda", linked_mmsi=target,
        metadata={"anomaly_type": "gap", "gap_reason": {"hypothesis": "vessel_gap", "confidence": 0.7}},
    ), dedup_key="e2e-gap-independent")
    intel_store.add(IntelEvent(
        id="e2e-gap-report", type="news", severity="medium", lat=35.5, lon=14.1,
        title="independent gap report", source="Independent report", linked_mmsi=target,
        metadata={"anomaly_type": "gap", "transport": "rss"},
    ), dedup_key="e2e-gap-report")

    w = MdaWatch()
    assert w.scan_hypotheses() == 1
    hyps = list_hypotheses(hypothesis_type="dark_transit")
    assert len(hyps) == 1
    hyp = hyps[0]
    assert hyp.hypothesis_id.startswith("hyp:v1:dark_transit:")
    assert hyp.episode_id is not None
    assert hyp.state == "review_ready"
    assert hyp.evidence_stage == "corroborated"
    assert w.scan_hypotheses() == 1
    with session_scope() as db:
        assert db.query(MaritimeEpisodeDB).count() == 1
        assert db.query(InvestigationHypothesisDB).count() == 1

    published = transition(hyp, "assessed", actor="analyst:test")
    published = transition(published, "published", actor="analyst:test")
    save_hypothesis(published)
    features = client.get("/api/v1/live/hypotheses").json()["features"]
    match = next(f for f in features if f["id"] == hyp.hypothesis_id)
    for field in ("linked_mmsi", "mmsi", "imo", "vessel_name"):
        assert field not in match["properties"]

def test_e2e_common_port_outage_never_reaches_a_hypothesis():
    """Full vertical slice: several vessels going silent together (a
    shared reception outage, not a real gap) never produces an
    ais_anomaly event at all (core.mda.watch.scan_gaps(), M14.1), so
    core.mda.watch.scan_hypotheses() (M14.3) has nothing to evaluate and
    the public hypotheses API stays empty for this vessel."""
    target = "211879802"
    track_store.on_position(target, target, 37.00, 18.00, sog=8.0, nav_status=0,
                             received_at=datetime.now(timezone.utc))
    track_store._last_write_epoch[target] = 0.0
    track_store._last[target].ts = time.time() - 5400

    for k in range(3):
        _witness(f"21100092{k}", 37.01, 18.01, minutes_ago=100)  # before only -- also went dark

    w = MdaWatch()
    assert w.scan_gaps() == 0
    assert intel_store.get(f"aisgap:{target}") is None
    assert w.scan_hypotheses() == 0

    result = client.get("/api/v1/live/hypotheses").json()
    assert result["features"] == []


def test_e2e_safety_event_never_produces_a_maritime_intelligence_hypothesis():
    """Full vertical slice: a real not_under_command IntelEvent, persisted
    through intel_store.add() (real entry point), projects correctly
    through the public API (core.live.projection._public_intel_feature,
    M14.4's privacy fix) and never becomes a Maritime Intelligence
    hypothesis at all -- safety_episode maps to no hypothesis_type
    (core.intel.hypothesis_engine, M14.3)."""
    mmsi = "352001999"
    intel_store.add(IntelEvent(
        id="nuc-e2e", type="vessel_incident", severity="high", lat=35.5, lon=14.1,
        title="Vessel unable to manoeuvre", source="mda", linked_mmsi=mmsi,
        metadata={
            "ais_nav_status_kind": "not_under_command", "maritime_domain": "safety",
            "source_policy": "official_api",
        },
    ), dedup_key="nuc-e2e")

    feature = _public_intel_feature(intel_store.get("nuc-e2e"), allowed_domains=frozenset({"safety"}))
    assert feature is not None
    assert feature["properties"]["maritime_domain"] == "safety"

    from core.live.vessel_episodes import coalesce_security_vessel_episodes

    episodes = coalesce_security_vessel_episodes([feature])
    assert len(episodes) == 1
    assert episodes[0]["properties"]["episode_family"] == "safety_episode"

    w = MdaWatch()
    assert w.scan_hypotheses() == 0  # safety_episode is never mapped to a hypothesis_type
    assert get_hypothesis(f"hyp:dark_transit:{episodes[0]['properties']['episode_id']}") is None

    result = client.get("/api/v1/live/hypotheses").json()
    assert result["features"] == []


def test_e2e_health_data_reflects_a_real_stuck_drift_job_without_manual_db_inspection():
    """Full vertical slice for the observability side (M14.5): a real
    stuck DriftResultDB row is observable through the actual GET
    /health/data route, not a synthetic summary."""
    from datetime import timedelta as _td

    from core.db.models import DriftResultDB
    from core.db.session import session_scope

    old = datetime.now(timezone.utc).replace(tzinfo=None) - _td(hours=3)
    with session_scope() as db:
        db.add(DriftResultDB(
            drift_id="e2e-stuck-drift", event_id="intel:e2e-stuck-drift", domain="ocean_sar",
            lat=35.5, lon=14.1, status="computing", created_at=old, metadata_json={},
        ))

    response = client.get("/health/data")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stuck_drift_detected"] is True
    assert payload["healthy"] is False
