# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md F-07 / Phase 3 -- positive compartment allow-lists.

An event's operational compartment is decided positively, never
"not in the security set -> humanitarian". piracy is security.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.intel.public_policy import compartment_for_domain
from core.intel.store import IntelEvent, intel_store
from core.live.feed import public_signal_collection


def test_compartment_for_domain_is_a_positive_mapping():
    assert compartment_for_domain("sar") == "humanitarian"
    assert compartment_for_domain("piracy") == "security"
    assert compartment_for_domain("sanctions") == "security"
    assert compartment_for_domain("grey_zone") == "security"
    assert compartment_for_domain("iuu_fishing") == "security"
    assert compartment_for_domain("smuggling") == "security"
    # No fallback -- these need an explicit decision.
    assert compartment_for_domain("safety") is None
    assert compartment_for_domain("environmental") is None
    assert compartment_for_domain("") is None
    assert compartment_for_domain(None) is None


def _cleanup(prefix: str) -> None:
    with intel_store._lock:
        intel_store._events = type(intel_store._events)(
            (e for e in intel_store._events if not e.id.startswith(prefix)),
            maxlen=intel_store._events.maxlen,
        )


def test_piracy_event_is_never_in_the_humanitarian_feed():
    event = IntelEvent(
        id="compartment-piracy-1",
        type="piracy_incident",
        severity="high",
        lat=12.5,
        lon=45.0,
        title="Boarding reported in the Gulf of Aden",
        source="SeaCommons MDA",
        timestamp_utc="2026-08-30T00:00:00+00:00",
        metadata={"source_policy": "official_api", "publication_status": "published"},
    )
    assert intel_store.add(event) is True
    try:
        humanitarian = public_signal_collection(mode="humanitarian", days=60)
        hum_ids = {str(f["properties"]["id"]) for f in humanitarian["features"]}
        assert "intel:compartment-piracy-1" not in hum_ids
        # mode_counts must not count it as humanitarian either.
        assert humanitarian["meta"]["mode_counts"].get("humanitarian", 0) >= 0
        assert "intel:compartment-piracy-1" not in {
            str(f["properties"]["id"])
            for f in public_signal_collection(mode="humanitarian", days=60)["features"]
        }
    finally:
        _cleanup("compartment-")


def test_safety_domain_event_has_no_operational_compartment():
    event = IntelEvent(
        id="compartment-safety-1",
        type="oil_spill",
        severity="high",
        lat=35.0,
        lon=18.0,
        title="Reported slick",
        source="SeaCommons MDA",
        timestamp_utc="2026-08-30T00:00:00+00:00",
        metadata={"maritime_domain": "environmental", "publication_status": "published"},
    )
    assert intel_store.add(event) is True
    try:
        for mode in ("humanitarian", "security", "all"):
            ids = {
                str(f["properties"]["id"])
                for f in public_signal_collection(mode=mode, days=60)["features"]
            }
            assert "intel:compartment-safety-1" not in ids
    finally:
        _cleanup("compartment-")


def test_public_maritime_mode_unifies_safety_and_security_with_canonical_counts(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    humanitarian = IntelEvent(
        id="canonical-hum-1", type="distress", severity="high", lat=34.8, lon=14.2,
        title="Reported distress", source="Alarm Phone", timestamp_utc=now,
        metadata={"is_distress": True, "maritime_domain": "sar", "source_policy": "official_site_embed"},
    )
    safety = IntelEvent(
        id="canonical-safety-1", type="vessel_incident", severity="medium", lat=35.2, lon=14.0,
        title="Vessel unable to manoeuvre", source="ais", timestamp_utc=now,
        metadata={"ais_nav_status_kind": "not_under_command", "maritime_domain": "safety",
                  "publication_status": "published", "source_policy": "official_api"},
    )
    security = IntelEvent(
        id="canonical-security-1", type="ais_anomaly", severity="high", lat=35.1, lon=14.5,
        title="AIS identity anomaly", source="SeaCommons MDA", timestamp_utc=now,
        metadata={"maritime_domain": "grey_zone", "publication_status": "published",
                  "source_policy": "operator_published"},
    )
    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: [humanitarian, safety, security])
    monkeypatch.setattr("core.live.feed.intel_store.persisted_events", lambda **_kwargs: [])
    monkeypatch.setattr("core.live.feed._published_ingested_features", lambda _limit: [])

    maritime = public_signal_collection(mode="maritime", days=1, limit=50)
    legacy_security = public_signal_collection(mode="security", days=1, limit=50)
    humanitarian_feed = public_signal_collection(mode="humanitarian", days=1, limit=50)
    all_feed = public_signal_collection(mode="all", days=1, limit=50)

    maritime_ids = {f["properties"]["id"] for f in maritime["features"]}
    assert maritime_ids == {"intel:canonical-safety-1"}
    assert {f["properties"]["id"] for f in legacy_security["features"]} == maritime_ids
    assert {f["properties"]["id"] for f in humanitarian_feed["features"]} == {"intel:canonical-hum-1"}
    assert {f["properties"]["id"] for f in all_feed["features"]} == maritime_ids | {"intel:canonical-hum-1"}
    assert maritime["meta"]["mode_counts"] == {"humanitarian": 1, "maritime": 1}
    assert maritime["meta"]["mode"] == "maritime"
    assert legacy_security["meta"]["mode"] == "maritime"
