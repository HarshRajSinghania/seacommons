# SPDX-License-Identifier: AGPL-3.0-or-later
"""AIS spike detector: circular search-pattern fit and the distress cross-check."""
from __future__ import annotations

import math
import os

os.environ["SEACOMMONS_TRACK_STORE_SYNC"] = "1"

from datetime import datetime, timezone

import pytest
from core.intel.ais_spike_detector import (
    AISSpikeDetector,
    _nearby_active_distress,
    _ngo_circular_pattern,
    backfill_recent_sar_activity,
)
from core.intel.ngo_registry import get_ngo_info
from core.intel.store import IntelEvent, intel_store
from core.vessels.track_store import track_store

_OCEAN_VIKING = "258479000"  # SOS Méditerranée -- real entry in ngo_registry


@pytest.fixture(autouse=True)
def _clean():
    with intel_store._lock:
        intel_store._events.clear()
        intel_store._seen.clear()
        intel_store._subscribers.clear()
    with track_store._buf_lock:
        track_store._buffer.clear()
    track_store._last.clear()
    track_store._last_write_epoch.clear()
    from core.db.models import VesselTrackDB
    from core.db.session import session_scope
    with session_scope() as db:
        db.query(VesselTrackDB).delete()
    yield


def _lay_ring(mmsi, center_lat=35.5, center_lon=12.6, radius_m=500, n=10, sog=3.0):
    for k in range(n):
        ang = k / n * 2 * math.pi
        dlat = radius_m * math.sin(ang) / 111_320
        dlon = radius_m * math.cos(ang) / (111_320 * math.cos(math.radians(center_lat)))
        track_store.on_position(mmsi, mmsi, center_lat + dlat, center_lon + dlon,
                                sog=sog, nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch[mmsi] = 0.0


def test_ngo_circular_pattern_needs_enough_fixes():
    _lay_ring(_OCEAN_VIKING, n=3)
    assert _ngo_circular_pattern(_OCEAN_VIKING) is None


def test_ngo_circular_pattern_detects_search_box():
    _lay_ring(_OCEAN_VIKING, n=10, radius_m=500)
    hit = _ngo_circular_pattern(_OCEAN_VIKING)
    assert hit is not None
    r_m, detail = hit
    assert 150 <= r_m <= 4000
    assert "ring radius" in detail


def test_ngo_circular_pattern_ignores_a_straight_line():
    for k in range(10):
        track_store.on_position(_OCEAN_VIKING, _OCEAN_VIKING, 35.5 + k * 0.01, 12.6,
                                sog=8.0, nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch[_OCEAN_VIKING] = 0.0
    assert _ngo_circular_pattern(_OCEAN_VIKING) is None


def test_nearby_active_distress_finds_within_radius():
    intel_store.add(IntelEvent(
        id="distress-1", type="distress", severity="critical",
        lat=35.5, lon=12.6, title="Distress report — ~40 people",
        source="alarm_phone", metadata={"is_distress": True, "incident_lifecycle": "active"},
    ))
    hit = _nearby_active_distress(35.55, 12.65)  # a few nm away
    assert hit is not None
    assert hit["case_id"] == "distress-1"


def test_nearby_active_distress_ignores_resolved_case():
    intel_store.add(IntelEvent(
        id="distress-2", type="distress", severity="critical",
        lat=35.5, lon=12.6, title="Distress report — resolved",
        source="alarm_phone", metadata={"is_distress": True, "incident_lifecycle": "resolved"},
    ))
    assert _nearby_active_distress(35.5, 12.6) is None


def test_nearby_active_distress_ignores_far_case():
    intel_store.add(IntelEvent(
        id="distress-3", type="distress", severity="critical",
        lat=10.0, lon=10.0, title="Distress report — far away",
        source="alarm_phone", metadata={"is_distress": True, "incident_lifecycle": "active"},
    ))
    assert _nearby_active_distress(35.5, 12.6) is None


def test_emit_rescue_cluster_flags_possible_response_near_active_distress():
    intel_store.add(IntelEvent(
        id="distress-4", type="distress", severity="critical",
        lat=35.5, lon=12.6, title="Distress report — ~40 people",
        source="alarm_phone", metadata={"is_distress": True, "incident_lifecycle": "active"},
    ))
    d = AISSpikeDetector()
    d._emit(
        spike_type="rescue_cluster", mmsi=_OCEAN_VIKING, name="Ocean Viking",
        lat=35.51, lon=12.61, severity="high", detail="Rescue cluster: 2 vessels within 3nm",
    )
    events = [e for e in intel_store.events(limit=10) if e.type == "ais_spike"]
    assert len(events) == 1
    ev = events[0]
    assert ev.severity == "critical"
    assert ev.metadata["possible_response_to"]["case_id"] == "distress-4"
    assert "possible response, not confirmed" in ev.text


def test_emit_rescue_cluster_without_nearby_distress_is_unflagged():
    d = AISSpikeDetector()
    d._emit(
        spike_type="rescue_cluster", mmsi=_OCEAN_VIKING, name="Ocean Viking",
        lat=35.51, lon=12.61, severity="high", detail="Rescue cluster: 2 vessels within 3nm",
    )
    ev = [e for e in intel_store.events(limit=10) if e.type == "ais_spike"][0]
    assert "possible_response_to" not in ev.metadata
    assert ev.severity == "high"


def test_emit_respects_cooldown_even_when_process_just_booted(monkeypatch):
    """Regression guard for the CI-only flake: a never-before-seen cooldown
    key must never be treated as "just emitted at t=0" -- that made the very
    first emission of any spike type vanish on a freshly booted process where
    time.monotonic() itself starts near zero (e.g. a fresh CI runner)."""
    import core.intel.ais_spike_detector as mod
    monkeypatch.setattr(mod.time, "monotonic", lambda: 12.0)  # < emit_cooldown_s
    d = AISSpikeDetector()
    d._emit(
        spike_type="rescue_cluster", mmsi=_OCEAN_VIKING, name="Ocean Viking",
        lat=35.51, lon=12.61, severity="high", detail="Rescue cluster: 2 vessels within 3nm",
    )
    assert len([e for e in intel_store.events(limit=10) if e.type == "ais_spike"]) == 1


def _cluster_feature(mmsi, lat, lon, *, speed=4.0, age_s=120):
    from datetime import timedelta
    seen = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "mmsi": mmsi, "ship_name": mmsi, "last_speed": speed,
            "last_course": 90.0, "last_seen": seen, "destination": "",
        },
    }


def _run_cluster_scan(monkeypatch, features):
    det = AISSpikeDetector()
    monkeypatch.setattr(
        "core.vessels.registry.registry.get_geojson",
        lambda: {"type": "FeatureCollection", "features": features},
    )
    emitted: list = []
    monkeypatch.setattr(det, "_emit", lambda **kw: emitted.append(kw))
    det._scan()
    return det, emitted


_CARGO = "999000111"


def test_converging_fresh_cluster_near_distress_is_a_rescue_cluster(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector.is_ngo", lambda m: m == _OCEAN_VIKING)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_hotspot", lambda a, b: "Central Med")
    monkeypatch.setattr(
        "core.intel.ais_spike_detector._nearby_active_distress",
        lambda a, b: {"case_id": "d1", "title": "distress", "distance_nm": 5.0},
    )
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)

    # scan 1: 2.4 nm apart, no previous positions -> convergence unknown -> possible
    det, e1 = _run_cluster_scan(monkeypatch, [
        _cluster_feature(_OCEAN_VIKING, 35.50, 12.60),
        _cluster_feature(_CARGO, 35.50, 12.64),
    ])
    assert e1 and e1[0]["spike_type"] == "possible_rescue_cluster"
    # scan 2: closed to ~1.2 nm
    monkeypatch.setattr(
        "core.vessels.registry.registry.get_geojson",
        lambda: {"type": "FeatureCollection", "features": [
            _cluster_feature(_OCEAN_VIKING, 35.50, 12.60),
            _cluster_feature(_CARGO, 35.50, 12.62),
        ]},
    )
    emitted: list = []
    monkeypatch.setattr(det, "_emit", lambda **kw: emitted.append(kw))
    det._scan()
    assert len(emitted) == 1
    assert emitted[0]["spike_type"] == "rescue_cluster"
    assert emitted[0]["metadata"]["converging"] is True
    assert emitted[0]["metadata"]["closing_nm"] > 0


def test_stale_positions_do_not_form_a_cluster(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector.is_ngo", lambda m: m == _OCEAN_VIKING)
    _, emitted = _run_cluster_scan(monkeypatch, [
        _cluster_feature(_OCEAN_VIKING, 35.50, 12.60, age_s=4000),
        _cluster_feature(_CARGO, 35.50, 12.62, age_s=4000),
    ])
    assert emitted == []


def test_proximity_without_convergence_is_only_possible(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector.is_ngo", lambda m: m == _OCEAN_VIKING)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_hotspot", lambda a, b: "Central Med")
    monkeypatch.setattr("core.intel.ais_spike_detector._nearby_active_distress", lambda a, b: None)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)

    feats = [
        _cluster_feature(_OCEAN_VIKING, 35.50, 12.60),
        _cluster_feature(_CARGO, 35.50, 12.62),
    ]
    det, _ = _run_cluster_scan(monkeypatch, feats)
    emitted: list = []
    monkeypatch.setattr(det, "_emit", lambda **kw: emitted.append(kw))
    det._scan()  # same positions -> not converging
    assert len(emitted) == 1
    assert emitted[0]["spike_type"] == "possible_rescue_cluster"
    assert emitted[0]["severity"] == "medium"
    assert emitted[0]["metadata"]["converging"] is False


def _stop_feature(mmsi, lat, lon, *, speed, nav_status=None, age_s=60):
    from datetime import timedelta
    seen = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    props = {
        "mmsi": mmsi, "ship_name": mmsi, "last_speed": speed,
        "last_course": 90.0, "last_seen": seen, "destination": "",
    }
    if nav_status is not None:
        props["nav_status"] = nav_status
    return {"geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def _scan_with(monkeypatch, det, features, emitted):
    monkeypatch.setattr(
        "core.vessels.registry.registry.get_geojson",
        lambda: {"type": "FeatureCollection", "features": features},
    )
    monkeypatch.setattr(det, "_emit", lambda **kw: emitted.append(kw))
    det._scan()


# ── sudden_stop: cue vs alert (audit SP-6, prompt.md PHASE 7B) ────────────────
_UNDERWAY = 34.90, 12.00  # open water, Strait of Sicily hotspot


def test_single_sample_stop_is_a_cue_not_an_alert(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)
    det = AISSpikeDetector()
    emitted: list = []
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=7.0)], emitted)
    assert emitted == []  # first sighting, no prior speed
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.1)], emitted)
    assert len(emitted) == 1
    assert emitted[0]["spike_type"] == "possible_sudden_stop"
    assert emitted[0]["severity"] == "medium"
    assert emitted[0]["metadata"]["promoted_from_cue"] is False


def test_persistent_stop_is_promoted_to_sudden_stop(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)
    monkeypatch.setattr("core.intel.ais_spike_detector._sudden_stop_persistence_s", lambda: 0.0)
    monkeypatch.setattr("core.intel.ais_spike_detector._sudden_stop_min_samples", lambda: 2)
    det = AISSpikeDetector()
    emitted: list = []
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=7.0)], emitted)
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.1)], emitted)
    emitted.clear()
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.1)], emitted)
    assert len(emitted) == 1
    assert emitted[0]["spike_type"] == "sudden_stop"
    assert emitted[0]["metadata"]["stop_samples"] == 2
    assert emitted[0]["metadata"]["promoted_from_cue"] is True


def test_anchored_vessel_is_not_a_sudden_stop(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)
    det = AISSpikeDetector()
    emitted: list = []
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=7.0)], emitted)
    _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.1, nav_status=1)], emitted)
    assert emitted == []


# ── vessel_loiter: nav-status exclusion (audit SP-3, prompt.md PHASE 7C) ──────
def test_moored_vessel_does_not_loiter(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_hotspot", lambda a, b: "Central Med")
    monkeypatch.setattr("core.intel.ais_spike_detector.LOITER_MIN_S", 0)
    det = AISSpikeDetector()
    emitted: list = []
    for _ in range(3):
        _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.0, nav_status=5)], emitted)
    assert [e for e in emitted if e["spike_type"] == "vessel_loiter"] == []


def test_loiter_fires_when_nav_status_is_underway(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_hotspot", lambda a, b: "Central Med")
    monkeypatch.setattr("core.intel.ais_spike_detector.LOITER_MIN_S", 0)
    det = AISSpikeDetector()
    emitted: list = []
    for _ in range(3):
        _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.0, nav_status=0)], emitted)
    loiter = [e for e in emitted if e["spike_type"] == "vessel_loiter"]
    assert loiter
    assert loiter[0]["metadata"]["nav_status_known"] is True


def test_loiter_without_nav_status_is_flagged_unknown(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: False)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_hotspot", lambda a, b: "Central Med")
    monkeypatch.setattr("core.intel.ais_spike_detector.LOITER_MIN_S", 0)
    det = AISSpikeDetector()
    emitted: list = []
    for _ in range(3):
        _scan_with(monkeypatch, det, [_stop_feature(_CARGO, *_UNDERWAY, speed=0.0)], emitted)
    loiter = [e for e in emitted if e["spike_type"] == "vessel_loiter"]
    assert loiter
    assert loiter[0]["metadata"]["nav_status_known"] is False


def test_ngo_vessels_moored_in_port_are_not_a_rescue(monkeypatch):
    monkeypatch.setattr("core.intel.ais_spike_detector.is_ngo", lambda m: True)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_hotspot", lambda a, b: None)
    monkeypatch.setattr("core.intel.ais_spike_detector._nearby_active_distress", lambda a, b: None)
    monkeypatch.setattr("core.intel.ais_spike_detector._in_port", lambda a, b: True)

    feats = [
        _cluster_feature(_OCEAN_VIKING, 37.80, 12.44, speed=0.0),
        _cluster_feature(_CARGO, 37.80, 12.45, speed=0.0),
    ]
    det, _ = _run_cluster_scan(monkeypatch, feats)
    # bring them closer on scan 2 -- still in port, still not moving
    monkeypatch.setattr(
        "core.vessels.registry.registry.get_geojson",
        lambda: {"type": "FeatureCollection", "features": [
            _cluster_feature(_OCEAN_VIKING, 37.80, 12.44, speed=0.0),
            _cluster_feature(_CARGO, 37.80, 12.445, speed=0.0),
        ]},
    )
    emitted: list = []
    monkeypatch.setattr(det, "_emit", lambda **kw: emitted.append(kw))
    det._scan()
    assert emitted and emitted[0]["spike_type"] == "possible_rescue_cluster"


def test_search_pattern_normalizes_active_responder_as_neutral_sar_observation():
    d = AISSpikeDetector()
    d._emit(
        spike_type="ngo_search_pattern",
        mmsi=_OCEAN_VIKING,
        name="Ocean Viking",
        lat=35.51,
        lon=12.61,
        severity="high",
        detail="Ocean Viking executing search pattern",
        ngo_info=get_ngo_info(_OCEAN_VIKING),
        metadata={"pattern": "course_change"},
    )
    normalized = [e for e in intel_store.events(limit=20) if e.type == "ngo_activity"]
    assert len(normalized) == 1
    ev = normalized[0]
    assert ev.metadata["activity_kind"] == "search_pattern_observed"
    assert ev.metadata["source_lineage"] == "ais_sensor_lineage"
    assert ev.metadata["independent_source_count"] == 1
    assert ev.metadata["verification_status"] == "single_source_observed"
    assert ev.metadata["publication_status"] == "published"
    assert ev.metadata["maritime_domain"] == "sar"
    assert "does not by itself confirm" in ev.text


def test_possible_cluster_does_not_normalize_into_public_sar_activity():
    d = AISSpikeDetector()
    d._emit(
        spike_type="possible_rescue_cluster",
        mmsi=_OCEAN_VIKING,
        name="Ocean Viking",
        lat=35.51,
        lon=12.61,
        severity="medium",
        detail="Two vessels nearby; convergence unknown",
        ngo_info=get_ngo_info(_OCEAN_VIKING),
        metadata={"converging": None, "in_port_or_anchorage": False},
    )
    assert not [e for e in intel_store.events(limit=20) if e.type == "ngo_activity"]


def test_recent_sar_backfill_reuses_canonical_normalization(monkeypatch):
    source = IntelEvent(
        id="legacy-rescue-cluster",
        timestamp_utc="2026-09-18T21:04:25+00:00",
        type="ais_spike",
        severity="high",
        lat=37.50,
        lon=12.02,
        title="AIS: Rescue Cluster — OCEAN VIKING",
        source="AIS Registry",
        linked_mmsi=_OCEAN_VIKING,
        metadata={
            "spike_type": "rescue_cluster",
            "converging": True,
            "in_port_or_anchorage": False,
        },
    )

    def persisted_events(*, types, **_kwargs):
        return [source] if types == ["ais_spike"] else []

    written = []
    monkeypatch.setattr(intel_store, "persisted_events", persisted_events)
    monkeypatch.setattr(
        intel_store,
        "add",
        lambda event, dedup_key="": written.append((event, dedup_key)) or True,
    )
    assert backfill_recent_sar_activity() == 1
    assert written[0][0].type == "ngo_activity"
    assert written[0][0].timestamp_utc == source.timestamp_utc
    assert written[0][0].metadata["activity_kind"] == "responder_convergence_observed"
