from __future__ import annotations

import importlib

import pytest
from core.intel.store import IntelEvent


def _eligibility():
    try:
        return importlib.import_module("core.intel.hypothesis_eligibility")
    except ModuleNotFoundError:
        pytest.fail("core.intel.hypothesis_eligibility is required")


def _event(event_id: str, anomaly_type: str, **metadata) -> IntelEvent:
    return IntelEvent(
        id=event_id,
        type="ais_anomaly",
        severity="medium",
        lat=35.5,
        lon=14.1,
        title=f"test:{event_id}",
        source="mda",
        linked_mmsi="211879870",
        metadata={"anomaly_type": anomaly_type, **metadata},
    )


def _episode(family: str, status: str, count: int, **props) -> dict:
    groups = ["ais_sensor_lineage"] if count else []
    if count >= 2:
        groups = ["ais_sensor_lineage", "secondary_news_reporting"]
    return {"properties": {
        "episode_id": f"episode:test:{family}",
        "episode_family": family,
        "verification_status": status,
        "independent_source_count": count,
        "independence_groups": groups,
        **props,
    }}


def test_single_gap_is_not_hypothesis_eligible() -> None:
    mod = _eligibility()
    event = _event("gap:1", "gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.6})
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("gap_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is False
    assert decision.hypothesis_type == "dark_transit"


def test_long_isolated_gap_enters_collecting_as_derived_investigation() -> None:
    mod = _eligibility()
    event = _event(
        "gap:strong", "gap",
        gap_reason={
            "hypothesis": "vessel_gap", "confidence": 0.7,
            "coverage_ratio": 1.2,
            "nearby_vessels_reporting_before": 12,
            "nearby_vessels_reporting_after": 14,
        },
        silent_seconds=4.5 * 3600,
        jamming_score=0.0,
        port_or_anchorage=None,
        pre_gap_speed_kn=9.0,
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("gap_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is True
    assert decision.hypothesis_type == "dark_transit"
    assert decision.may_advance_collecting is True
    assert decision.evidence_stage == "derived"


def test_long_gap_with_community_coverage_witness_enters_collecting() -> None:
    mod = _eligibility()
    event = _event(
        "gap:community", "gap",
        gap_reason={
            "hypothesis": "vessel_gap", "confidence": 0.82,
            "coverage_ratio": 0.0,
            "nearby_vessels_reporting_before": 0,
            "nearby_vessels_reporting_after": 0,
        },
        silent_seconds=5 * 3600,
        jamming_score=0.0,
        port_or_anchorage=None,
        pre_gap_speed_kn=9.0,
        offshore_context={
            "ais_coverage_witnesses": [{
                "network": "aiscatcher_community",
                "station_id": "3372",
                "coverage_role": "same_lineage_coverage_witness",
            }]
        },
        offshore_reason_codes=["OFFSHORE_CONTEXT", "COMMUNITY_AIS_COVERAGE_PRESENT"],
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("gap_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is True
    assert decision.hypothesis_type == "dark_transit"
    assert decision.may_advance_collecting is True


def test_long_gap_near_port_stays_unpromoted() -> None:
    mod = _eligibility()
    event = _event(
        "gap:port", "gap",
        gap_reason={
            "hypothesis": "vessel_gap", "confidence": 0.7,
            "coverage_ratio": 1.0,
            "nearby_vessels_reporting_before": 20,
            "nearby_vessels_reporting_after": 20,
        },
        silent_seconds=5 * 3600, jamming_score=0.0,
        port_or_anchorage="Piraeus", pre_gap_speed_kn=8.0,
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("gap_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is False


def test_two_same_lineage_gap_indicators_are_not_hypothesis_eligible() -> None:
    mod = _eligibility()
    events = [
        _event("gap:1", "gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.6}),
        _event("gap:2", "gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.55}),
    ]
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("gap_episode", "single_source_multi_indicator", 1), events
    )
    assert decision.eligible is False
    assert "independent corroboration" in decision.explanation


def test_independent_gap_corroboration_is_eligible() -> None:
    mod = _eligibility()
    events = [
        _event("gap:1", "gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.6}),
        IntelEvent(
            id="report:1", type="news", severity="medium", lat=35.5, lon=14.1,
            title="independent report", source="Independent report", linked_mmsi="211879870",
            metadata={"anomaly_type": "gap", "transport": "rss"},
        ),
    ]
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("gap_episode", "multi_source_corroborated", 2), events
    )
    assert decision.eligible is True
    assert decision.may_advance_collecting is True
    assert decision.evidence_stage == "corroborated"


def test_behaviour_context_never_counts_as_independent_source() -> None:
    mod = _eligibility()
    events = [
        _event("gap:1", "gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.6}),
        _event("gap:2", "gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.55}),
    ]
    decision = mod.evaluate_hypothesis_eligibility(
        _episode(
            "gap_episode", "single_source_multi_indicator", 1,
            behaviour_context={"status": "unusual", "reason_codes": ["UNUSUAL_AIS_SILENCE"]},
        ),
        events,
    )
    assert decision.eligible is False



def test_stale_dark_gap_over_twelve_hours_does_not_auto_advance() -> None:
    mod = _eligibility()
    event = _event(
        "gap:stale", "gap",
        gap_reason={
            "hypothesis": "vessel_gap", "confidence": 0.7,
            "coverage_ratio": 1.1,
            "nearby_vessels_reporting_before": 12,
            "nearby_vessels_reporting_after": 13,
        },
        silent_seconds=13 * 3600, jamming_score=0.0,
        port_or_anchorage=None, pre_gap_speed_kn=8.0,
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode(
            "gap_episode", "single_source_observed", 1,
            gap_still_open=True, current_silent_seconds=13 * 3600,
        ),
        [event],
    )
    assert decision.eligible is False
    assert decision.may_advance_collecting is False

def test_reproducible_teleport_enters_collecting_without_becoming_corroborated() -> None:
    mod = _eligibility()
    events = [
        _event(
            "spoof:1", "position_jump",
            ais_integrity_classification={"label": "position_anomaly", "confidence": 0.8},
            teleport_pattern="sustained_relocation",
        ),
        _event(
            "spoof:2", "position_jump",
            ais_integrity_classification={"label": "position_anomaly", "confidence": 0.8},
            teleport_pattern="sustained_relocation",
        ),
    ]
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("spoofing_episode", "single_source_multi_indicator", 1), events
    )
    assert decision.eligible is True
    assert decision.hypothesis_type == "position_spoofing"
    assert decision.may_advance_collecting is True
    assert decision.evidence_stage == "derived"
def test_circular_pattern_does_not_auto_advance_on_one_ais_lineage() -> None:
    mod = _eligibility()
    event = _event(
        "circle:1", "circle_spoof",
        ais_integrity_classification={"label": "position_anomaly", "confidence": 0.8},
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("spoofing_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is True
    assert decision.hypothesis_type == "position_spoofing"
    assert decision.may_advance_collecting is False
    assert decision.evidence_stage == "derived"


def test_sustained_teleport_near_port_does_not_auto_advance() -> None:
    mod = _eligibility()
    event = _event(
        "spoof:port", "position_jump",
        ais_integrity_classification={"label": "position_anomaly", "confidence": 0.8},
        teleport_pattern="sustained_relocation", teleport_near_port="Trieste",
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("spoofing_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is True
    assert decision.may_advance_collecting is False


def test_sustained_teleport_with_coincident_peer_does_not_auto_advance() -> None:
    mod = _eligibility()
    event = _event(
        "spoof:peer", "position_jump",
        ais_integrity_classification={"label": "position_anomaly", "confidence": 0.8},
        teleport_pattern="sustained_relocation",
        coincident_teleport_peers=["subj:mmsi:111000102"],
    )
    decision = mod.evaluate_hypothesis_eligibility(
        _episode("spoofing_episode", "single_source_observed", 1), [event]
    )
    assert decision.eligible is True
    assert decision.may_advance_collecting is False
