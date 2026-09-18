# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/updates.md P0.8: Beacon compartment fix.

Regression fixture for a real production defect: an AIS-SART/AIS-MOB/
AIS-EPIRB distress beacon (or nav_status==14) is a Maritime Safety
self-report, not a human-corroborated Humanitarian case. Before this
fix, core.intel.vessel_incident_monitor wrote maritime_domain="sar" for
a beacon (unlike the other three nav-status kinds, which already wrote
"safety"), and core.live.feed.public_signal_collection routes the "sar"
domain into the Humanitarian compartment via
core.intel.public_policy.compartment_for_domain -- so a bare AIS
transponder ping rendered as a pulsing Humanitarian distress marker on
the public Live map and (in a slightly different, already-guarded path)
risked being read as a Humanitarian case, with zero human corroboration.

Exit gate: a beacon alone (1) classifies maritime/safety, never
humanitarian, (2) never appears in the Humanitarian Live feed mode,
(3) does appear in the Safety Live feed mode, (4) never creates a
HumanitarianIncidentDB row.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from core.intel import vessel_incident_monitor as vim
from core.intel.service_taxonomy import classify_service


class _Clock:
    def __init__(self) -> None:
        self.now = 2_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def monitor(monkeypatch):
    """Unlike tests/test_vessel_incidents.py's `monitor` fixture, this
    one does NOT mock intel_store.add -- P0.8's regression must prove
    the real live entry point (intel_store.add -> subscriber fan-out ->
    public_signal_collection / humanitarian_incident sync), not just the
    event object vessel_incident_monitor constructs."""
    clock = _Clock()
    monkeypatch.setattr(vim.time, "time", clock)
    m = vim.VesselIncidentMonitor()
    m._running = True
    return m


@pytest.fixture(autouse=True)
def _fresh_incident_table():
    from core.db.models import HumanitarianIncidentDB
    from core.db.session import engine, session_scope

    HumanitarianIncidentDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.query(HumanitarianIncidentDB).delete()
    yield


@pytest.mark.parametrize("mmsi_prefix,label", [("970", "ais_sart"), ("972", "ais_mob"), ("974", "ais_epirb")])
def test_beacon_event_classifies_maritime_safety_never_humanitarian(monitor, mmsi_prefix, label) -> None:
    mmsi = f"{mmsi_prefix}{uuid.uuid4().int % 1_000_000:06d}"
    monitor.on_position(mmsi, "", 35.1, 14.2, 0.0, 0)

    from core.intel.store import intel_store

    event = next(e for e in intel_store.events(limit=50) if e.linked_mmsi == mmsi)
    assert event.metadata["maritime_domain"] == "safety"
    assert event.metadata["service"] == "maritime"
    assert event.metadata["lane"] == "safety"

    classification = classify_service(event)
    assert classification.service == "maritime"
    assert classification.lane == "safety"
    assert classification.service != "humanitarian"


def test_nav_status_14_on_normal_vessel_does_not_become_distress_beacon(monitor) -> None:
    mmsi = f"211{uuid.uuid4().int % 1_000_000:06d}"
    monitor.on_position(mmsi, "MV TEST", 34.0, 13.0, 0.0, 14)

    from core.intel.store import intel_store

    assert not any(e.linked_mmsi == mmsi for e in intel_store.events(limit=50))


def test_beacon_never_appears_in_the_humanitarian_live_feed(monitor) -> None:
    from core.live.feed import public_signal_collection

    mmsi = f"972{uuid.uuid4().int % 1_000_000:06d}"
    monitor.on_position(mmsi, "", 35.1, 14.2, 0.0, 0)

    collection = public_signal_collection(mode="humanitarian", limit=500, days=1)
    ids = {f["properties"].get("id") for f in collection["features"]}
    assert not any(mmsi in str(i) for i in ids if i)


def test_beacon_appears_in_the_safety_live_feed(monitor) -> None:
    from core.live.feed import public_signal_collection

    mmsi = f"974{uuid.uuid4().int % 1_000_000:06d}"
    monitor.on_position(mmsi, "", 35.1, 14.2, 0.0, 0)
    from core.intel.store import intel_store

    event = next(e for e in intel_store.events(limit=50) if e.linked_mmsi == mmsi)
    event.metadata["last_observed_at"] = datetime.now(timezone.utc).isoformat()

    collection = public_signal_collection(mode="safety", limit=500, days=1)
    ids = {f["properties"].get("id") for f in collection["features"]}
    assert any(mmsi in str(i) for i in ids if i)


def test_stale_beacon_leaves_public_live_case_surface() -> None:
    from core.live.projection import is_useful_public_case_feature

    now = datetime.now(timezone.utc)
    base = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.2, 35.1]},
        "properties": {
            "type": "distress",
            "ais_nav_status_kind": "distress_beacon",
            "linked_mmsi": "970123456",
            "mmsi": "970123456",
            "source": "ais_sart",
            "visual_category": "distress",
            "verification_status": "ais_transponder",
        },
    }
    stale = {
        **base,
        "properties": {
            **base["properties"],
            "last_observed_at": (now - timedelta(hours=3)).isoformat(),
        },
    }
    fresh = {
        **base,
        "properties": {
            **base["properties"],
            "last_observed_at": (now - timedelta(minutes=10)).isoformat(),
        },
    }
    assert is_useful_public_case_feature(stale) is False
    assert is_useful_public_case_feature(fresh) is True


def test_beacon_alone_never_creates_a_humanitarian_incident(monitor) -> None:
    from core.intel.humanitarian_incident import get_incident, register

    register()  # idempotent subscribe, same as bootstrap

    mmsi = f"970{uuid.uuid4().int % 1_000_000:06d}"
    monitor.on_position(mmsi, "", 35.1, 14.2, 0.0, 0)

    event_id = vim._event_id(mmsi, "distress_beacon")
    # Subscriber fan-out runs off-thread; give it a bounded chance to run,
    # then assert it correctly did NOT create an incident either way.
    for _ in range(20):
        time.sleep(0.05)
        if get_incident(event_id) is not None:
            break

    assert get_incident(event_id) is None
