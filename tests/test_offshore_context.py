from datetime import datetime, timezone

from core.intel.store import IntelEvent
from core.live.projection import _public_intel_feature, is_useful_public_case_feature
from core.mda.offshore_context import build_offshore_context, qualify_offshore_anomaly
from core.mda.reference import reference


def test_bundled_coastline_distinguishes_land_from_open_water():
    assert reference.distance_from_coast_km(37.5, 14.0) == 0.0  # Sicily interior, low-res context
    distance = reference.distance_from_coast_km(35.0, 15.0)
    assert distance is not None and distance > 20.0


def test_offshore_gap_needs_healthy_coverage_and_strong_context(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 120.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 90.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(35.0, 15.0)
    result = qualify_offshore_anomaly("gap", {
        "silent_seconds": 5 * 3600,
        "jamming_score": 0.0,
        "gap_reason": {
            "hypothesis": "vessel_gap",
            "nearby_vessels_reporting_before": 6,
            "nearby_vessels_reporting_after": 7,
            "confidence": 0.72,
        },
        "behaviour_context": {"reason_codes": []},
    }, context)
    assert context["offshore"] is True
    assert result["qualified"] is False
    assert "LOCAL_AIS_COVERAGE_HEALTHY" in result["reason_codes"]
    assert "PROLONGED_OFFSHORE_GAP" in result["reason_codes"]
    assert "OPEN_GAP_COVERAGE_NOT_TRACK_CONTINUOUS" in result["reason_codes"]


def test_reappeared_offshore_gap_without_strong_reception_stays_unqualified(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 120.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 90.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(35.0, 15.0)
    result = qualify_offshore_anomaly("gap", {
        "silent_seconds": 5 * 3600,
        "gap_reappearance_confirmed": True,
        "jamming_score": 0.0,
        "gap_reason": {
            "hypothesis": "vessel_gap",
            "nearby_vessels_reporting_before": 6,
            "nearby_vessels_reporting_after": 7,
            "confidence": 0.72,
        },
        "behaviour_context": {"reason_codes": []},
    }, context)
    assert result["qualified"] is False
    assert "GAP_REAPPEARANCE_CONFIRMED" in result["reason_codes"]


def test_same_gap_near_coast_stays_raw_anomaly(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 8.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 4.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(35.0, 15.0)
    result = qualify_offshore_anomaly("gap", {
        "silent_seconds": 8 * 3600,
        "gap_reason": {
            "hypothesis": "vessel_gap",
            "nearby_vessels_reporting_before": 10,
            "nearby_vessels_reporting_after": 10,
            "confidence": 0.8,
        },
    }, context)
    assert context["offshore"] is False
    assert result["qualified"] is False


def test_narrow_open_sea_gap_needs_track_corridor_continuity(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 95.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 24.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(36.0, 15.0)
    assert context["open_sea"] is True
    assert context["deep_offshore"] is False

    metadata = {
        "silent_seconds": 7 * 3600,
        "pre_gap_speed_kn": 12.0,
        "jamming_score": 0.0,
        "gap_reason": {
            "hypothesis": "vessel_gap",
            "nearby_vessels_reporting_before": 18,
            "nearby_vessels_reporting_after": 22,
            "confidence": 0.8,
        },
        "behaviour_context": {"reason_codes": []},
    }
    without_corridor = qualify_offshore_anomaly("long_gap", metadata, context)
    assert without_corridor["qualified"] is False

    with_corridor = qualify_offshore_anomaly(
        "long_gap",
        {
            **metadata,
            "track_coverage_continuity": {
                "continuous": True,
                "checkpoint_count": 4,
                "covered_checkpoints": 4,
                "covered_fraction": 1.0,
                "median_nearby_vessels": 8,
            },
            "reception_expectation": {
                "support_level": "strong",
                "reason_codes": [
                    "DENSE_PRE_GAP_REPORTING_HISTORY",
                    "NEIGHBOUR_TRAFFIC_CONTINUED",
                    "TRACK_CORRIDOR_COVERAGE_PRESENT",
                    "NO_MATERIAL_JAMMING_CONTEXT",
                    "MANY_EXPECTED_REPORTS_MISSING",
                ],
            },
        },
        context,
    )
    assert with_corridor["qualified"] is True
    assert "OPEN_SEA_CONTEXT" in with_corridor["reason_codes"]
    assert "TRACK_CORRIDOR_COVERAGE_PRESENT" in with_corridor["reason_codes"]


def test_sustained_offshore_tanker_rendezvous_is_evidence_candidate(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 180.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 110.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(34.0, 17.0)
    result = qualify_offshore_anomaly("ais_rendezvous", {
        "duration_min": 95,
        "tanker": True,
        "dark": False,
    }, context)
    assert result["qualified"] is True
    assert result["stage"] == "evidence_candidate"


def test_published_qualified_offshore_gap_reaches_live_as_evidence_not_case():
    base_metadata = {
        "anomaly_type": "gap",
        "maritime_domain": "grey_zone",
        "publication_status": "published",
        "source_policy": "official_api",
        "verification_status": "ais_transponder",
        "analysis_state": "evidence_candidate",
        "offshore_anomaly_qualified": True,
        "offshore_reason_codes": ["OFFSHORE_CONTEXT", "LOCAL_AIS_COVERAGE_HEALTHY"],
        "offshore_rationale": "Offshore anomaly passed contextual gates; it is evidence for investigation, not proof of intent.",
        "offshore_context": {
            "offshore": True,
            "distance_from_coast_km": 95.0,
            "distance_from_port_km": 120.0,
            "nearest_port": "Test Port",
        },
    }

    weak_event = IntelEvent(
        id="offshore-gap:test:weak-reception",
        type="ais_anomaly",
        severity="high",
        lat=34.5,
        lon=17.0,
        title="AIS gap — TEST",
        source="mda",
        linked_mmsi="211123456",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        metadata=base_metadata,
    )
    weak_feature = _public_intel_feature(
        weak_event, allowed_domains=frozenset({"grey_zone"})
    )
    assert weak_feature is not None
    assert is_useful_public_case_feature(weak_feature) is False

    event = IntelEvent(
        id="offshore-gap:test:strong-reception",
        type="ais_anomaly",
        severity="high",
        lat=34.5,
        lon=17.0,
        title="AIS gap — TEST",
        source="mda",
        linked_mmsi="211123456",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        metadata={
            **base_metadata,
            "reception_expectation": {
                "support_level": "strong",
                "reason_codes": [
                    "DENSE_PRE_GAP_REPORTING_HISTORY",
                    "NEIGHBOUR_TRAFFIC_CONTINUED",
                    "TRACK_CORRIDOR_COVERAGE_PRESENT",
                    "NO_MATERIAL_JAMMING_CONTEXT",
                    "MANY_EXPECTED_REPORTS_MISSING",
                ],
            },
        },
    )
    feature = _public_intel_feature(event, allowed_domains=frozenset({"grey_zone"}))
    assert feature is not None
    props = feature["properties"]
    assert props["live_role"] == "maritime_evidence"
    assert props["analysis_state"] == "evidence_candidate"
    assert props["offshore_context"]["offshore"] is True
    assert props["reception_expectation"]["support_level"] == "strong"
    assert is_useful_public_case_feature(feature) is True


def test_offshore_gap_below_gap_reason_confidence_stays_anomaly(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 120.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 90.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(35.0, 15.0)
    result = qualify_offshore_anomaly("gap", {
        "silent_seconds": 5 * 3600,
        "gap_reason": {
            "hypothesis": "vessel_gap",
            "nearby_vessels_reporting_before": 10,
            "nearby_vessels_reporting_after": 10,
            "confidence": 0.69,
        },
    }, context)
    assert result["qualified"] is False


def test_zero_interval_position_jump_stays_anomaly(monkeypatch):
    monkeypatch.setattr(reference, "nearest_port_km", lambda lat, lon: ("Test Port", 120.0))
    monkeypatch.setattr(reference, "distance_from_coast_km", lambda lat, lon: 90.0)
    monkeypatch.setattr(reference, "in_port_or_anchorage", lambda lat, lon: None)
    monkeypatch.setattr(reference, "in_sts_zone", lambda lat, lon: None)
    monkeypatch.setattr(reference, "chokepoint_of", lambda lat, lon: None)
    context = build_offshore_context(35.0, 15.0)
    result = qualify_offshore_anomaly("impossible_speed", {
        "anomaly_confidence": 0.95,
        "anomaly_evidence": {"gap_s": 0.2, "computed_kts": 1700},
    }, context)
    assert result["qualified"] is False


def test_same_lineage_impossible_speed_stays_play_only_even_when_repeated() -> None:
    base = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [23.28, 37.70]},
        "properties": {
            "type": "ais_anomaly",
            "anomaly_type": "impossible_speed",
            "source": "ais",
            "visual_category": "spoofing",
            "verification_status": "single_source_observed",
            "analysis_state": "evidence_candidate",
            "offshore_anomaly_qualified": True,
        },
    }
    one = {
        **base,
        "properties": {**base["properties"], "evidence_count": 1},
    }
    repeated = {
        **base,
        "properties": {**base["properties"], "evidence_count": 2},
    }
    sustained = {
        **base,
        "properties": {
            **base["properties"],
            "evidence_count": 2,
            "teleport_pattern": "sustained_relocation",
        },
    }
    assert is_useful_public_case_feature(one) is False
    assert is_useful_public_case_feature(repeated) is False
    assert is_useful_public_case_feature(sustained) is True


def test_non_offshore_qualification_preserves_full_result_contract() -> None:
    result = qualify_offshore_anomaly(
        "ais_rendezvous",
        {"duration_min": 120, "dark": True},
        {"offshore": False},
    )
    assert result["qualified"] is False
    assert result["reason_codes"] == ["NOT_OFFSHORE"]
    assert result["stage"] == "anomaly"
    assert "not offshore" in result["rationale"].lower()


def test_scheduled_hsc_short_gap_is_suppressed_as_low_specificity() -> None:
    result = qualify_offshore_anomaly(
        "gap",
        {
            "vessel_type_context": 41,
            "scheduled_service": True,
            "silent_seconds": 90 * 60,
            "jamming_score": 0.0,
            "gap_reason": {
                "hypothesis": "vessel_gap",
                "confidence": 0.85,
                "nearby_vessels_reporting_before": 12,
                "nearby_vessels_reporting_after": 11,
            },
            "behaviour_context": {
                "status": "unusual",
                "reason_codes": ["ROUTE_DEVIATION"],
            },
        },
        {"offshore": True},
    )
    assert result["qualified"] is False
    assert result["stage"] == "anomaly"
    assert "PASSENGER_HSC_LOW_SPECIFICITY" in result["reason_codes"]
    assert "ROUTINE_SCHEDULED_TRAFFIC" in result["reason_codes"]


def test_passenger_ferry_baseline_consistent_gap_is_suppressed_with_reason() -> None:
    result = qualify_offshore_anomaly(
        "gap",
        {
            "ship_type": 60,
            "silent_seconds": 2 * 3600,
            "jamming_score": 0.0,
            "gap_reason": {
                "hypothesis": "vessel_gap",
                "confidence": 0.9,
                "nearby_vessels_reporting_before": 8,
                "nearby_vessels_reporting_after": 8,
            },
            "behaviour_context": {"status": "expected", "reason_codes": []},
        },
        {"offshore": True},
    )
    assert result["qualified"] is False
    assert "PASSENGER_HSC_LOW_SPECIFICITY" in result["reason_codes"]
    assert "BASELINE_ROUTE_CONSISTENT" in result["reason_codes"]


def test_passenger_class_does_not_suppress_high_specificity_position_anomaly() -> None:
    result = qualify_offshore_anomaly(
        "position_jump",
        {
            "vessel_type_context": 61,
            "anomaly_confidence": 0.95,
            "anomaly_evidence": {"gap_s": 90, "computed_kts": 420},
            "teleport_pattern": "sustained_relocation",
            "behaviour_context": {"status": "expected", "reason_codes": []},
        },
        {"offshore": True},
    )
    assert result["qualified"] is True
    assert "PASSENGER_HSC_LOW_SPECIFICITY" not in result["reason_codes"]


def test_prolonged_open_gap_with_only_origin_neighbor_coverage_stays_candidate() -> None:
    result = qualify_offshore_anomaly(
        "long_gap",
        {
            "vessel_type_context": 42,
            "scheduled_service": True,
            "silent_seconds": 6 * 3600,
            "jamming_score": 0.0,
            "gap_reason": {
                "hypothesis": "vessel_gap",
                "confidence": 0.9,
                "nearby_vessels_reporting_before": 9,
                "nearby_vessels_reporting_after": 10,
            },
            "behaviour_context": {"status": "expected", "reason_codes": []},
        },
        {"offshore": True, "ais_coverage_witnesses": []},
    )
    assert result["qualified"] is False
    assert "PROLONGED_OFFSHORE_GAP" in result["reason_codes"]
    assert "OPEN_GAP_COVERAGE_NOT_TRACK_CONTINUOUS" in result["reason_codes"]


def test_prolonged_gap_with_only_community_coverage_stays_unqualified() -> None:
    result = qualify_offshore_anomaly(
        "long_gap",
        {
            "silent_seconds": 6 * 3600,
            "jamming_score": 0.0,
            "gap_reason": {
                "hypothesis": "vessel_gap",
                "confidence": 0.9,
                "nearby_vessels_reporting_before": 0,
                "nearby_vessels_reporting_after": 0,
            },
            "behaviour_context": {"status": "insufficient_history", "reason_codes": []},
        },
        {
            "offshore": True,
            "ais_coverage_witnesses": [{"station_id": "3372", "coverage_role": "same_lineage_coverage_witness"}],
        },
    )
    assert result["qualified"] is False
    assert "COMMUNITY_AIS_COVERAGE_PRESENT" in result["reason_codes"]


def test_passenger_ferry_pair_does_not_turn_same_lineage_gap_into_sts_case() -> None:
    result = qualify_offshore_anomaly(
        "ais_rendezvous",
        {
            "vessel_type_contexts": [41, 60],
            "duration_min": 95,
            "dark": True,
            "tanker": False,
            "independent_source_count": 1,
            "contributing_independence_groups": ["ais_sensor_lineage"],
        },
        {"offshore": True},
    )
    assert result["qualified"] is False
    assert "PASSENGER_HSC_LOW_SPECIFICITY" in result["reason_codes"]


def test_passenger_impossible_speed_without_sustained_relocation_is_suppressed() -> None:
    result = qualify_offshore_anomaly(
        "impossible_speed",
        {
            "vessel_type_context": 69,
            "anomaly_confidence": 0.95,
            "anomaly_evidence": {"gap_s": 120, "computed_kts": 600},
            "behaviour_context": {"status": "expected", "reason_codes": []},
        },
        {"offshore": True},
    )
    assert result["qualified"] is False
    assert "PASSENGER_HSC_LOW_SPECIFICITY" in result["reason_codes"]
