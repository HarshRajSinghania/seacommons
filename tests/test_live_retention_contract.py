"""Durable Live retention contract (core.live.retention).

A qualified AIS-evidence observation must stay visible on Live for >=24h
from its last qualified observation, independent of the bounded in-memory
deque, process restart, or resolved/explained operational state -- and the
server, never the browser clock, is authoritative for that window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.intel.store import IntelEvent, intel_store
from core.live.retention import compute_retention_window, is_live_retained


def _qualified_ais_event(event_id: str, mmsi: str, *, lat=34.5, lon=17.0) -> IntelEvent:
    return IntelEvent(
        id=event_id,
        type="ais_anomaly",
        severity="high",
        lat=lat,
        lon=lon,
        title=f"AIS gap — retention test {event_id}",
        source="mda",
        linked_mmsi=mmsi,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        metadata={
            "anomaly_type": "gap",
            "maritime_domain": "grey_zone",
            "publication_status": "published",
            "verification_status": "ais_transponder",
            "analysis_state": "evidence_candidate",
            "offshore_anomaly_qualified": True,
            "reception_expectation": {
                "support_level": "strong",
                "expected_messages_during_gap": 120,
            },
            "gap_still_open": True,
        },
    )


def test_qualified_ais_event_gets_a_durable_24h_window():
    event = _qualified_ais_event("retention-test:new-case", "211900001")
    assert intel_store.add(event, dedup_key=event.id) is True

    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    with session_scope() as db:
        row = db.get(IntelEventDB, event.id)
        assert row is not None
        assert row.live_entered_at is not None
        assert row.live_expires_at is not None
        assert row.live_expires_at - row.live_entered_at == timedelta(hours=24)
        assert row.live_case_key == f"ais_anomaly:gap:211900001"


def test_non_qualifying_event_gets_no_retention_window():
    event = IntelEvent(
        id="retention-test:unqualified",
        type="ais_anomaly",
        severity="low",
        lat=34.5,
        lon=17.0,
        title="AIS anomaly, not offshore-qualified",
        source="mda",
        linked_mmsi="211900002",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        metadata={"anomaly_type": "gap", "analysis_state": "anomaly"},
    )
    assert intel_store.add(event, dedup_key=event.id) is True

    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    with session_scope() as db:
        row = db.get(IntelEventDB, event.id)
        assert row is not None
        assert row.live_expires_at is None
        assert row.live_case_key is None


def test_repeated_qualified_observation_extends_not_restarts_the_window():
    mmsi = "211900003"
    first = _qualified_ais_event("retention-test:case-a-1", mmsi)
    assert intel_store.add(first, dedup_key=first.id) is True

    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    with session_scope() as db:
        first_row = db.get(IntelEventDB, first.id)
        first_entered_at = first_row.live_entered_at
        first_expires_at = first_row.live_expires_at

    second = _qualified_ais_event("retention-test:case-a-2", mmsi)
    assert intel_store.add(second, dedup_key=second.id) is True

    with session_scope() as db:
        second_row = db.get(IntelEventDB, second.id)
        # Same case (same mmsi + anomaly family): entered_at carries forward
        # from the still-live first observation, but the expiry extends.
        assert second_row.live_entered_at == first_entered_at
        assert second_row.live_expires_at >= first_expires_at


def test_qualified_evidence_survives_eviction_from_the_bounded_deque():
    """The exact bug this contract fixes: an item evicted from IntelStore's
    600-slot in-memory deque (by churn, or a restart that reloads only the
    most-recent 600 rows globally) used to vanish from Live entirely, well
    before its 24h window. live_retained_events() must find it by durable
    live_expires_at alone, with no dependency on deque residency."""
    event = _qualified_ais_event("retention-test:evicted-case", "211900004")
    assert intel_store.add(event, dedup_key=event.id) is True

    # Simulate deque eviction / a fresh-process restart: the event is no
    # longer resident in memory at all.
    with intel_store._lock:
        intel_store._events = type(intel_store._events)(
            (e for e in intel_store._events if e.id != event.id),
            maxlen=intel_store._events.maxlen,
        )
    assert all(e.id != event.id for e in intel_store.events(limit=600))

    retained = intel_store.live_retained_events(limit=500)
    assert any(e.id == event.id for e in retained)


def test_server_expiry_is_authoritative_regardless_of_client_supplied_window():
    """core.live.feed accepts a caller-supplied `days` window for the
    generic durable read, but a retained qualified event's own visibility
    is governed only by its server-computed live_expires_at -- a caller
    cannot make an expired case reappear, or a live one disappear, by
    varying `days`."""
    from core.live.feed import public_signal_collection

    event = _qualified_ais_event("retention-test:server-authoritative", "211900005")
    assert intel_store.add(event, dedup_key=event.id) is True

    for days in (1, 7, 30, 365):
        collection = public_signal_collection(days=days, mode="maritime")
        # Maritime mode coalesces raw signals into a vessel-centric episode
        # feature (core.live.vessel_episodes) -- the id is rewritten, so
        # assert on the vessel identity surviving into the response instead
        # of the raw intel: id.
        ids = {f["properties"]["id"] for f in collection["features"]}
        assert any("211900005" in feature_id for feature_id in ids), f"missing at days={days}: {ids}"


def test_compute_retention_window_pure_arithmetic():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entered, last_qualified, expires = compute_retention_window(
        prior_entered_at=None, prior_expires_at=None, now=now,
    )
    assert entered == now
    assert last_qualified == now
    assert expires == now + timedelta(hours=24)

    later = now + timedelta(hours=10)
    entered2, last_qualified2, expires2 = compute_retention_window(
        prior_entered_at=entered, prior_expires_at=expires, now=later,
    )
    assert entered2 == now  # carried forward: the prior window was still live
    assert expires2 == later + timedelta(hours=24)  # extended

    after_lapse = expires + timedelta(hours=1)
    entered3, _, _ = compute_retention_window(
        prior_entered_at=entered, prior_expires_at=expires, now=after_lapse,
    )
    assert entered3 == after_lapse  # window had already lapsed: fresh entry


def test_is_live_retained_handles_naive_datetimes_from_sqlite():
    # This project's SQLite DateTime columns round-trip naive; is_live_retained
    # must not raise (and must not silently misjudge) when given one.
    naive_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    now = datetime.now(timezone.utc)
    assert is_live_retained(naive_future, now=now) is True
    assert is_live_retained(naive_past, now=now) is False
    assert is_live_retained(None, now=now) is False


def test_gap_retention_uses_same_reception_gate_as_public_live():
    from core.live.retention import is_qualifying_ais_evidence

    base = {
        "anomaly_type": "gap",
        "offshore_anomaly_qualified": True,
        "analysis_state": "evidence_candidate",
        "independent_source_count": 1,
    }
    assert is_qualifying_ais_evidence("ais_anomaly", base) is False
    assert is_qualifying_ais_evidence(
        "ais_anomaly",
        {**base, "reception_expectation": {"support_level": "strong"}},
    ) is True
