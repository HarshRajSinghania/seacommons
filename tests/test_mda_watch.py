# SPDX-License-Identifier: AGPL-3.0-or-later
"""MDA periodic scans: rendezvous / STS, infra loitering, gap, identity."""
from __future__ import annotations

import os
import time

os.environ["SEACOMMONS_TRACK_STORE_SYNC"] = "1"

from datetime import datetime, timezone

import pytest
from core.intel.store import intel_store
from core.mda.watch import MdaWatch
from core.vessels.track_store import track_store


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


def _feed(mmsi, lat, lon, sog=0.5, secs_ago=0.0):
    ts = datetime.now(timezone.utc)
    track_store.on_position(mmsi, mmsi, lat, lon, sog=sog, nav_status=0, received_at=ts)
    # force the throttle clock so successive calls in a test all land
    track_store._last_write_epoch[mmsi] = 0.0


def _alerts(t):
    return [e for e in intel_store.events(limit=100) if e.type == t]


def test_rendezvous_emits_after_sustained(monkeypatch):
    w = MdaWatch()
    # two near-stationary vessels ~300 m apart, offshore (mid-Ionian)
    _feed("111000001", 37.00, 18.00, sog=0.3)
    _feed("111000002", 37.0025, 18.0005, sog=0.4)
    # first scan: pair seen, not yet sustained
    assert w.scan_rendezvous() == 0
    # backdate the pair's first_seen past the 30-min threshold
    key = tuple(sorted(("111000001", "111000002")))
    w._pairs[key]["first_seen"] = time.time() - 40 * 60
    _feed("111000001", 37.00, 18.00, sog=0.3)
    _feed("111000002", 37.0025, 18.0005, sog=0.4)
    assert w.scan_rendezvous() == 1
    ev = _alerts("ais_rendezvous")
    assert len(ev) == 1
    # docs/fixes.md M0.3: a raw rendezvous is not a sanctions event by
    # itself -- was "sanctions" unconditionally.
    assert ev[0].metadata["maritime_domain"] == "grey_zone"
    assert ev[0].metadata["service"] == "maritime"
    assert ev[0].metadata["lane"] == "intelligence"
    assert ev[0].metadata["observation_type"] == "rendezvous"
    assert ev[0].metadata["publication_status"] == "internal"

    from core.intel.service_taxonomy import classify_service

    result = classify_service(ev[0])
    assert (result.service, result.lane, result.publishable) == ("maritime", "intelligence", False)


def test_rendezvous_ignored_inside_a_port():
    w = MdaWatch()
    _feed("111000003", 37.94, 23.60, sog=0.2)   # Piraeus
    _feed("111000004", 37.941, 23.601, sog=0.2)
    key = tuple(sorted(("111000003", "111000004")))
    w._pairs[key] = {"first_seen": time.time() - 3600, "count": 5}
    assert w.scan_rendezvous() == 0


def test_infra_loiter_near_pipeline():
    from datetime import timedelta
    w = MdaWatch()
    # on the Greenstream corridor, near-stationary, 8 fixes over > 90 min
    for i in range(8):
        track_store.on_position("111000005", "SLOWSHIP", 35.80 + i * 0.0005, 14.10, sog=0.8,
                                nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000005"] = 0.0
    from core.db.models import VesselTrackDB
    from core.db.session import session_scope
    with session_scope() as db:
        rows = db.query(VesselTrackDB).filter(VesselTrackDB.mmsi == "111000005").order_by(VesselTrackDB.ts).all()
        for k, r in enumerate(rows):
            r.ts = datetime.now(timezone.utc) - timedelta(minutes=130 - k * 15)
    assert w.scan_infra_loiter() == 1
    ev = _alerts("ais_anomaly")
    assert ev and ev[0].metadata["maritime_domain"] == "grey_zone"
    assert ev[0].metadata["infrastructure"]["kind"] == "pipeline"
    assert "not evidence of interference" in ev[0].metadata["detection_reason"]
    assert ev[0].url.endswith("mmsi:111000005")


def _loiter_in_malta_sts(mmsi: str, name: str) -> None:
    """8 near-stationary fixes over >90 min inside the bundled Malta
    bunkering/STS anchorage polygon (35.7-35.98N, 14.35-14.75E)."""
    from datetime import timedelta

    for i in range(8):
        track_store.on_position(mmsi, name, 35.85 + i * 0.0005, 14.55, sog=0.8,
                                nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch[mmsi] = 0.0
    from core.db.models import VesselTrackDB
    from core.db.session import session_scope
    with session_scope() as db:
        rows = db.query(VesselTrackDB).filter(VesselTrackDB.mmsi == mmsi).order_by(VesselTrackDB.ts).all()
        for k, r in enumerate(rows):
            r.ts = datetime.now(timezone.utc) - timedelta(minutes=130 - k * 15)


def test_infra_loiter_ignores_ordinary_vessel_in_sts_zone():
    """Idling in a bunkering/STS anchorage is what the zone is for -- not
    itself an anomaly for a vessel with no sanctions match."""
    from core.vessels.registry import registry

    w = MdaWatch()
    registry.upsert("111000020", ship_type=80, ship_name="ORDINARY TANKER")
    _loiter_in_malta_sts("111000020", "ORDINARY TANKER")
    assert w.scan_infra_loiter() == 0
    assert not _alerts("ais_anomaly")


def test_infra_loiter_flags_sanctioned_vessel_in_sts_zone():
    """A confirmed sanctions match idling at a known bunkering/STS hub is a
    classic evasion pattern -- surfaced even without a paired rendezvous
    vessel for scan_rendezvous to catch."""
    from core.db.models import SanctionedVesselDB
    from core.db.session import engine, session_scope
    from core.vessels.registry import registry

    SanctionedVesselDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.add(SanctionedVesselDB(source_list="OFAC_SDN", name="SHADOW TANKER",
                                  name_upper="SHADOW TANKER", imo=None, mmsi="111000021",
                                  program="RUSSIA-EO14024"))

    w = MdaWatch()
    registry.upsert("111000021", ship_type=80, ship_name="SHADOW TANKER")
    _loiter_in_malta_sts("111000021", "SHADOW TANKER")
    assert w.scan_infra_loiter() == 1
    ev = _alerts("ais_anomaly")
    assert ev[0].metadata["anomaly_type"] == "sanctions_bunkering_loiter"
    assert ev[0].metadata["maritime_domain"] == "sanctions"
    assert ev[0].metadata["sanctions_matched"] is True
    assert ev[0].metadata["infrastructure"]["kind"] == "sts_zone"


def test_gap_scan_flags_silent_vessel(monkeypatch):
    """No corroborating neighbour data either way (an isolated vessel with
    nothing else in range) must not be treated as proof of a coverage
    outage -- docs/fixes.md M14.1 keeps this pre-wiring behaviour."""
    w = MdaWatch()
    _feed("111000006", 34.0, 20.0, sog=12.0)
    track_store._last["111000006"].ts = time.time() - 5400   # 90 min silent
    assert w.scan_gaps() == 1
    assert _alerts("ais_anomaly")[0].metadata["anomaly_type"] == "gap"


def _witness(mmsi: str, lat: float, lon: float, minutes_ago: float) -> None:
    """A nearby vessel's single position report, `minutes_ago` in the past --
    the corroborating-neighbour data core.mda.gap_reason needs to tell a
    vessel-specific gap from a shared reception outage."""
    from datetime import timedelta

    track_store.on_position(
        mmsi, mmsi, lat, lon, sog=8.0, nav_status=0,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    track_store._last_write_epoch[mmsi] = 0.0


@pytest.mark.parametrize("ship_type", [37, 60, 30, 52])
def test_gap_scan_flags_isolated_gap_independent_of_vessel_class(ship_type):
    """docs/fixes.md M14.1 exit gate: an isolated gap -- this vessel silent
    while its neighbours keep reporting normally through the same window --
    remains detectable regardless of ship type. Vessel-class hard exclusions
    (pleasure/passenger/fishing/tug) are removed; class is context only."""
    from core.vessels.registry import registry

    mmsi = f"11100003{ship_type}"
    registry.upsert(mmsi, ship_type=ship_type, ship_name="ANY CLASS")
    _feed(mmsi, 37.00, 18.00, sog=8.0)
    track_store._last[mmsi].ts = time.time() - 5400   # 90 min silent

    # three neighbours reporting steadily both before AND through the gap --
    # healthy local coverage, so the silence is this vessel's own.
    for k in range(3):
        w_mmsi = f"11100004{k}"
        _witness(w_mmsi, 37.01, 18.01, minutes_ago=100)   # before the gap
        _witness(w_mmsi, 37.01, 18.01, minutes_ago=40)    # during the gap

    w = MdaWatch()
    assert w.scan_gaps() == 1
    ev = _alerts("ais_anomaly")[0]
    assert ev.metadata["anomaly_type"] == "gap"
    assert ev.metadata["vessel_type_context"] == ship_type
    assert ev.metadata["gap_reason"]["hypothesis"] == "vessel_gap"


def test_gap_scan_suppresses_common_port_wide_outage():
    """docs/fixes.md M14.1 exit gate: neighbouring vessels also went silent
    through the same window as this one -- a shared reception outage, not
    an intentional-dark hypothesis on this vessel."""
    mmsi = "111000040"
    _feed(mmsi, 37.00, 18.00, sog=8.0)
    track_store._last[mmsi].ts = time.time() - 5400   # 90 min silent

    # three neighbours reporting before the gap, then ALSO silent for its
    # duration -- the outage is the local reception environment, not this
    # vessel going dark.
    for k in range(3):
        _witness(f"11100005{k}", 37.01, 18.01, minutes_ago=100)

    w = MdaWatch()
    assert w.scan_gaps() == 0
    assert not _alerts("ais_anomaly")



def test_gap_scan_does_not_cross_cue_young_gap(monkeypatch):
    from core.mda import darkship_cue
    calls = []
    monkeypatch.setattr(darkship_cue, "build", lambda **kwargs: calls.append(kwargs) or {})
    mmsi = "111000061"
    _feed(mmsi, 37.00, 18.00, sog=8.0)
    track_store._last[mmsi].ts = time.time() - 90 * 60
    for k in range(6):
        _witness(f"11100016{k}", 37.01, 18.01, minutes_ago=100)
        _witness(f"11100016{k}", 37.01, 18.01, minutes_ago=40)
    assert MdaWatch().scan_gaps() == 1
    assert calls == []


def test_gap_scan_cross_cues_only_investigation_window(monkeypatch):
    from core.mda import darkship_cue
    calls = []
    monkeypatch.setattr(darkship_cue, "build", lambda **kwargs: calls.append(kwargs) or {"association_status": "no_detection"})
    mmsi = "111000062"
    _feed(mmsi, 37.00, 18.00, sog=8.0)
    track_store._last[mmsi].ts = time.time() - 5 * 3600
    for k in range(6):
        _witness(f"11100017{k}", 37.01, 18.01, minutes_ago=310)
        _witness(f"11100017{k}", 37.01, 18.01, minutes_ago=120)
    assert MdaWatch().scan_gaps() == 1
    assert len(calls) == 1

def test_spoofing_circular_ignores_tug_working_the_breakwater():
    import math

    from core.vessels.registry import registry

    w = MdaWatch()
    registry.upsert("111000024", ship_type=52, ship_name="GENOA TUG 3")
    for k in range(14):
        ang = k / 14 * 2 * math.pi
        dlat = 200 * math.sin(ang) / 111320
        dlon = 200 * math.cos(ang) / (111320 * math.cos(math.radians(44.40)))
        track_store.on_position("111000024", "GENOA TUG 3", 44.40 + dlat, 8.93 + dlon,
                                sog=1.0, nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000024"] = 0.0
    assert w.scan_spoofing() == 0
    assert not _alerts("ais_anomaly")


def test_spoofing_circular_track():
    import math
    w = MdaWatch()
    for k in range(14):
        ang = k / 14 * 2 * math.pi
        dlat = 200 * math.sin(ang) / 111320
        dlon = 200 * math.cos(ang) / (111320 * math.cos(math.radians(37)))
        track_store.on_position("111000010", "SPOOF", 37.0 + dlat, 15.0 + dlon,
                                sog=21.0, nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000010"] = 0.0
    assert w.scan_spoofing() == 1
    ev = _alerts("ais_anomaly")
    assert ev[0].metadata["anomaly_type"] == "circle_spoof"


def test_spoofing_circular_ignores_pleasure_craft_swinging_at_anchor():
    """The same ring signature a real spoofer draws is what a boat swinging
    on its anchor chain near a marina produces -- must not alert a
    non-sanctioned pleasure craft on this signature alone."""
    import math

    from core.vessels.registry import registry

    w = MdaWatch()
    registry.upsert("111000012", ship_type=36, ship_name="WINDSWEPT")
    for k in range(14):
        ang = k / 14 * 2 * math.pi
        dlat = 200 * math.sin(ang) / 111320
        dlon = 200 * math.cos(ang) / (111320 * math.cos(math.radians(37)))
        track_store.on_position("111000012", "WINDSWEPT", 37.0 + dlat, 15.0 + dlon,
                                sog=0.4, nav_status=1, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000012"] = 0.0
    assert w.scan_spoofing() == 0
    assert not _alerts("ais_anomaly")


def test_spoofing_circular_ignores_passenger_ferry_at_its_own_terminal():
    """A scheduled ferry's repeated berthing manoeuvre draws the same ring
    signature a spoofed track would -- passenger vessels are withheld from
    Live entirely for now (see the gap tests for the non-port case too)."""
    import math

    from core.vessels.registry import registry

    w = MdaWatch()
    registry.upsert("111000017", ship_type=60, ship_name="BLUE HORIZON")
    for k in range(14):
        ang = k / 14 * 2 * math.pi
        dlat = 200 * math.sin(ang) / 111320
        dlon = 200 * math.cos(ang) / (111320 * math.cos(math.radians(37.94)))
        track_store.on_position("111000017", "BLUE HORIZON", 37.94 + dlat, 23.60 + dlon,
                                sog=1.0, nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000017"] = 0.0
    assert w.scan_spoofing() == 0
    assert not _alerts("ais_anomaly")


def test_spoofing_teleport():
    from datetime import timedelta
    w = MdaWatch()
    base = datetime.now(timezone.utc) - timedelta(minutes=15)
    for i in range(6):
        track_store.on_position("111000011", "JUMP", 35.0, 15.0, sog=10.0,
                                nav_status=0, received_at=base + timedelta(seconds=i * 20))
        track_store._last_write_epoch["111000011"] = 0.0
    # one impossible jump 40 s after the last fix
    track_store.on_position("111000011", "JUMP", 39.0, 20.0, sog=10.0,
                            nav_status=0, received_at=base + timedelta(seconds=6 * 20 + 40))
    track_store._last_write_epoch["111000011"] = 0.0
    assert w.scan_spoofing() == 1
    assert _alerts("ais_anomaly")[0].metadata["anomaly_type"] == "position_jump"


def test_spoofing_ignores_zero_longitude_sentinel_jump():
    from datetime import timedelta
    w = MdaWatch()
    base = datetime.now(timezone.utc) - timedelta(minutes=15)
    points = [
        (43.5885, 7.1236), (43.5885, 7.1237), (43.5885, 0.0),
        (43.5885, 7.1236), (43.5885, 7.1237), (43.5885, 7.1236),
    ]
    for i, (lat, lon) in enumerate(points):
        track_store.on_position("111000119", "ZERO-LON", lat, lon, sog=0.0,
                                nav_status=0, received_at=base + timedelta(seconds=i * 60))
        track_store._last_write_epoch["111000119"] = 0.0
    assert w.scan_spoofing() == 0
    assert not [e for e in _alerts("ais_anomaly") if e.linked_mmsi == "111000119"]


def test_mmsi_duplicate():
    w = MdaWatch()
    # same MMSI, two clusters 300 km apart, many fixes
    for i in range(4):
        track_store.on_position("111000007", "CLONE", 35.0, 15.0, sog=8.0,
                                nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000007"] = 0.0
        track_store.on_position("111000007", "CLONE", 40.0, 20.0, sog=8.0,
                                nav_status=0, received_at=datetime.now(timezone.utc))
        track_store._last_write_epoch["111000007"] = 0.0
    assert w.scan_mmsi_duplicate() == 1
    assert _alerts("vessel_identity")[0].metadata["anomaly_type"] == "mmsi_duplicate"


def _degraded_provider_gap_fixture(monkeypatch, mode: str, mmsi: str):
    from core.config import config
    from core.vessels import ais_coverage
    from core.vessels.ais_coverage import CoverageAssessment

    _feed(mmsi, 37.0, 18.0, sog=8.0)
    track_store._last[mmsi].ts = time.time() - 5400
    for k in range(3):
        witness = f"{mmsi[:-1]}{k}"
        _witness(witness, 37.01, 18.01, minutes_ago=100)
        _witness(witness, 37.01, 18.01, minutes_ago=40)
    monkeypatch.setattr(config, "AIS_FUSION_ENABLED", False)
    monkeypatch.setattr(config, "AIS_FUSION_MODE", mode)
    monkeypatch.setattr(
        ais_coverage.coverage_state, "assess",
        lambda **_kwargs: CoverageAssessment(
            status="provider_degraded", active_upstreams=frozenset({"volunteer"}),
            degraded_upstreams=frozenset({"aisstream"}), confidence=0.25,
            reason_codes=("UPSTREAM_DEGRADED",), gap_eligible=False,
        ),
    )


def test_shadow_mode_never_changes_gap_decisions(monkeypatch):
    mmsi = "111000098"
    _degraded_provider_gap_fixture(monkeypatch, "shadow", mmsi)
    assert MdaWatch().scan_gaps() == 1
    assert [e for e in _alerts("ais_anomaly") if e.linked_mmsi == mmsi]


def test_fused_mode_suppresses_gap_while_provider_is_degraded(monkeypatch):
    mmsi = "111000097"
    _degraded_provider_gap_fixture(monkeypatch, "fused", mmsi)
    assert MdaWatch().scan_gaps() == 0
    assert not [e for e in _alerts("ais_anomaly") if e.linked_mmsi == mmsi]


def test_teleport_pattern_separates_transient_outlier_from_sustained_relocation():
    from datetime import timedelta
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    def point(lat, lon, seconds):
        return {"lat": lat, "lon": lon, "ts": base + timedelta(seconds=seconds), "sog": 10.0}

    transient = [
        point(35.0, 15.0, 0), point(39.0, 20.0, 60),
        point(35.01, 15.01, 120), point(35.02, 15.02, 180),
    ]
    sustained = [
        point(35.0, 15.0, 0), point(39.0, 20.0, 60),
        point(39.01, 20.01, 120), point(39.02, 20.02, 180),
    ]
    assert MdaWatch._teleport_pattern(transient) == "transient_outlier"
    assert MdaWatch._teleport_pattern(sustained) == "sustained_relocation"


def test_coincident_teleport_peers_detect_same_area_and_time():
    from datetime import timedelta
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    def p(lat, lon, sec):
        return {"lat": lat, "lon": lon, "ts": base + timedelta(seconds=sec), "sog": 10.0}
    tracks = {
        "111000101": [p(30.0, 30.0, 0), p(31.0, 32.0, 60), p(31.01, 32.01, 120)],
        "111000102": [p(30.5, 30.5, 5), p(31.05, 32.05, 65), p(31.06, 32.06, 125)],
        "111000103": [p(35.0, 15.0, 0), p(39.0, 20.0, 60)],
    }
    assert MdaWatch._coincident_teleport_peers(tracks, "111000101") == ("111000102",)



def test_sanctioned_port_call_requires_strong_identity_and_qualified_stay(monkeypatch):
    from core.api.routes import mda as mda_routes
    from core.mda import identity
    from core.mda.reference import reference
    from core.vessels.registry import registry

    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: "Augusta")
    mmsi = "244123456"
    registry._cache[mmsi] = {"imo": "1234567", "ship_name": "LISTED SHIP", "flag": "NL"}
    rows = [
        {"mmsi": mmsi, "lat": 37.0, "lon": 15.0, "ts": "2026-09-18T05:00:00+00:00"},
        {"mmsi": mmsi, "lat": 37.0, "lon": 15.0, "ts": "2026-09-18T06:00:00+00:00"},
    ]
    monkeypatch.setattr(track_store, "positions_between", lambda *a, **k: rows)
    monkeypatch.setattr(
        mda_routes, "_derive_recent_port_calls",
        lambda track, limit=2: [{
            "port": "Augusta", "arrived_at": rows[0]["ts"],
            "last_seen_at": rows[-1]["ts"], "ais_fixes": 2,
            "evidence_level": "derived", "method": "ais_port_approach",
        }],
    )

    monkeypatch.setattr(identity, "screen", lambda **kwargs: {
        "risk_flags": ["sanctions_hit"],
        "sanctions": [{
            "list": "OFAC_SDN", "program": "TEST",
            "matched_on": ["imo"], "imo": "1234567",
        }],
    })

    w = MdaWatch()
    assert w.scan_sanctioned_port_calls() == 1
    events = _alerts("vessel_identity")
    assert len(events) == 1
    event = events[0]
    assert event.metadata["anomaly_type"] == "sanctioned_port_call"
    assert event.metadata["publication_status"] == "published"
    assert event.metadata["analysis_state"] == "evidence"
    assert event.metadata["port_call"]["port"] == "Augusta"
    assert "evasion" in event.text


def test_sanctioned_port_call_rejects_name_only_match(monkeypatch):
    from core.api.routes import mda as mda_routes
    from core.mda import identity
    from core.mda.reference import reference
    from core.vessels.registry import registry

    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: "Augusta")
    mmsi = "244654321"
    registry._cache[mmsi] = {"ship_name": "SAME NAME"}
    rows = [{"mmsi": mmsi, "lat": 37.0, "lon": 15.0, "ts": "2026-09-18T05:00:00+00:00"}]
    monkeypatch.setattr(track_store, "positions_between", lambda *a, **k: rows)
    monkeypatch.setattr(
        mda_routes, "_derive_recent_port_calls",
        lambda track, limit=2: [{
            "port": "Augusta", "arrived_at": rows[0]["ts"],
            "method": "ais_port_approach",
        }],
    )
    monkeypatch.setattr(identity, "screen", lambda **kwargs: {
        "risk_flags": ["sanctions_hit"],
        "sanctions": [{"list": "OFAC_SDN", "matched_on": ["name"]}],
    })

    w = MdaWatch()
    assert w.scan_sanctioned_port_calls() == 0
    assert not _alerts("vessel_identity")


def test_retrospective_darkship_refresh_reuses_canonical_hypothesis_scan(monkeypatch):
    from datetime import timedelta

    from core.intel.store import IntelEvent
    from core.mda import darkship_cue

    now = datetime.now(timezone.utc)
    candidate = {
        "event_id": "aisgap:111000099",
        "mmsi": "111000099",
        "lat": 35.0,
        "lon": 18.0,
        "speed_kn": 8.0,
        "course_deg": 90.0,
        "gap_start": now - timedelta(days=4),
        "search_hours": 6.0,
        "last_checked": None,
        "offshore_context": {"offshore": True},
    }
    cue = {
        "association_status": "unmatched_candidate",
        "gfw_unmatched_in_area": [{
            "lat": 35.1, "lon": 18.4,
            "timestamp": (candidate["gap_start"] + timedelta(hours=3)).isoformat(),
            "matched": False,
        }],
        "search_window_hours": 6.0,
    }
    updated = IntelEvent(
        id=candidate["event_id"],
        timestamp_utc=(candidate["gap_start"] + timedelta(hours=6)).isoformat(),
        type="ais_anomaly",
        severity="high",
        lat=35.0,
        lon=18.0,
        title="AIS gap",
        source="mda",
        linked_mmsi=candidate["mmsi"],
        metadata={"anomaly_type": "long_gap", "darkship_cue": cue},
    )
    scan_calls = []

    monkeypatch.setattr(
        MdaWatch, "_retrospective_darkship_candidates",
        staticmethod(lambda **kwargs: [candidate]),
    )
    monkeypatch.setattr(darkship_cue, "build", lambda **kwargs: cue)
    monkeypatch.setattr(
        MdaWatch, "_persist_darkship_cue_refresh",
        staticmethod(lambda candidate, cue, checked_at: updated),
    )
    monkeypatch.setattr(
        MdaWatch, "scan_hypotheses",
        lambda self, *, extra_events=None: scan_calls.append(extra_events) or 1,
    )

    report = MdaWatch().refresh_darkship_cues(limit=1)

    assert report["scanned"] == 1
    assert report["refreshed"] == 1
    assert report["with_unmatched_sar"] == 1
    assert report["hypotheses_evaluated"] == 1
    assert scan_calls == [[updated]]
