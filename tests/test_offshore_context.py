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
    assert result["qualified"] is True
    assert "LOCAL_AIS_COVERAGE_HEALTHY" in result["reason_codes"]
    assert "PROLONGED_OFFSHORE_GAP" in result["reason_codes"]


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
    event = IntelEvent(
        id="offshore-gap:test",
        type="ais_anomaly",
        severity="high",
        lat=34.5,
        lon=17.0,
        title="AIS gap — TEST",
        source="mda",
        linked_mmsi="211123456",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        metadata={
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
        },
    )
    feature = _public_intel_feature(event, allowed_domains=frozenset({"grey_zone"}))
    assert feature is not None
    props = feature["properties"]
    assert props["live_role"] == "maritime_evidence"
    assert props["analysis_state"] == "evidence_candidate"
    assert props["offshore_context"]["offshore"] is True
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


def test_single_impossible_speed_outlier_stays_play_only_until_repeated() -> None:
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
    assert is_useful_public_case_feature(one) is False
    assert is_useful_public_case_feature(repeated) is True


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
