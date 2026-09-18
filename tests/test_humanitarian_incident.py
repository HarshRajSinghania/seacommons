# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/updates.md P0.3: canonical HumanitarianIncident.

Exit gate (v0-bounded, documented in the module itself): persisted
state_changed_at/resolved_at/archived_at exist and update correctly on
real lifecycle transitions -- full cross-source correlation is P2.1, a
later packet.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest
from core.intel.humanitarian_incident import (
    _on_intel_event,
    get_incident,
    list_transitions,
    register,
    sync_incident_for_event,
)
from core.intel.store import IntelEvent, intel_store


@pytest.fixture(autouse=True)
def _fresh_table():
    from core.db.models import HumanitarianIncidentDB, IncidentTransitionDB
    from core.db.session import engine, session_scope

    HumanitarianIncidentDB.__table__.create(bind=engine(), checkfirst=True)
    IncidentTransitionDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.query(HumanitarianIncidentDB).delete()
        db.query(IncidentTransitionDB).delete()
    yield


def _distress_event(event_id, text, *, timestamp=None):
    return IntelEvent(
        id=event_id, type="distress", severity="high", lat=35.5, lon=14.1,
        title=text[:80], text=text, source="Alarm Phone",
        timestamp_utc=timestamp or datetime.now(timezone.utc).isoformat(),
        metadata={"is_distress": True},
    )


def test_a_new_incident_is_created_on_first_sync():
    event = _distress_event("h1", "MAYDAY 30 people taking water off Libya")
    sync_incident_for_event(event, lifecycle="active")

    incident = get_incident("h1")
    assert incident is not None
    assert incident["lifecycle"] == "active"
    assert incident["revision"] == 1
    assert incident["resolved_at"] is None
    assert incident["source_observation_ids"] == ["h1"]


def test_state_changed_at_updates_only_when_lifecycle_actually_changes():
    event = _distress_event("h2", "MAYDAY people in the water")
    sync_incident_for_event(event, lifecycle="active")
    first = get_incident("h2")

    time.sleep(0.01)
    sync_incident_for_event(event, lifecycle="active")  # same lifecycle -- no-op
    second = get_incident("h2")
    assert second["state_changed_at"] == first["state_changed_at"]
    assert second["revision"] == 2  # last_update_at/revision still advance

    sync_incident_for_event(event, lifecycle="resolved")  # real transition
    third = get_incident("h2")
    assert third["state_changed_at"] != first["state_changed_at"]


def test_resolved_at_is_set_once_and_never_cleared():
    event = _distress_event("h3", "distress")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="resolved")
    first_resolved_at = get_incident("h3")["resolved_at"]
    assert first_resolved_at is not None

    time.sleep(0.01)
    sync_incident_for_event(event, lifecycle="resolved")  # repeated resolved sync
    assert get_incident("h3")["resolved_at"] == first_resolved_at


def test_archived_at_is_set_when_lifecycle_becomes_archived():
    event = _distress_event("h4", "distress")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="archived")
    incident = get_incident("h4")
    assert incident["archived_at"] is not None
    assert incident["lifecycle"] == "archived"


def test_case_type_is_recorded_once_available_and_not_overwritten_with_none():
    event = _distress_event("h5", "distress")
    sync_incident_for_event(event, lifecycle="active", case_type="distress")
    sync_incident_for_event(event, lifecycle="active", case_type=None)
    assert get_incident("h5")["case_type"] == "distress"


def test_get_incident_returns_none_for_an_unknown_id():
    assert get_incident("does-not-exist") is None


# ── subscriber wiring ────────────────────────────────────────────────────


def test_on_intel_event_skips_non_humanitarian_events():
    event = IntelEvent(
        id="ais1", type="ais_anomaly", severity="medium", lat=35.5, lon=14.1,
        source="mda", metadata={"anomaly_type": "gap", "maritime_domain": "grey_zone"},
    )
    _on_intel_event(event)  # must not raise
    assert get_incident("ais1") is None


def test_on_intel_event_syncs_a_real_humanitarian_event():
    event = _distress_event("h6", "MAYDAY urgent rescue needed")
    _on_intel_event(event)
    incident = get_incident("h6")
    assert incident is not None
    assert incident["lifecycle"] == "active"


def test_registered_subscriber_syncs_on_a_real_intel_store_add():
    """docs/updates.md P0.3: end-to-end through the real intel_store.add()
    write path and the real (async, background-thread) subscriber
    fan-out -- not a direct function call."""
    with intel_store._lock:
        intel_store._subscribers.clear()
    register()

    event_id = f"h-e2e-{uuid.uuid4()}"
    event = _distress_event(event_id, "MAYDAY 12 people aboard, taking water")
    intel_store.add(event, dedup_key=event_id)

    incident = None
    for _ in range(50):
        incident = get_incident(event_id)
        if incident is not None:
            break
        time.sleep(0.05)
    assert incident is not None
    assert incident["lifecycle"] == "active"


# ── docs/updates.md P0.5: transition audit trail ────────────────────────


def test_a_new_incident_records_its_first_transition():
    event = _distress_event("t1", "MAYDAY people in the water")
    sync_incident_for_event(event, lifecycle="active")

    transitions = list_transitions("t1")
    assert len(transitions) == 1
    assert transitions[0]["from_state"] is None
    assert transitions[0]["to_state"] == "active"
    assert transitions[0]["supporting_observation_ids"] == ["t1"]


def test_repeated_syncs_at_the_same_lifecycle_record_no_new_transition():
    event = _distress_event("t2", "MAYDAY people in the water")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="active")
    assert len(list_transitions("t2")) == 1


def test_a_real_transition_appends_a_new_row_with_from_and_to_state():
    event = _distress_event("t3", "distress")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="resolved")

    transitions = list_transitions("t3")
    assert len(transitions) == 2
    assert {"from_state": None, "to_state": "active"}.items() <= transitions[0].items()
    assert transitions[1]["from_state"] == "active"
    assert transitions[1]["to_state"] == "resolved"
    assert transitions[1]["reason_code"] in {"cross_post_resolution_signal", "self_reply_outcome"}


def test_transitions_are_never_edited_in_place_only_appended():
    """docs/updates.md P0.5: append-only -- reopening/oscillating a
    lifecycle keeps every prior transition, never rewrites one."""
    event = _distress_event("t4", "distress")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="needs_review")
    sync_incident_for_event(event, lifecycle="archived")

    transitions = list_transitions("t4")
    assert [t["to_state"] for t in transitions] == ["active", "needs_review", "archived"]
    assert transitions[2]["from_state"] == "needs_review"


def test_needs_review_transition_sets_review_required():
    event = _distress_event("t5", "distress")
    sync_incident_for_event(event, lifecycle="active")
    sync_incident_for_event(event, lifecycle="needs_review")

    transitions = list_transitions("t5")
    assert transitions[-1]["review_required"] is True
    assert transitions[0]["review_required"] is False


def test_list_transitions_returns_empty_for_an_unknown_incident():
    assert list_transitions("does-not-exist") == []


# ── docs/updates.md P0.6: timer contract ────────────────────────────────


def test_reported_at_never_changes_after_creation():
    event = _distress_event("p6-1", "distress", timestamp="2026-09-04T08:00:00+00:00")
    sync_incident_for_event(event, lifecycle="active")
    later = _distress_event("p6-1", "update", timestamp="2026-09-04T09:00:00+00:00")
    sync_incident_for_event(later, lifecycle="active")

    assert get_incident("p6-1")["reported_at"] == "2026-09-04T08:00:00+00:00"


def test_last_update_at_advances_on_an_in_order_update():
    event = _distress_event("p6-2", "distress", timestamp="2026-09-04T08:00:00+00:00")
    sync_incident_for_event(event, lifecycle="active")
    later = _distress_event("p6-2", "update", timestamp="2026-09-04T09:00:00+00:00")
    sync_incident_for_event(later, lifecycle="active")

    assert get_incident("p6-2")["last_update_at"] == "2026-09-04T09:00:00+00:00"


def test_last_update_at_never_moves_backward_on_an_out_of_order_update():
    """docs/updates.md P0.6 exit gate: a delayed/out-of-order observation
    must never make an incident look older than an already-processed
    later one."""
    event = _distress_event("p6-3", "distress", timestamp="2026-09-04T09:00:00+00:00")
    sync_incident_for_event(event, lifecycle="active")
    stale = _distress_event("p6-3", "a delayed earlier report", timestamp="2026-09-04T07:00:00+00:00")
    sync_incident_for_event(stale, lifecycle="active")

    assert get_incident("p6-3")["last_update_at"] == "2026-09-04T09:00:00+00:00"


def test_humanitarian_incident_route_reconstructs_the_timer_from_api_fields() -> None:
    from core.api.main import app
    from fastapi.testclient import TestClient

    event = _distress_event("p6-4", "MAYDAY people aboard", timestamp="2026-09-04T08:00:00+00:00")
    sync_incident_for_event(event, lifecycle="active")

    response = TestClient(app).get("/api/v1/audit/humanitarian-incidents/p6-4")
    assert response.status_code == 200
    payload = response.json()
    assert payload["reported_at"] == "2026-09-04T08:00:00+00:00"
    assert payload["last_update_at"] == "2026-09-04T08:00:00+00:00"
    assert len(payload["transitions"]) == 1


def test_humanitarian_incident_route_404s_for_an_unknown_incident() -> None:
    from core.api.main import app
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/api/v1/audit/humanitarian-incidents/does-not-exist")
    assert response.status_code == 404

# ── Live/Play status separation (2026-09-05) ───────────────────────────


def test_public_incident_status_keeps_recent_active_incident_active():
    from core.intel.humanitarian_incident import public_incident_status

    incident = {
        "lifecycle": "active",
        "incident_status": "active",
        "last_update_at": "2026-09-04T12:30:00+00:00",
    }
    now = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    assert public_incident_status(incident, now=now) == "active"


def test_public_incident_status_retires_silent_active_to_outcome_unknown():
    from core.intel.humanitarian_incident import public_incident_status

    incident = {
        "lifecycle": "active",
        "incident_status": "active",
        "last_update_at": "2026-09-03T23:00:00+00:00",
    }
    now = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    assert public_incident_status(incident, now=now) == "outcome_unknown"

def test_public_incident_status_maps_legacy_archived_to_outcome_unknown():
    from core.intel.humanitarian_incident import public_incident_status

    incident = {
        "lifecycle": "archived",
        "incident_status": None,
        "last_update_at": "2026-09-01T08:00:00+00:00",
    }
    now = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    assert public_incident_status(incident, now=now) == "outcome_unknown"


def test_sync_persists_incident_status_independently_from_legacy_lifecycle():
    event = _distress_event("status-1", "distress")
    sync_incident_for_event(event, lifecycle="active")
    assert get_incident("status-1")["incident_status"] == "active"

    sync_incident_for_event(event, lifecycle="resolved")
    assert get_incident("status-1")["incident_status"] == "resolved"

    sync_incident_for_event(event, lifecycle="archived")
    incident = get_incident("status-1")
    assert incident["lifecycle"] == "archived"
    assert incident["incident_status"] == "outcome_unknown"


def test_reconcile_stale_active_incident_persists_outcome_unknown():
    from core.intel.humanitarian_incident import reconcile_stale_incidents

    event = _distress_event("reconcile-old", "distress", timestamp="2026-09-03T10:00:00+00:00")
    sync_incident_for_event(event, lifecycle="active")
    changed = reconcile_stale_incidents(now=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc))

    incident = get_incident("reconcile-old")
    assert changed == 1
    assert incident["incident_status"] == "outcome_unknown"
    assert incident["lifecycle"] == "archived"
    assert incident["archived_at"] is not None


def test_reconcile_does_not_convert_needs_review_to_outcome_unknown():
    from core.intel.humanitarian_incident import reconcile_stale_incidents

    event = _distress_event("reconcile-review", "distress", timestamp="2026-09-03T10:00:00+00:00")
    sync_incident_for_event(event, lifecycle="needs_review")
    changed = reconcile_stale_incidents(now=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc))

    incident = get_incident("reconcile-review")
    assert changed == 0
    assert incident["incident_status"] == "needs_review"
    assert incident["lifecycle"] == "needs_review"


def test_verification_source_cannot_open_humanitarian_incident():
    event = IntelEvent(
        id="ngo-verify-1", type="distress", severity="high", lat=35.5, lon=14.1,
        title="We rescued 42 people", text="We rescued 42 people from a boat in distress.",
        source="SOS Méditerranée", timestamp_utc=datetime.now(timezone.utc).isoformat(),
        metadata={"is_distress": True, "service": "humanitarian", "lane": "resolution", "transport": "rss"},
    )
    _on_intel_event(event)
    assert get_incident(event.id) is None


def test_alarm_phone_operational_origin_still_opens_humanitarian_incident():
    event = _distress_event("origin-still-opens", "MAYDAY urgent rescue needed")
    event.metadata["transport"] = "email"
    _on_intel_event(event)
    assert get_incident(event.id) is not None


def test_recent_reconcile_updates_stale_needs_review_after_classifier_fix(monkeypatch):
    from core.intel.humanitarian_incident import reconcile_recent_incident_lifecycles

    text = (
        "Yesterday, following a Mayday relay, the #OceanViking altered course "
        "towards a rubber boat carrying around 80 people in distress. As our "
        "team approached, we witnessed the Libyan Coast Guard intercept the "
        "boat and return those on board to Libya."
    )
    event = IntelEvent(
        id="sosmed-reconcile",
        type="twitter",
        severity="critical",
        lat=32.0,
        lon=16.8,
        title=text[:80],
        text=text,
        source="SOSMedIntl",
        timestamp_utc="2026-09-18T09:46:44+00:00",
        metadata={
            "is_distress": True,
            "humanitarian_case_type": "interception",
            "thread_reposts": [{
                "posted_at": "2026-09-18T09:46:47+00:00",
                "note": (
                    "We were only minutes away. The Ocean Viking is sailing "
                    "towards its assigned Place of Safety."
                ),
            }],
        },
    )
    sync_incident_for_event(event, lifecycle="needs_review", case_type="interception")
    monkeypatch.setattr(intel_store, "get_durable", lambda _event_id: event)
    monkeypatch.setattr(
        intel_store,
        "persisted_events",
        lambda **_kwargs: [event],
    )

    changed = reconcile_recent_incident_lifecycles(
        now=datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    )

    incident = get_incident(event.id)
    assert changed == 1
    assert incident["lifecycle"] == "resolved"
    assert incident["incident_status"] == "resolved"
    assert incident["case_type"] == "interception"
