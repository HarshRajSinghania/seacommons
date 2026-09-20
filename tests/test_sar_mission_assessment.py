from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_assessments():
    from core.db.models import AssessmentDB
    from core.db.session import engine, session_scope

    AssessmentDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.query(AssessmentDB).filter(AssessmentDB.field_type == "sar_mission").delete()
    yield


def _response(*, state="probable_rescue_activity", coverage="coverage_present", providers=None, upstream=None):
    return {
        "ngo_vessels": [{
            "mmsi": "258479000", "name": "Ocean Viking", "org": "SOS Méditerranée",
            "mission_state": state, "coverage_status": coverage,
            "motion_flags": ["search_pattern"] if state == "probable_rescue_activity" else [],
            "track_providers": providers or ["aisstream", "aiscast"],
            "upstream_sources": upstream or ["aisstream", "volunteer"],
            "stations": ["mt-01"], "distance_nm": 4.2, "heading_toward": True,
            "eta_h": 0.4, "fix_age_min": 3,
        }],
    }


def test_enrich_active_humanitarian_incidents_wires_analyze_and_persist(monkeypatch):
    """persist_sar_mission_assessments()/analyze_ngo_response() both already
    existed fully implemented but nothing in the running system ever called
    this combination -- enrich_active_humanitarian_incidents_with_sar_mission()
    is the missing caller, run every 15 min by the scheduler."""
    from core.db.models import HumanitarianIncidentDB, IntelEventDB
    from core.db.session import session_scope
    from core.intel import sar_mission_assessment as module

    incident_id = "sar-enrich-test-incident"
    with session_scope() as db:
        db.query(HumanitarianIncidentDB).filter(
            HumanitarianIncidentDB.incident_id == incident_id
        ).delete()
        db.query(IntelEventDB).filter(IntelEventDB.id == incident_id).delete()
        db.add(IntelEventDB(
            id=incident_id, timestamp_utc="2026-09-20T10:00:00+00:00",
            type="distress", severity="high", lat=35.5, lon=14.1,
            title="Distress report", source="Alarm Phone", meta={},
        ))
        db.add(HumanitarianIncidentDB(
            incident_id=incident_id, lifecycle="active", incident_status="active",
            case_type="distress",
        ))

    import core.intel.ngo_response as ngo_response_module

    monkeypatch.setattr(ngo_response_module, "analyze_ngo_response", lambda event, **kw: _response())

    enriched = module.enrich_active_humanitarian_incidents_with_sar_mission()
    assert enriched >= 1

    rows = module.get_sar_mission_assessments(incident_id)
    assert len(rows) == 1
    assert rows[0]["value"]["mission_state"] == "probable_rescue_activity"
    # SAR AIS movement is one lineage -- it must never fabricate corroboration.
    assert rows[0]["value"]["independence_groups"] == ["ais_sensor_lineage"]

    with session_scope() as db:
        db.query(HumanitarianIncidentDB).filter(
            HumanitarianIncidentDB.incident_id == incident_id
        ).delete()
        db.query(IntelEventDB).filter(IntelEventDB.id == incident_id).delete()


def test_enrich_active_humanitarian_incidents_skips_incidents_with_no_position():
    from core.db.models import HumanitarianIncidentDB, IntelEventDB
    from core.db.session import session_scope
    from core.intel.sar_mission_assessment import (
        enrich_active_humanitarian_incidents_with_sar_mission,
    )

    incident_id = "sar-enrich-no-position"
    with session_scope() as db:
        db.query(HumanitarianIncidentDB).filter(
            HumanitarianIncidentDB.incident_id == incident_id
        ).delete()
        db.query(IntelEventDB).filter(IntelEventDB.id == incident_id).delete()
        db.add(IntelEventDB(
            id=incident_id, timestamp_utc="2026-09-20T10:00:00+00:00",
            type="distress", severity="high", lat=None, lon=None,
            title="Distress report, no position", source="Alarm Phone", meta={},
        ))
        db.add(HumanitarianIncidentDB(
            incident_id=incident_id, lifecycle="active", incident_status="active",
            case_type="distress",
        ))

    # Must not raise (ValueError from analyze_ngo_response's own lat/lon
    # guard) -- the caller filters unpositioned incidents out first.
    enrich_active_humanitarian_incidents_with_sar_mission()

    with session_scope() as db:
        db.query(HumanitarianIncidentDB).filter(
            HumanitarianIncidentDB.incident_id == incident_id
        ).delete()
        db.query(IntelEventDB).filter(IntelEventDB.id == incident_id).delete()


def test_persisted_sar_mission_assessment_is_idempotent_per_incident_asset():
    from core.intel.sar_mission_assessment import (
        get_sar_mission_assessments,
        persist_sar_mission_assessments,
    )

    first = persist_sar_mission_assessments("incident-1", _response())
    second = persist_sar_mission_assessments("incident-1", _response())
    assert first == second
    rows = get_sar_mission_assessments("incident-1")
    assert len(rows) == 1
    assert rows[0]["value"]["mission_state"] == "probable_rescue_activity"


def test_provider_degraded_caps_persisted_state_at_possible_response():
    from core.intel.sar_mission_assessment import persist_sar_mission_assessments

    row = persist_sar_mission_assessments(
        "incident-2", _response(state="probable_rescue_activity", coverage="provider_degraded")
    )[0]
    assert row["value"]["mission_state"] == "possible_response"
    assert "PROVIDER_DEGRADED_CAP" in row["value"]["reason_codes"]


def test_ais_only_input_can_never_persist_rescue_confirmed():
    from core.intel.sar_mission_assessment import persist_sar_mission_assessments

    row = persist_sar_mission_assessments("incident-3", _response(state="rescue_confirmed"))[0]
    assert row["value"]["mission_state"] == "probable_rescue_activity"
    assert "AIS_CONFIRMATION_CAP" in row["value"]["reason_codes"]


def test_multiple_ais_transports_remain_one_physical_independence_group():
    from core.intel.sar_mission_assessment import persist_sar_mission_assessments

    row = persist_sar_mission_assessments(
        "incident-4",
        _response(providers=["aisstream", "aiscast"], upstream=["aisstream", "aisstream"]),
    )[0]
    assert row["value"]["track_providers"] == ["aiscast", "aisstream"]
    assert row["value"]["upstream_sources"] == ["aisstream"]
    assert row["value"]["independence_groups"] == ["ais_sensor_lineage"]


@pytest.mark.parametrize("state", ["unrelated", "possible_response", "approaching", "on_scene", "probable_rescue_activity"])
def test_existing_descriptive_mission_states_persist_without_intent_inference(state):
    from core.intel.sar_mission_assessment import persist_sar_mission_assessments

    row = persist_sar_mission_assessments(f"incident-{state}", _response(state=state))[0]
    assert row["value"]["mission_state"] == state
    assert "intent" not in row["value"]
    assert "risk_score" not in row["value"]
