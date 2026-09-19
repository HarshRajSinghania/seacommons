# SPDX-License-Identifier: AGPL-3.0-or-later
"""AIS anomaly detector, now fed from the shared AIS position hook."""
from __future__ import annotations

import time

import pytest
from core.anomaly import ais as ais_mod


@pytest.fixture
def detector(monkeypatch):
    added: list = []
    from core.intel import store as store_mod

    monkeypatch.setattr(store_mod.intel_store, "add", lambda e, dedup_key="": (added.append(e), True)[1])
    det = ais_mod.AISAnomalyDetector()
    det._running = True
    det._added = added
    return det


def test_impossible_speed_becomes_an_operator_only_intel_event(detector) -> None:
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc) - timedelta(minutes=3)
    detector.process_position(
        "247012345", "X", 35.0, 14.0, 10.0, "", observed_at=base
    )
    detector._added.clear()
    detector.process_position(
        "247012345", "X", 36.0, 15.0, 300.0, "",
        observed_at=base + timedelta(minutes=1),
    )
    # First impossible leg is quarantined, not published.
    assert detector._added == []
    detector.process_position(
        "247012345", "X", 36.001, 15.001, 10.0, "",
        observed_at=base + timedelta(minutes=2),
    )
    assert len(detector._added) == 1
    event = detector._added[0]
    assert event.type == "ais_anomaly"
    assert event.metadata["anomaly_type"] == "impossible_speed"
    assert event.metadata["is_distress"] is False
    assert event.metadata["publication_status"] == "published"
    assert event.metadata["analysis_state"] == "evidence_candidate"
    assert event.metadata["offshore_anomaly_qualified"] is True
    assert event.metadata["offshore_context"]["offshore"] is True


def test_sdn_match_is_high_severity(detector) -> None:
    detector._sdn_mmsi = {"538001234"}
    detector.process_position("538001234", "SANCTIONED", 34.0, 18.0, 12.0, "")
    assert detector._added and detector._added[0].severity == "high"
    assert detector._added[0].metadata["anomaly_type"] == "sdn_match"


def test_emit_cooldown_prevents_a_flood(detector) -> None:
    detector._sdn_mmsi = {"538001234"}
    for _ in range(5):
        detector.process_position("538001234", "S", 34.0, 18.0, 12.0, "")
    assert len(detector._added) == 1


def test_dark_zone_entry_fires_once_on_crossing(detector) -> None:
    from datetime import datetime, timedelta, timezone

    zone = ais_mod._DARK_ZONES[0]
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    # Cross the boundary plausibly rather than teleporting several degrees.
    outside = (zone[0] - 0.05, zone[1] + 0.1)
    inside = (zone[0] + 0.05, zone[1] + 0.1)
    detector.process_position(
        "111", "V", outside[0], outside[1], 10.0, "", observed_at=base
    )
    detector.process_position(
        "111", "V", inside[0], inside[1], 10.0, "",
        observed_at=base + timedelta(hours=1),
    )
    kinds = {e.metadata["anomaly_type"] for e in detector._added}
    assert "dark_zone_entry" in kinds


def test_silence_sweep_emits_a_gap_for_a_vessel_gone_dark(detector) -> None:
    detector._last_seen["222"] = {
        "lat": 34.5, "lon": 18.0, "ts": time.time() - 1800,  # silent 30 min
        "speed": 12.0, "type": "", "name": "GHOST",
    }
    # run one sweep iteration inline
    now = time.time()
    for mmsi, seen in list(detector._last_seen.items()):
        silent_s = now - seen["ts"]
        if 900 < silent_s < 6 * 3600 and seen["speed"] >= 1.0 and not detector._in_dark_zone(seen["lat"], seen["lon"]):
            seen["gap_emitted"] = True
            detector._emit(ais_mod.AISAnomalyEvent(
                event_id="x", timestamp_utc="2026-08-27T00:00:00Z", anomaly_type="gap",
                mmsi=mmsi, vessel_name=seen["name"],
                position={"lat": seen["lat"], "lon": seen["lon"]},
                confidence=0.6, evidence={"silent_seconds": round(silent_s)},
            ))
    assert detector._added and detector._added[0].metadata["anomaly_type"] == "gap"


def _silent(lat, lon, *, age_s, speed=12.0, name="V"):
    return {"lat": lat, "lon": lon, "ts": time.time() - age_s, "speed": speed,
            "type": "", "name": name}


def test_gap_is_vessel_specific_when_nearby_coverage_is_healthy(detector) -> None:
    now = time.time()
    detector._last_seen["ghost"] = _silent(34.5, 18.0, age_s=1800, name="GHOST")
    for i in range(4):  # neighbours still reporting now
        detector._last_seen[f"n{i}"] = _silent(34.5 + i * 0.05, 18.1, age_s=60)
    ev = detector._build_gap_event("ghost", detector._last_seen["ghost"], 1800, now)
    assert ev.anomaly_type == "gap"
    assert ev.evidence["nearby_vessels_after"] == 4


def test_gap_becomes_coverage_gap_when_nearby_traffic_also_went_silent(detector) -> None:
    now = time.time()
    detector._last_seen["ghost"] = _silent(34.5, 18.0, age_s=1800, name="GHOST")
    for i in range(4):  # neighbours seen recently in history, none fresh now
        detector._last_seen[f"n{i}"] = _silent(34.5 + i * 0.05, 18.1, age_s=1700)
    ev = detector._build_gap_event("ghost", detector._last_seen["ghost"], 1800, now)
    assert ev.anomaly_type == "coverage_gap"
    assert ev.evidence["nearby_vessels_before"] == 4
    assert ev.evidence["nearby_vessels_after"] == 0


def test_coverage_gap_intel_event_is_neutral_context(detector) -> None:
    ev = ais_mod.AISAnomalyEvent(
        event_id="x", timestamp_utc="2026-09-02T00:00:00Z", anomaly_type="coverage_gap",
        mmsi="ghost", vessel_name="GHOST", position={"lat": 34.5, "lon": 18.0},
        confidence=0.4, evidence={"nearby_vessels_before": 4, "nearby_vessels_after": 0},
    )
    detector._emit(ev)
    assert detector._added
    ie = detector._added[0]
    assert ie.metadata["anomaly_type"] == "coverage_gap"
    assert ie.metadata["report_kind"] == "coverage_outage"
    assert ie.metadata["maritime_domain"] == "safety"
    assert ie.metadata["is_distress"] is False
    assert ie.severity == "low"
    assert ie.linked_mmsi is None


def test_feed_wide_outage_collapses_to_one_event_per_cell(detector) -> None:
    for mmsi in ("a", "b", "c"):
        detector._emit(ais_mod.AISAnomalyEvent(
            event_id=mmsi, timestamp_utc="2026-09-02T00:00:00Z", anomaly_type="coverage_gap",
            mmsi=mmsi, position={"lat": 34.4, "lon": 18.2}, confidence=0.4, evidence={},
        ))
    assert len(detector._added) == 1


def test_position_hook_adapter_forwards_to_process_position(detector, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(detector, "process_position", lambda *a: seen.append(a))
    detector._on_feed_position("333", "NAME", 35.0, 14.0, 8.0, 0)
    assert seen == [("333", "NAME", 35.0, 14.0, 8.0, "")]


def test_impossible_speed_near_coast_stays_internal(monkeypatch, detector) -> None:
    from datetime import datetime, timedelta, timezone

    from core.mda.reference import reference
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 3.0)
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 5.0))
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    base = datetime.now(timezone.utc) - timedelta(minutes=3)
    detector.process_position(
        "247012346", "Y", 35.0, 14.0, 10.0, "", observed_at=base
    )
    detector._added.clear()
    detector.process_position(
        "247012346", "Y", 36.0, 15.0, 300.0, "",
        observed_at=base + timedelta(minutes=1),
    )
    detector.process_position(
        "247012346", "Y", 36.001, 15.001, 10.0, "",
        observed_at=base + timedelta(minutes=2),
    )
    event = detector._added[-1]
    assert event.metadata["publication_status"] == "internal"
    assert event.metadata["offshore_anomaly_qualified"] is False

def test_out_of_order_source_fix_never_emits_impossible_speed(detector):
    from datetime import datetime, timedelta, timezone

    newer = datetime.now(timezone.utc)
    detector.process_position(
        "247999001", "PASSENGER", 40.0, 10.0, 20.0, "",
        observed_at=newer,
    )
    detector._added.clear()

    detector.process_position(
        "247999001", "PASSENGER", 42.0, 14.0, 20.0, "",
        observed_at=newer - timedelta(minutes=10),
    )

    assert detector._added == []
    assert detector._last_seen["247999001"]["lat"] == 40.0


def test_single_frame_coordinate_glitch_is_rejected(detector):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 19, 19, 10, tzinfo=timezone.utc)
    detector.process_position(
        "240760100", "ONCE MORE", 37.9327, 23.6823, 0.0, "",
        observed_at=base,
    )
    detector._added.clear()
    detector.process_position(
        "240760100", "ONCE MORE", 37.9327, 2.5823, 0.0, "",
        observed_at=base + timedelta(minutes=10),
    )
    assert detector._added == []
    assert "240760100" in detector._pending_jump

    detector.process_position(
        "240760100", "ONCE MORE", 37.93265, 23.6823, 0.0, "",
        observed_at=base + timedelta(minutes=16),
    )

    assert not any(
        event.metadata.get("anomaly_type") == "impossible_speed"
        for event in detector._added
    )
    assert "240760100" not in detector._pending_jump
    assert detector._last_seen["240760100"]["lon"] == pytest.approx(23.6823)


def test_sustained_position_jump_is_confirmed(detector):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    detector.process_position("247999003", "VESSEL", 35.0, 14.0, 10.0, "", observed_at=base)
    detector._added.clear()
    detector.process_position(
        "247999003", "VESSEL", 40.0, 20.0, 10.0, "",
        observed_at=base + timedelta(minutes=5),
    )
    assert detector._added == []
    detector.process_position(
        "247999003", "VESSEL", 40.01, 20.01, 10.0, "",
        observed_at=base + timedelta(minutes=10),
    )
    events = [
        event for event in detector._added
        if event.metadata.get("anomaly_type") == "impossible_speed"
    ]
    assert len(events) == 1
    assert events[0].metadata["anomaly_evidence"]["confirmed_by_followup"] is True


def test_impossible_speed_uses_source_time_not_receipt_time(detector, monkeypatch):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ais_mod.time, "time", lambda: 1_800_000_000.0)
    detector.process_position(
        "247999002", "VESSEL", 35.0, 14.0, 10.0, "",
        observed_at=base,
    )
    detector.process_position(
        "247999002", "VESSEL", 35.1, 14.0, 10.0, "",
        observed_at=base + timedelta(hours=1),
    )

    # ~6nm over one source-timed hour is plausible even if both messages are
    # processed in the same local second.
    assert not any(
        event.metadata.get("anomaly_type") == "impossible_speed"
        for event in detector._added
    )
