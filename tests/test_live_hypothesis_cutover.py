from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from core.intel.hypothesis import new_hypothesis, transition
from core.intel.hypothesis_store import save_hypothesis
from core.intel.store import IntelEvent, intel_store
from core.live.feed import public_signal_collection


@pytest.fixture(autouse=True)
def _fresh_hypotheses():
    from core.db.models import InvestigationHypothesisDB
    from core.db.session import session_scope
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
    with intel_store._lock:
        intel_store._events.clear(); intel_store._seen.clear()
    yield
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()


def _publish_spoofing_hypothesis():
    now = datetime.now(timezone.utc).isoformat()
    for event_id, lat, lon in (("spoof-a", 35.0, 14.0), ("spoof-b", 35.1, 14.1)):
        intel_store.add(IntelEvent(
            id=event_id, type="ais_anomaly", severity="high",
            lat=lat, lon=lon, title="raw spoof detector output",
            source="mda", timestamp_utc=now, linked_mmsi="211879870",
            metadata={
                "anomaly_type": "position_jump",
                "maritime_domain": "grey_zone",
                "publication_status": "published",
                "source_policy": "operator_published",
            },
        ), dedup_key=event_id)
    h = new_hypothesis(
        "hyp:test:spoof-published", "position_spoofing",
        ("subj:mmsi:211879870",), episode_id="episode:test:spoof",
    )
    h = replace(
        h, reason_codes=("position_jump",),
        evidence_links=("spoof-a", "spoof-b"), evidence_stage="corroborated",
    )
    for state in ("collecting", "review_ready", "assessed", "published"):
        h = transition(h, state, actor="test")
    save_hypothesis(h)
    return h.hypothesis_id


def test_live_security_uses_published_hypothesis_not_raw_detector_events():
    hypothesis_id = _publish_spoofing_hypothesis()
    collection = public_signal_collection(mode="maritime", days=1, limit=50)
    ids = {feature["properties"]["id"] for feature in collection["features"]}
    assert hypothesis_id in ids
    assert "intel:spoof-a" not in ids
    assert "intel:spoof-b" not in ids
    feature = next(f for f in collection["features"] if f["properties"]["id"] == hypothesis_id)
    assert feature["properties"]["hypothesis_type"] == "position_spoofing"
    assert feature["properties"]["hypothesis_state"] == "published"
    assert feature["properties"]["visual_category"] == "spoofing"
    assert feature["properties"]["verification_status"] == "multi_source_corroborated"
    assert "mmsi" not in feature["properties"]
    assert "linked_mmsi" not in feature["properties"]
