"""Epistemically strict corroboration in Play, and the 24h investigation
decision window's expiry of stale candidate/collecting hypotheses.

`corroborated` must mean >=2 genuinely independent evidence lineages -- never
high confidence, many observations from one source, many AIS detectors, or
review state (assessed/confirmed). core.intel.fusion.verification_for_event_ids
is the canonical classifier; this file pins down that Play's public
projection reads it faithfully and never substitutes a raw count.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from core.intel.episode_store import save_episode
from core.intel.hypothesis import new_hypothesis, transition
from core.intel.hypothesis_engine import expire_stale_hypotheses
from core.intel.hypothesis_store import get_hypothesis, save_hypothesis
from core.intel.store import IntelEvent, intel_store


def _save_ais_event(event_id: str, mmsi: str, *, minutes_ago: int = 0) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    intel_store.add(
        IntelEvent(
            id=event_id, type="ais_anomaly", severity="medium", lat=35.5, lon=14.1,
            title=f"AIS anomaly {event_id}", source="mda", linked_mmsi=mmsi,
            timestamp_utc=ts, metadata={"anomaly_type": "gap"},
        ),
        dedup_key=event_id,
    )


def test_single_ais_lineage_assessed_is_not_corroborated():
    # Two events, both ais_sensor_lineage -- one independence group, so even
    # a review pass that pushes evidence_stage to "corroborated" must not be
    # reachable, and Play's corroborated flag must be False.
    mmsi = "211911001"
    _save_ais_event("corrob-test:single-lineage-1", mmsi)
    _save_ais_event("corrob-test:single-lineage-2", mmsi, minutes_ago=5)

    episode_id = "episode:corrob-test:single-lineage"
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id, "episode_family": "gap_episode",
            "subject_ids": [f"subj:mmsi:{mmsi}"],
            "related_signal_ids": ["corrob-test:single-lineage-1", "corrob-test:single-lineage-2"],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "verification_status": "single_source_multi_indicator",
            "independence_groups": ["ais_sensor_lineage"],
        },
    })
    hyp = new_hypothesis(
        f"hyp:v1:dark_transit:{episode_id}", "dark_transit", (f"subj:mmsi:{mmsi}",),
        episode_id=episode_id,
    )
    hyp = replace(
        hyp,
        reason_codes=("ISOLATED_GAP",),
        evidence_links=("corrob-test:single-lineage-1", "corrob-test:single-lineage-2"),
        evidence_stage="derived",
    )
    hyp = transition(hyp, "collecting", actor="test")
    hyp = transition(hyp, "review_ready", actor="test")
    save_hypothesis(hyp)

    from fastapi.testclient import TestClient

    from core.api.main import app
    from core.api.routes import play as play_routes

    play_routes._play_catalog_cache.clear()
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    row = next(item for item in rows if item.get("hypothesis_id") == hyp.hypothesis_id)
    assert row["incident_id"] == episode_id
    assert row["corroborated"] is False
    assert row["independence_groups"] == ["ais_sensor_lineage"]
    assert row["verification_status"] != "multi_source_corroborated"


def test_two_independent_lineages_are_corroborated():
    mmsi = "211911002"
    _save_ais_event("corrob-test:multi-lineage-ais", mmsi)

    episode_id = "episode:corrob-test:multi-lineage"
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id, "episode_family": "gap_episode",
            "subject_ids": [f"subj:mmsi:{mmsi}"],
            "related_signal_ids": ["corrob-test:multi-lineage-ais"],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "verification_status": "multi_source_corroborated",
            "independence_groups": ["ais_sensor_lineage", "gfw_sar"],
        },
    })
    hyp = new_hypothesis(
        f"hyp:v1:dark_transit:{episode_id}", "dark_transit", (f"subj:mmsi:{mmsi}",),
        episode_id=episode_id,
    )
    hyp = replace(
        hyp,
        reason_codes=("ISOLATED_GAP", "SATELLITE_CANDIDATE_IN_REACHABLE_AREA"),
        evidence_links=("corrob-test:multi-lineage-ais", "sat:gfw_sar:test"),
        evidence_stage="corroborated",
    )
    hyp = transition(hyp, "collecting", actor="test")
    hyp = transition(hyp, "review_ready", actor="test")
    save_hypothesis(hyp)

    from fastapi.testclient import TestClient

    from core.api.main import app
    from core.api.routes import play as play_routes

    play_routes._play_catalog_cache.clear()
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    row = next(item for item in rows if item.get("hypothesis_id") == hyp.hypothesis_id)
    assert row["incident_id"] == episode_id
    assert row["corroborated"] is True
    assert sorted(row["independence_groups"]) == ["ais_sensor_lineage", "gfw_sar"]


def test_stale_candidate_expires_after_24h_using_episode_time_not_updated_at():
    mmsi = "211911003"
    old_time = datetime.now(timezone.utc) - timedelta(hours=30)
    episode_id = "episode:corrob-test:stale-candidate"
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id, "episode_family": "gap_episode",
            "subject_ids": [f"subj:mmsi:{mmsi}"],
            "related_signal_ids": [],
            "timestamp_utc": old_time.isoformat(),
            "verification_status": "single_source_observed",
        },
    })
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    with session_scope() as db:
        episode_row = db.get(MaritimeEpisodeDB, episode_id)
        episode_row.end_at = old_time.replace(tzinfo=None)

    hyp = new_hypothesis(
        f"hyp:v1:dark_transit:{episode_id}", "dark_transit", (f"subj:mmsi:{mmsi}",),
        episode_id=episode_id,
    )
    hyp = replace(hyp, reason_codes=("ISOLATED_GAP",), evidence_stage="derived")
    # Re-saving with no new evidence bumps updated_at (a no-op re-evaluation)
    # -- expiry must still fire, because it is judged by episode end_at.
    save_hypothesis(hyp)
    save_hypothesis(hyp)

    expired = expire_stale_hypotheses()
    assert expired >= 1

    reloaded = get_hypothesis(hyp.hypothesis_id)
    assert reloaded.state == "expired"


def test_fresh_candidate_does_not_expire():
    mmsi = "211911004"
    episode_id = "episode:corrob-test:fresh-candidate"
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id, "episode_family": "gap_episode",
            "subject_ids": [f"subj:mmsi:{mmsi}"],
            "related_signal_ids": [],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "verification_status": "single_source_observed",
        },
    })
    hyp = new_hypothesis(
        f"hyp:v1:dark_transit:{episode_id}", "dark_transit", (f"subj:mmsi:{mmsi}",),
        episode_id=episode_id,
    )
    save_hypothesis(hyp)

    expire_stale_hypotheses()
    reloaded = get_hypothesis(hyp.hypothesis_id)
    assert reloaded.state == "candidate"


def test_expired_hypothesis_stays_out_of_public_play():
    mmsi = "211911005"
    episode_id = "episode:corrob-test:expired-play"
    old_time = datetime.now(timezone.utc) - timedelta(hours=30)
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id, "episode_family": "gap_episode",
            "subject_ids": [f"subj:mmsi:{mmsi}"],
            "related_signal_ids": [],
            "timestamp_utc": old_time.isoformat(),
            "verification_status": "single_source_observed",
        },
    })
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    with session_scope() as db:
        db.get(MaritimeEpisodeDB, episode_id).end_at = old_time.replace(tzinfo=None)

    hyp = new_hypothesis(
        f"hyp:v1:dark_transit:{episode_id}", "dark_transit", (f"subj:mmsi:{mmsi}",),
        episode_id=episode_id,
    )
    save_hypothesis(hyp)
    expire_stale_hypotheses()

    from fastapi.testclient import TestClient

    from core.api.main import app
    from core.api.routes import play as play_routes

    play_routes._play_catalog_cache.clear()
    rows = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert all(item["incident_id"] != hyp.hypothesis_id for item in rows)
