# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from datetime import datetime, timezone

from core.mda.ais_coverage import parse_station_profile
from core.mda.offshore_context import qualify_offshore_anomaly
from core.vessels.ais_coverage import CoverageState, assess_coverage
from core.vessels.ais_provider import AISPositionObservation, AISProviderHealth

_NOW = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)


def test_single_provider_outage_blocks_vessel_specific_gap():
    c = assess_coverage(
        active_upstreams={"volunteer"}, degraded_upstreams={"aisstream"},
        nearby_traffic_seen=True,
    )
    assert c.status == "provider_degraded"
    assert "UPSTREAM_DEGRADED" in c.reason_codes
    assert c.gap_eligible is False


def test_healthy_upstreams_and_nearby_traffic_support_coverage_present():
    c = assess_coverage(
        active_upstreams={"aisstream", "volunteer"}, degraded_upstreams=set(),
        nearby_traffic_seen=True,
    )
    assert c.status == "coverage_present"
    assert c.gap_eligible is True


def test_aiscast_relay_of_aisstream_does_not_create_alternate_upstream():
    state = CoverageState()
    state.note_observation(AISPositionObservation(
        mmsi="247123456", ship_name="", lat=35.0, lon=15.0,
        sog=8.0, cog=90.0, heading=90.0, nav_status=0,
        observed_at=_NOW, received_at=_NOW,
        provider="aiscast", upstream_source="aisstream",
    ))
    state.update_health(AISProviderHealth(
        provider="aisstream", connected=False, last_message_at=None,
        messages_received=0, error="timeout",
    ))
    assessment = state.assess(nearby_traffic_seen=True, now=_NOW)
    assert assessment.active_upstreams == frozenset({"aisstream"})
    assert assessment.status == "provider_degraded"
    assert assessment.gap_eligible is False


def test_no_upstream_information_is_coverage_unknown():
    c = assess_coverage(active_upstreams=set(), degraded_upstreams=set(), nearby_traffic_seen=False)
    assert c.status == "coverage_unknown"
    assert c.gap_eligible is False


def test_parse_public_aiscatcher_station_profile() -> None:
    raw = """
    <h1>Gozo</h1>
    <a href="/livemap?lat=36.070315&lon=14.235899&zoom=11">map</a>
    <span id="station-state" data-state="active">Active</span>
    <dd id="station-vessels">6</dd>
    <dd id="station-messages">808</dd>
    <dd id="station-last">2026-09-21 12:36:59 UTC</dd>
    <dd id="station-online">98%</dd>
    <div><dt>Furthest · 24 h</dt><dd>21.2 nmi</dd></div>
    """
    row = parse_station_profile("3372", raw)
    assert row.station_id == "3372"
    assert row.label == "Gozo"
    assert row.status == "active"
    assert row.lat == 36.070315
    assert row.lon == 14.235899
    assert row.last_received_at == datetime(2026, 9, 21, 12, 36, 59, tzinfo=timezone.utc).replace(tzinfo=None)
    assert row.online_24h == 0.98
    assert round(row.max_reception_km or 0, 1) == 39.3
    assert row.messages_1h == 808
    assert row.vessels_1h == 6


def test_coverage_witness_uses_closest_snapshot_in_time() -> None:
    from datetime import timedelta

    from core.db.models import AISCoverageSnapshotDB
    from core.db.session import engine, session_scope
    from core.mda.ais_coverage import coverage_witnesses

    AISCoverageSnapshotDB.__table__.create(bind=engine(), checkfirst=True)
    target = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    station_id = "test-temporal-witness"
    with session_scope() as db:
        db.query(AISCoverageSnapshotDB).filter_by(station_id=station_id).delete()
        for suffix, observed_at, radius in (
            ("near", target - timedelta(minutes=1), 100.0),
            ("later", target + timedelta(minutes=19), 1.0),
        ):
            db.add(AISCoverageSnapshotDB(
                snapshot_id=f"test:{station_id}:{suffix}",
                network="test",
                station_id=station_id,
                station_label="Temporal test",
                source_url="https://example.invalid/station",
                lat=35.9,
                lon=14.4,
                station_status="active",
                station_last_received_at=target,
                online_24h=1.0,
                max_reception_km=radius,
                messages_1h=1000,
                vessels_1h=20,
                observed_at=observed_at,
            ))
    try:
        witnesses = coverage_witnesses(35.95, 14.4, at=target.replace(tzinfo=timezone.utc))
        match = next(row for row in witnesses if row["station_id"] == station_id)
        assert match["observed_max_reception_km"] == 100.0
    finally:
        with session_scope() as db:
            db.query(AISCoverageSnapshotDB).filter_by(station_id=station_id).delete()


def test_community_station_is_coverage_context_not_corroboration() -> None:
    result = qualify_offshore_anomaly(
        "long_gap",
        {
            "silent_seconds": 7 * 3600,
            "jamming_score": 0.0,
            "gap_reason": None,
            "behaviour_context": {"reason_codes": ["INSUFFICIENT_HISTORY"]},
        },
        {
            "offshore": True,
            "ais_coverage_witnesses": [
                {
                    "network": "aiscatcher_community",
                    "station_id": "3372",
                    "coverage_role": "same_lineage_coverage_witness",
                }
            ],
        },
    )
    assert result["qualified"] is False
    assert "COMMUNITY_AIS_COVERAGE_PRESENT" in result["reason_codes"]
    assert "LOCAL_AIS_COVERAGE_HEALTHY" not in result["reason_codes"]


def test_community_station_can_replace_neighbour_count_for_valid_vessel_gap() -> None:
    result = qualify_offshore_anomaly(
        "long_gap",
        {
            "silent_seconds": 7 * 3600,
            "jamming_score": 0.0,
            "gap_reason": {
                "hypothesis": "vessel_gap",
                "confidence": 0.82,
                "coverage_ratio": 0.0,
                "nearby_vessels_reporting_before": 0,
                "nearby_vessels_reporting_after": 0,
            },
            "behaviour_context": {"reason_codes": ["INSUFFICIENT_HISTORY"]},
        },
        {
            "offshore": True,
            "ais_coverage_witnesses": [
                {
                    "network": "aiscatcher_community",
                    "station_id": "3372",
                    "coverage_role": "same_lineage_coverage_witness",
                }
            ],
        },
    )
    assert result["qualified"] is True
    assert "COMMUNITY_AIS_COVERAGE_PRESENT" in result["reason_codes"]
    assert "LOCAL_AIS_COVERAGE_HEALTHY" in result["reason_codes"]
    assert "PROLONGED_OFFSHORE_GAP" in result["reason_codes"]
