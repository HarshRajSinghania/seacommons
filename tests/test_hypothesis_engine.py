# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md M14.3: InvestigationHypothesis live wiring.

Exit gate, verbatim: "a single AIS observation must never create a
published allegation" and "official sanctions match alone remains an
official-list fact, not sanctions-evasion behaviour."
"""
from __future__ import annotations

import os
import time

os.environ["SEACOMMONS_TRACK_STORE_SYNC"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from core.intel.hypothesis_engine import evaluate_episode
from core.intel.hypothesis_store import get_hypothesis
from core.intel.store import IntelEvent, intel_store


@pytest.fixture(autouse=True)
def _clean():
    from core.db.models import InvestigationHypothesisDB, MaritimeEpisodeDB
    from core.db.session import engine, session_scope

    InvestigationHypothesisDB.__table__.create(bind=engine(), checkfirst=True)
    MaritimeEpisodeDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.query(InvestigationHypothesisDB).delete()
        db.query(MaritimeEpisodeDB).delete()
    with intel_store._lock:
        intel_store._events.clear()
        intel_store._seen.clear()
        intel_store._subscribers.clear()
    yield


def _add_event(event_id, *, mmsi="211879870", anomaly_type="gap", **metadata):
    # title carries event_id so IntelEvent.content_hash() (source:title:text)
    # never collides between two distinct synthetic events in one test --
    # add() would otherwise silently drop the second as a content duplicate.
    intel_store.add(IntelEvent(
        id=event_id, type="ais_anomaly", severity="medium",
        lat=35.5, lon=14.1, title=f"test:{event_id}", linked_mmsi=mmsi,
        source="mda", metadata={"anomaly_type": anomaly_type, **metadata},
    ), dedup_key=event_id)


def _episode(family, *, subject="subj:mmsi:211879870", signal_ids, episode_id="hyp-ep:test"):
    status = "single_source_observed" if len(signal_ids) <= 1 else "single_source_multi_indicator"
    return {"properties": {
        "episode_id": episode_id, "episode_family": family,
        "subject_ids": [subject], "related_signal_ids": list(signal_ids),
        "first_observed_at": "2026-09-06T08:00:00+00:00",
        "last_observed_at": "2026-09-06T08:20:00+00:00",
        "verification_status": status,
        "independence_groups": ["ais_sensor_lineage"],
        "independent_source_count": 1,
        "signal_count": len(signal_ids),
        "evidence_count": len(signal_ids),
        "analysis_state": "evidence_candidate",
    }}


def test_same_lineage_isolated_gap_pair_stays_episode_only():
    """V1 replaces the legacy detector-count promotion with lineage gating."""
    _add_event("gap1", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.6})
    _add_event("gap2", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.55})

    hyp = evaluate_episode(_episode("gap_episode", signal_ids=["gap1", "gap2"]))

    assert hyp is None


def test_coverage_gap_does_not_create_a_dark_transit_hypothesis():
    """docs/fixes.md M14.1/M14.3: a common/port-wide outage's coverage_gap
    classification must not seed an intentional-dark hypothesis either."""
    _add_event("gap3", gap_reason={"hypothesis": "coverage_gap", "confidence": 0.05})

    hyp = evaluate_episode(_episode("gap_episode", signal_ids=["gap3"]))

    assert hyp is None


def test_exit_gate_a_single_low_specificity_observation_stays_episode_only():
    _add_event("gap4", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.6})

    hyp = evaluate_episode(_episode("gap_episode", signal_ids=["gap4"]))

    assert hyp is None


def test_single_sustained_position_relocation_opens_dossier_without_corroboration():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    _add_event(
        "open-spoof-1",
        anomaly_type="position_jump",
        ais_integrity_classification={"label": "position_anomaly", "confidence": 0.9},
        teleport_pattern="sustained_relocation",
        coincident_teleport_peers=[],
        teleport_near_port=False,
    )
    episode_id = "episode:test:open-spoof"
    episode = _episode(
        "spoofing_episode", signal_ids=["open-spoof-1"], episode_id=episode_id
    )

    hyp = evaluate_episode(episode)

    assert hyp is not None
    assert hyp.state == "collecting"
    assert hyp.evidence_stage == "derived"
    with session_scope() as db:
        row = db.get(MaritimeEpisodeDB, episode_id)
        analysis = (row.behaviour_context or {}).get("analysis") or {}
        opening = (row.behaviour_context or {}).get("case_opening") or {}
        assert analysis["publication_state"] == "published"
        assert analysis["analysis_state"] == "evidence_candidate"
        assert "SUSTAINED_POSITION_RELOCATION" in opening["reason_codes"]


def test_single_safety_signal_opens_dossier_but_not_intelligence_hypothesis():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    intel_store.add(
        IntelEvent(
            id="safety-beacon-1",
            type="distress",
            severity="high",
            lat=35.5,
            lon=14.1,
            title="AIS distress beacon",
            source="ais",
            linked_mmsi="970123456",
            metadata={
                "ais_nav_status_kind": "distress_beacon",
                "publication_status": "published",
                "analysis_state": "signal",
                "verification_status": "ais_transponder",
                "beacon_repeat_confirmed": True,
                "episode_update_count": 2,
            },
        ),
        dedup_key="safety-beacon-1",
    )
    episode_id = "episode:test:safety"
    episode = _episode(
        "safety_episode",
        subject="subj:mmsi:970123456",
        signal_ids=["safety-beacon-1"],
        episode_id=episode_id,
    )
    episode["properties"]["analysis_state"] = "signal"

    hyp = evaluate_episode(episode)

    assert hyp is None
    with session_scope() as db:
        row = db.get(MaritimeEpisodeDB, episode_id)
        assert row is not None
        analysis = (row.behaviour_context or {}).get("analysis") or {}
        assert analysis["publication_state"] == "published"
        assert analysis["analysis_state"] == "evidence_candidate"


def test_single_sanctions_sts_dwell_opens_infrastructure_dossier_only():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    _add_event(
        "infra-sanctions-1",
        anomaly_type="sanctions_bunkering_loiter",
        sanctions_matched=True,
        sanctions=[{
            "list": "test-list",
            "matched_on": ["mmsi"],
        }],
        loiter_minutes=135.0,
    )
    episode_id = "episode:test:infra-sanctions"
    episode = _episode(
        "infrastructure_proximity_episode",
        signal_ids=["infra-sanctions-1"],
        episode_id=episode_id,
    )

    hyp = evaluate_episode(episode)

    assert hyp is None
    with session_scope() as db:
        row = db.get(MaritimeEpisodeDB, episode_id)
        analysis = (row.behaviour_context or {}).get("analysis") or {}
        opening = (row.behaviour_context or {}).get("case_opening") or {}
        assert analysis["publication_state"] == "published"
        assert "SUSTAINED_STS_ZONE_DWELL" in opening["reason_codes"]



def test_name_only_sanctions_loiter_does_not_open_public_dossier():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    _add_event(
        "infra-name-only",
        anomaly_type="sanctions_bunkering_loiter",
        sanctions_matched=True,
        sanctions=[{"list": "test-list", "matched_on": ["name"]}],
        loiter_minutes=180.0,
    )
    episode_id = "episode:test:infra-name-only"

    assert evaluate_episode(_episode(
        "infrastructure_proximity_episode",
        signal_ids=["infra-name-only"],
        episode_id=episode_id,
    )) is None

    with session_scope() as db:
        assert db.get(MaritimeEpisodeDB, episode_id) is None


def test_spoofing_candidate_in_coastal_context_stays_internal(monkeypatch):
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.mda import offshore_context

    monkeypatch.setattr(
        offshore_context,
        "build_offshore_context",
        lambda *args, **kwargs: {
            "open_sea": False,
            "in_port_or_anchorage": False,
        },
    )
    _add_event(
        "spoof-coastal",
        anomaly_type="position_jump",
        ais_integrity_classification={
            "label": "position_anomaly",
            "confidence": 0.95,
        },
        teleport_pattern="sustained_relocation",
        coincident_teleport_peers=[],
    )
    episode_id = "episode:test:spoof-coastal"

    assert evaluate_episode(_episode(
        "spoofing_episode",
        signal_ids=["spoof-coastal"],
        episode_id=episode_id,
    )) is None

    with session_scope() as db:
        assert db.get(MaritimeEpisodeDB, episode_id) is None


def test_persistent_open_sea_nuc_opens_safety_dossier_without_claiming_casualty():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    event = IntelEvent(
        id="safety-nuc-open-sea",
        type="vessel_incident",
        severity="medium",
        lat=35.5,
        lon=14.1,
        title="AIS not-under-command state",
        source="ais",
        linked_mmsi="211879999",
        metadata={
            "ais_nav_status_kind": "not_under_command",
            "publication_status": "internal",
            "analysis_state": "observation",
            "verification_status": "ais_transponder",
            "episode_update_count": 4,
        },
    )
    intel_store.add(event, dedup_key=event.id)
    episode_id = "episode:test:safety-nuc"

    assert evaluate_episode(_episode(
        "safety_episode",
        subject="subj:mmsi:211879999",
        signal_ids=[event.id],
        episode_id=episode_id,
    )) is None

    with session_scope() as db:
        row = db.get(MaritimeEpisodeDB, episode_id)
        assert row is not None
        analysis = (row.behaviour_context or {}).get("analysis") or {}
        opening = (row.behaviour_context or {}).get("case_opening") or {}
        assert analysis["publication_state"] == "published"
        assert analysis["analysis_state"] == "evidence_candidate"
        assert "PERSISTENT_AIS_REPORTED_SAFETY_STATE" in opening["reason_codes"]


def test_exit_gate_sanctions_match_alone_never_creates_a_hypothesis():
    """docs/fixes.md M14.3: an identity_integrity_episode (sdn_match,
    mmsi_duplicate, ...) has no wired gate at all -- a bare official-list
    match can never become a hypothesis through this engine, by
    construction, regardless of how much evidence accumulates."""
    _add_event("sdn1", mmsi="273999000", anomaly_type="sdn_match", sanctions_matched=True)
    _add_event("sdn2", mmsi="273999000", anomaly_type="sdn_match", sanctions_matched=True)

    hyp = evaluate_episode(_episode(
        "identity_integrity_episode", subject="subj:mmsi:273999000",
        signal_ids=["sdn1", "sdn2"],
    ))

    assert hyp is None


def test_covert_rendezvous_requires_an_independent_irregularity():
    _add_event("rdv1", mmsi="111000001", anomaly_type="ais_rendezvous", dark=False)

    assert evaluate_episode(_episode(
        "rendezvous_episode", subject="subj:mmsi:111000001", signal_ids=["rdv1"],
    )) is None

    _add_event("rdv2", mmsi="111000001", anomaly_type="ais_rendezvous", dark=True)

    hyp = evaluate_episode(_episode(
        "rendezvous_episode", subject="subj:mmsi:111000001",
        signal_ids=["rdv1", "rdv2"], episode_id="hyp-ep:rdv",
    ))
    assert hyp is None  # dark flag is an indicator, not an independent source


def test_position_spoofing_wires_ais_integrity_classification():
    _add_event(
        "spoof1", mmsi="111000002", anomaly_type="position_jump",
        ais_integrity_classification={"label": "position_anomaly", "confidence": 0.6},
    )
    _add_event(
        "spoof2", mmsi="111000002", anomaly_type="position_jump",
        ais_integrity_classification={"label": "position_anomaly", "confidence": 0.6},
    )

    hyp = evaluate_episode(_episode(
        "spoofing_episode", subject="subj:mmsi:111000002",
        signal_ids=["spoof1", "spoof2"], episode_id="hyp-ep:spoof",
    ))
    assert hyp is not None
    assert hyp.hypothesis_type == "position_spoofing"


def test_infrastructure_pattern_requires_more_than_bare_proximity():
    _add_event(
        "infra1", mmsi="111000003", anomaly_type="loiter", loiter_minutes=90.0,
    )
    assert evaluate_episode(_episode(
        "infrastructure_proximity_episode", subject="subj:mmsi:111000003",
        signal_ids=["infra1"],
    )) is None

    _add_event(
        "infra2", mmsi="111000003", anomaly_type="loiter", loiter_minutes=95.0,
        sanctions_matched=True,
    )
    hyp = evaluate_episode(_episode(
        "infrastructure_proximity_episode", subject="subj:mmsi:111000003",
        signal_ids=["infra1", "infra2"], episode_id="hyp-ep:infra",
    ))
    assert hyp is None  # sanctions flag on the same lineage is not corroboration


def test_end_to_end_isolated_gap_stays_raw_without_materialized_episode():
    """A lone AIS gap remains durable raw evidence, not an Episode wrapper."""
    from core.mda.watch import MdaWatch
    from core.vessels.track_store import track_store

    with track_store._buf_lock:
        track_store._buffer.clear()
    track_store._last.clear()
    track_store._last_write_epoch.clear()
    from core.db.models import VesselTrackDB
    from core.db.session import session_scope
    with session_scope() as db:
        db.query(VesselTrackDB).delete()

    def _witness(mmsi, lat, lon, minutes_ago):
        track_store.on_position(
            mmsi, mmsi, lat, lon, sog=8.0, nav_status=0,
            received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )
        track_store._last_write_epoch[mmsi] = 0.0

    target = "211879870"
    track_store.on_position(target, target, 37.00, 18.00, sog=8.0, nav_status=0,
                             received_at=datetime.now(timezone.utc))
    track_store._last_write_epoch[target] = 0.0
    track_store._last[target].ts = time.time() - 5400  # 90 min silent -- isolated gap

    for k in range(3):
        w_mmsi = f"11100009{k}"
        _witness(w_mmsi, 37.01, 18.01, minutes_ago=100)
        _witness(w_mmsi, 37.01, 18.01, minutes_ago=40)

    w = MdaWatch()
    assert w.scan_gaps() == 1
    assert w.scan_hypotheses() == 0

    from core.db.models import MaritimeEpisodeDB
    with session_scope() as db:
        episodes = db.query(MaritimeEpisodeDB).filter(MaritimeEpisodeDB.episode_family == "gap_episode").all()
        assert episodes == []


def test_v1_single_gap_does_not_create_dark_transit_hypothesis() -> None:
    _add_event("v1-gap-one", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.7})
    episode = _episode("gap_episode", signal_ids=["v1-gap-one"], episode_id="episode:v1:gap-one")
    episode["properties"].update({
        "first_observed_at": "2026-09-06T08:00:00+00:00",
        "last_observed_at": "2026-09-06T08:00:00+00:00",
        "verification_status": "single_source_observed",
        "independence_groups": ["ais_sensor_lineage"],
        "independent_source_count": 1,
    })
    assert evaluate_episode(episode) is None


def test_v1_two_same_lineage_gaps_do_not_create_dark_transit_hypothesis() -> None:
    _add_event("v1-gap-a", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.7})
    _add_event("v1-gap-b", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.65})
    episode = _episode("gap_episode", signal_ids=["v1-gap-a", "v1-gap-b"], episode_id="episode:v1:gap-pair")
    episode["properties"].update({
        "first_observed_at": "2026-09-06T08:00:00+00:00",
        "last_observed_at": "2026-09-06T08:20:00+00:00",
        "verification_status": "single_source_multi_indicator",
        "independence_groups": ["ais_sensor_lineage"],
        "independent_source_count": 1,
    })
    assert evaluate_episode(episode) is None


def test_v1_corroborated_gap_links_persisted_episode() -> None:
    from core.db.models import InvestigationHypothesisDB, MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.intel.store import IntelEvent, intel_store

    _add_event("v1-gap-c", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.7})
    intel_store.add(IntelEvent(
        id="v1-report-c", type="news", severity="medium", lat=35.5, lon=14.1,
        title="independent:v1-report-c", source="Independent report", linked_mmsi="211879870",
        metadata={"anomaly_type": "gap", "transport": "rss"},
    ), dedup_key="v1-report-c")
    episode_id = "episode:v1:gap-corroborated"
    episode = _episode("gap_episode", signal_ids=["v1-gap-c", "v1-report-c"], episode_id=episode_id)
    episode["properties"].update({
        "first_observed_at": "2026-09-06T08:00:00+00:00",
        "last_observed_at": "2026-09-06T08:20:00+00:00",
        "verification_status": "multi_source_corroborated",
        "independence_groups": ["ais_sensor_lineage", "secondary_news_reporting"],
        "independent_source_count": 2,
    })
    hyp = evaluate_episode(episode)
    assert hyp is not None
    assert hyp.episode_id == episode_id
    assert hyp.hypothesis_id == f"hyp:v1:dark_transit:{episode_id}"
    # Independent two-lineage corroboration clears the same evidence bar
    # can_publish() re-verifies, so the automatic engine (2026-09-21 product
    # decision, docs/current_work.md) carries it all the way to "published"
    # itself -- no human analyst transition() call is required or possible.
    assert hyp.state == "published"
    assert hyp.evidence_stage == "corroborated"
    assert [entry.new_state for entry in hyp.audit_history[-4:]] == [
        "collecting", "review_ready", "assessed", "published",
    ]
    assert all(entry.actor == "hypothesis_engine_v1" for entry in hyp.audit_history[-4:])
    with session_scope() as db:
        assert db.query(MaritimeEpisodeDB).filter_by(episode_id=episode_id).count() == 1
        row = db.query(InvestigationHypothesisDB).filter_by(hypothesis_id=hyp.hypothesis_id).one()
        assert row.episode_id == episode_id


def test_v1_reproducible_teleport_enters_collecting_as_derived() -> None:
    for event_id in ("v1-spoof-a", "v1-spoof-b"):
        _add_event(
            event_id,
            anomaly_type="position_jump",
            ais_integrity_classification={"label": "position_anomaly", "confidence": 0.8},
            teleport_pattern="sustained_relocation",
        )
    episode = _episode(
        "spoofing_episode",
        signal_ids=["v1-spoof-a", "v1-spoof-b"],
        episode_id="episode:v1:spoof-one-lineage",
    )
    episode["properties"].update({
        "first_observed_at": "2026-09-06T08:00:00+00:00",
        "last_observed_at": "2026-09-06T08:10:00+00:00",
        "verification_status": "single_source_multi_indicator",
        "independence_groups": ["ais_sensor_lineage"],
        "independent_source_count": 1,
    })
    hyp = evaluate_episode(episode)
    assert hyp is not None
    assert hyp.hypothesis_type == "position_spoofing"
    assert hyp.state == "collecting"
    assert hyp.evidence_stage == "derived"


def test_v1_engine_never_relinks_or_mutates_legacy_null_episode_hypothesis() -> None:
    from core.db.models import InvestigationHypothesisDB
    from core.db.session import session_scope
    from core.intel.store import IntelEvent, intel_store

    episode_id = "episode:v1:legacy-isolation"
    legacy_id = f"hyp:dark_transit:{episode_id}"
    with session_scope() as db:
        db.add(InvestigationHypothesisDB(
            hypothesis_id=legacy_id,
            episode_id=None,
            hypothesis_type="dark_transit",
            subject_ids=["subj:mmsi:211879870"],
            state="candidate",
            reason_codes=["legacy-gap"],
            counter_indicators=[],
            evidence_links=["legacy-evidence"],
            evidence_stage="derived",
            audit_history=[],
        ))

    _add_event("v1-legacy-gap", gap_reason={"hypothesis": "vessel_gap", "confidence": 0.8})
    intel_store.add(IntelEvent(
        id="v1-legacy-report", type="news", severity="medium", lat=35.5, lon=14.1,
        title="independent legacy isolation", source="Independent report",
        linked_mmsi="211879870",
        metadata={"anomaly_type": "gap", "transport": "rss"},
    ), dedup_key="v1-legacy-report")
    episode = _episode(
        "gap_episode",
        signal_ids=["v1-legacy-gap", "v1-legacy-report"],
        episode_id=episode_id,
    )
    episode["properties"].update({
        "first_observed_at": "2026-09-06T08:00:00+00:00",
        "last_observed_at": "2026-09-06T08:20:00+00:00",
        "verification_status": "multi_source_corroborated",
        "independence_groups": ["ais_sensor_lineage", "secondary_news_reporting"],
        "independent_source_count": 2,
    })

    new_hyp = evaluate_episode(episode)
    assert new_hyp is not None
    assert new_hyp.hypothesis_id == f"hyp:v1:dark_transit:{episode_id}"
    assert new_hyp.episode_id == episode_id
    assert new_hyp.state == "published"
    with session_scope() as db:
        legacy = db.get(InvestigationHypothesisDB, legacy_id)
        assert legacy is not None
        assert legacy.episode_id is None
        assert legacy.reason_codes == ["legacy-gap"]
        assert legacy.evidence_links == ["legacy-evidence"]


def test_rendezvous_event_preserves_all_vessel_subjects():
    from core.intel.hypothesis_engine import event_to_episode_input_feature

    event = IntelEvent(
        id="rdv-pair", type="ais_rendezvous", severity="medium",
        lat=35.0, lon=17.0, title="pair", source="GFW",
        metadata={
            "anomaly_type": "ais_rendezvous",
            "vessels": ["247123456", "255987654"],
        },
    )
    feature = event_to_episode_input_feature(event)
    assert feature is not None
    assert feature["properties"]["subject_ids"] == [
        "subj:mmsi:247123456", "subj:mmsi:255987654"
    ]


def test_episode_input_uses_registry_imo_like_public_live(monkeypatch):
    from core.intel.hypothesis_engine import event_to_episode_input_feature
    from core.vessels.registry import registry

    mmsi = "211879871"
    monkeypatch.setitem(registry._cache, mmsi, {"imo": "9872092"})
    event = IntelEvent(
        id="imo-aligned-gap", type="ais_anomaly", severity="high",
        lat=35.4, lon=14.2, title="gap", source="mda",
        linked_mmsi=mmsi, metadata={"anomaly_type": "long_gap"},
    )

    feature = event_to_episode_input_feature(event)

    assert feature is not None
    assert feature["properties"]["subject_ids"] == ["subj:imo:9872092"]
    assert feature["properties"]["imo"] == "9872092"


def test_durable_gfw_gap_is_not_independent_from_ais_gap(monkeypatch):
    from core.mda.watch import MdaWatch

    ais = IntelEvent(
        id="durable-ais-gap", type="ais_anomaly", severity="medium",
        lat=35.5, lon=14.1, title="isolated AIS gap", source="mda",
        linked_mmsi="211879870", metadata={
            "anomaly_type": "gap",
            "gap_reason": {"hypothesis": "vessel_gap", "confidence": 0.8},
        },
    )
    gfw = IntelEvent(
        id="durable-gfw-gap", type="ais_anomaly", severity="medium",
        lat=35.51, lon=14.11, title="GFW gap", source="GFW",
        linked_mmsi="211879870", metadata={"anomaly_type": "long_gap"},
    )
    by_id = {ais.id: ais, gfw.id: gfw}
    monkeypatch.setattr(intel_store, "events", lambda *a, **k: [])
    monkeypatch.setattr(intel_store, "persisted_events", lambda *a, **k: [ais, gfw])
    monkeypatch.setattr(intel_store, "get_durable", lambda event_id: by_id.get(event_id))

    assert MdaWatch().scan_hypotheses() == 0
    assert get_hypothesis(
        "hyp:v1:dark_transit:episode:subj:mmsi:211879870:gap_episode:1"
    ) is None


def test_hypothesis_sampler_never_evicts_public_evidence_candidate():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    from core.mda.watch import MdaWatch

    now = datetime.now(timezone.utc).isoformat()
    priority_id = "sampler-public-priority-gap"
    with session_scope() as db:
        db.add(IntelEventDB(
            id=priority_id, timestamp_utc=now, type="ais_anomaly",
            severity="high", lat=35.4, lon=14.2, title="public candidate",
            text="", url="", source="mda", linked_mmsi="211879873",
            maritime_domain="grey_zone", meta={
                "anomaly_type": "long_gap",
                "silent_seconds": 4 * 3600,
                "publication_status": "published",
                "analysis_state": "evidence_candidate",
            },
        ))
        db.add(IntelEventDB(
            id="sampler-internal-louder-gap", timestamp_utc=now,
            type="ais_anomaly", severity="high", lat=35.5, lon=14.3,
            title="internal louder gap", text="", url="", source="mda",
            linked_mmsi="211879874", maritime_domain="grey_zone", meta={
                "anomaly_type": "long_gap",
                "silent_seconds": 48 * 3600,
                "publication_status": "internal",
                "analysis_state": "anomaly",
            },
        ))

    sampled = MdaWatch._durable_hypothesis_family_events(limit_per_family=1)

    assert priority_id in {event.id for event in sampled}


def test_unmatched_satellite_candidate_stays_context_and_does_not_promote():
    gap = IntelEvent(
        id="gap-with-sar", type="ais_anomaly", severity="high",
        lat=35.5, lon=14.1, title="isolated AIS gap", source="mda",
        linked_mmsi="211879870", metadata={
            "anomaly_type": "gap",
            "gap_reason": {"hypothesis": "vessel_gap", "confidence": 0.8},
            "darkship_cue": {
                "association_status": "unmatched_candidate",
                "gfw_unmatched_in_area": [
                    {"lat": 35.53, "lon": 14.15, "timestamp": "2026-09-17T10:15:00+00:00"}
                ],
            },
        },
    )
    intel_store.add(gap, dedup_key=gap.id)
    hyp = evaluate_episode(_episode(
        "gap_episode", signal_ids=[gap.id], episode_id="episode:dark:sar",
    ))
    # A detection merely inside the reachable area is context. It does not
    # create an independent corroboration lineage or advance the hypothesis.
    assert hyp is None

    # The unmatched SAR detection is still materialized as proper, persisted
    # contextual satellite evidence, never claiming a vessel match.
    from core.intel.satellite_observation import list_incident_observations

    observations = list_incident_observations(gap.id)
    assert len(observations) == 1
    assert observations[0].association_status == "unmatched_candidate"
    assert observations[0].provider == "gfw"
    assert observations[0].footprint == {"type": "Point", "coordinates": [14.15, 35.53]}


def test_durable_hypothesis_sampler_keeps_gap_family_under_spoof_flood():
    from datetime import datetime, timezone

    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    from core.mda.watch import MdaWatch

    now = datetime.now(timezone.utc).isoformat()
    with session_scope() as db:
        for i in range(40):
            db.add(IntelEventDB(
                id=f"sampler-spoof-{i}", timestamp_utc=now, type="ais_anomaly",
                severity="medium", lat=35.0, lon=15.0, title="spoof", source="mda",
                linked_mmsi=f"21188{i:04d}"[-9:],
                meta={"anomaly_type": "position_jump", "maritime_domain": "grey_zone"},
            ))
        db.add(IntelEventDB(
            id="sampler-gap", timestamp_utc=now, type="ais_anomaly", severity="medium",
            lat=36.0, lon=16.0, title="gap", source="mda", linked_mmsi="211879870",
            meta={"anomaly_type": "gap", "maritime_domain": "grey_zone"},
        ))
    sampled = MdaWatch._durable_hypothesis_family_events(limit_per_family=5)
    ids = {event.id for event in sampled}
    assert "sampler-gap" in ids
    assert len([event for event in sampled if (event.metadata or {}).get("anomaly_type") == "position_jump"]) <= 5


def test_legacy_short_gap_is_telemetry_not_hypothesis_episode_input():
    from core.intel.hypothesis_engine import event_to_episode_input_feature

    legacy = IntelEvent(
        id="aisanom:211879870:gap",
        type="ais_anomaly",
        severity="medium",
        lat=35.5,
        lon=14.1,
        title="legacy short gap",
        linked_mmsi="211879870",
        source="ais",
        metadata={"anomaly_type": "gap"},
    )
    canonical = IntelEvent(
        id="aisgap:211879870",
        type="ais_anomaly",
        severity="medium",
        lat=35.5,
        lon=14.1,
        title="canonical MDA gap",
        linked_mmsi="211879870",
        source="mda",
        metadata={"anomaly_type": "long_gap"},
    )

    assert event_to_episode_input_feature(legacy) is None
    assert event_to_episode_input_feature(canonical) is not None


def test_unclassified_episode_is_not_persisted_as_analysis():
    _add_event("unknown1", anomaly_type="unknown_pattern")
    _add_event("unknown2", anomaly_type="unknown_pattern")
    hyp = evaluate_episode(_episode("unclassified_episode", signal_ids=["unknown1", "unknown2"]))
    assert hyp is None

    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope
    with session_scope() as db:
        assert db.query(MaritimeEpisodeDB).count() == 0
