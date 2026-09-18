# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live Observation → persisted Episode → InvestigationHypothesis wiring.

V1 persists only meaningful maritime episodes before hypothesis evaluation.
Single-observation detector wrappers remain durable as IntelEvent /
SourceObservation until corroboration or aggregation promotes them.
Low-specificity hypotheses require the episode-level independent-evidence
gate; detector count never substitutes for source independence. High-
specificity spoofing may remain a candidate on one lineage but cannot
advance to collecting without corroboration.

Legacy hypothesis rows are never relinked. New rows use a versioned ID
(`hyp:v1:...`) and always carry a non-null episode_id.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from core.intel.episode_store import save_episode
from core.intel.hypothesis import InvestigationHypothesis, new_hypothesis, transition
from core.intel.hypothesis_eligibility import evaluate_hypothesis_eligibility
from core.intel.hypothesis_store import get_hypothesis, save_hypothesis
from core.intel.store import IntelEvent, intel_store


def _event_mmsis(event: IntelEvent) -> tuple[str, ...]:
    metadata = event.metadata or {}
    candidates: list[Any] = [event.linked_mmsi, metadata.get("mmsi")]
    for vessel in metadata.get("vessels") or ():
        if isinstance(vessel, dict):
            candidates.extend((vessel.get("mmsi"), vessel.get("ssvid")))
        else:
            candidates.append(vessel)
    return tuple(dict.fromkeys(
        text for value in candidates
        if len((text := str(value or "").strip())) == 9 and text.isdigit()
    ))


def _attach_cross_modal_evidence(
    episode: dict[str, Any], events: list[IntelEvent],
) -> dict[str, Any]:
    """Attach independent sensor evidence without turning it into a verdict.

    A dark-ship cue can move a case into investigation only when an AIS
    lineage and a physically independent satellite detection are both present.
    An unmatched SAR detection remains a candidate, never corroborated identity.
    """
    from copy import deepcopy
    from datetime import datetime, timezone

    from core.evidence.cross_modal import CrossModalEvidencePacket, EvidenceReference
    from core.evidence.cross_modal_analysis import evaluate_independence
    from core.intel.lifecycle import parse_utc

    refs: list[EvidenceReference] = []
    reason_codes: set[str] = set()
    for event in events:
        observed = parse_utc(event.timestamp_utc) or datetime.now(timezone.utc)
        if event.type in {"ais_anomaly", "ais_rendezvous", "ais_spike"}:
            confidence = float(((event.metadata or {}).get("gap_reason") or {}).get("confidence") or 0.5)
            refs.append(EvidenceReference(
                evidence_id=f"ais:{event.id}", evidence_class="ais_observation",
                source_lineage="ais_sensor_lineage", modality="ais",
                observed_at=observed, confidence=max(0.0, min(1.0, confidence)),
            ))
        cue = (event.metadata or {}).get("darkship_cue") or {}
        if cue.get("association_status") != "unmatched_candidate":
            continue
        for detection in cue.get("gfw_unmatched_in_area") or ():
            if not isinstance(detection, dict):
                continue
            det_at = parse_utc(str(detection.get("timestamp") or "")) or observed
            lat = detection.get("lat")
            lon = detection.get("lon")
            evidence_id = f"sat:gfw_sar:{det_at.isoformat()}:{lat}:{lon}"
            refs.append(EvidenceReference(
                evidence_id=evidence_id, evidence_class="satellite_observation",
                source_lineage="gfw_sar", modality="satellite",
                observed_at=det_at, confidence=0.45,
            ))
            reason_codes.add("SATELLITE_CANDIDATE_IN_REACHABLE_AREA")

    if not refs:
        return episode
    subject_ids = tuple(str(v) for v in ((episode.get("properties") or {}).get("subject_ids") or ()) if v)
    if not subject_ids:
        return episode
    packet = CrossModalEvidencePacket(subject_id=subject_ids[0], evidence=tuple(refs))
    assessment = evaluate_independence(packet)
    updated = deepcopy(episode)
    props = updated.setdefault("properties", {})
    props["cross_modal_packet_id"] = packet.packet_id
    props["cross_modal_evidence_ids"] = [ref.evidence_id for ref in packet.evidence]
    props["cross_modal_modalities"] = list(assessment.modalities)
    props["cross_modal_independence_groups"] = list(assessment.independence_groups)
    props["cross_modal_reason_codes"] = sorted(reason_codes)
    props["cross_modal_investigation_ready"] = (
        assessment.independent_group_count >= 2
        and {"ais", "satellite"}.issubset(set(assessment.modalities))
    )
    return updated


def event_to_episode_input_feature(event: IntelEvent) -> Optional[dict[str, Any]]:
    """Build the internal feature used by the bounded episode builder."""
    from core.intel.analysis_state import annotate_event_analysis

    annotate_event_analysis(event)
    mmsis = _event_mmsis(event)
    if not mmsis:
        return None
    mmsi = mmsis[0]
    from core.mda.vessel_subject import subject_id_for
    subject_ids = tuple(
        subject_id_for(mmsi=value) or f"subj:mmsi:{value}" for value in mmsis
    )
    coordinates = [event.lon, event.lat] if event.lat is not None and event.lon is not None else []
    metadata = event.metadata or {}
    parent_ids = tuple(str(v) for v in (metadata.get("contributing") or ()) if v)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coordinates},
        "properties": {
            "id": event.id,
            "timestamp_utc": event.timestamp_utc,
            "linked_mmsi": mmsi,
            "subject_ids": list(subject_ids),
            "anomaly_type": metadata.get("anomaly_type"),
            "ais_nav_status_kind": metadata.get("ais_nav_status_kind"),
            "episode_family": metadata.get("episode_family"),
            "severity": event.severity,
            "source": event.source,
            "maritime_domain": event.maritime_domain(),
            "verification_status": metadata.get("verification_status"),
            "sanctions_matched": metadata.get("sanctions_matched"),
            "sanctions": metadata.get("sanctions"),
            "port_call": metadata.get("port_call"),
            "contributing_independence_groups": metadata.get(
                "contributing_independence_groups"
            ),
            "observation_ids": list(parent_ids or (event.id,)),
            "feature_ids": [event.id] if parent_ids else [],
            "incident_lifecycle": metadata.get("incident_lifecycle"),
            "behaviour_context": metadata.get("behaviour_context"),
            "alternative_explanations": metadata.get("alternative_explanations"),
            "analysis_state": metadata.get("analysis_state"),
            "publication_state": metadata.get("publication_state"),
            "resolution_state": metadata.get("resolution_state"),
            "lineage_ids": metadata.get("lineage_ids"),
            "gap_still_open": metadata.get("gap_still_open"),
            "current_silent_seconds": metadata.get("current_silent_seconds"),
        },
    }



def _should_persist_episode(props: dict[str, Any]) -> bool:
    """Persist aggregation, not one-detector wrappers. Raw signals stay durable."""
    family = str(props.get("episode_family") or "unclassified_episode")
    signal_count = int(props.get("signal_count") or 0)
    evidence_count = int(props.get("evidence_count") or 0)
    independent = int(props.get("independent_source_count") or 0)
    verification = str(props.get("verification_status") or "")
    analysis_state = str(props.get("analysis_state") or "")
    if family in {"safety_episode", "port_call_episode"} and analysis_state in {"signal", "evidence"}:
        return True
    if bool(props.get("cross_modal_investigation_ready")):
        return True
    if verification == "multi_source_corroborated" or independent >= 2:
        return True
    if signal_count >= 2 and evidence_count >= 2:
        return True
    return False

def evaluate_episode(episode: dict[str, Any]) -> Optional[InvestigationHypothesis]:
    """Persist the episode, then create/update a v1 hypothesis only when eligible."""
    props = episode.get("properties") or {}
    episode_id = str(props.get("episode_id") or "")
    subject_ids = tuple(str(s) for s in (props.get("subject_ids") or ()) if s)
    if not episode_id or not subject_ids:
        return None

    signal_ids = tuple(str(s) for s in (props.get("related_signal_ids") or ()) if s)
    events = [e for e in (intel_store.get_durable(sid) for sid in signal_ids) if e is not None]
    if events:
        episode = _attach_cross_modal_evidence(episode, events)
        props = episode.get("properties") or {}

    # SourceObservation/IntelEvent are the durable anomaly archive. An episode
    # is materialised only when it adds aggregation or corroboration.
    if not _should_persist_episode(props):
        return None
    save_episode(episode)
    from core.observability import record_maritime_episode_evaluation

    record_maritime_episode_evaluation(
        str(props.get("episode_family") or "unclassified_episode"),
        str(props.get("verification_status") or "single_source_observed"),
    )
    if not events:
        return None

    decision = evaluate_hypothesis_eligibility(episode, events)
    from core.observability import record_v1_hypothesis_decision

    record_v1_hypothesis_decision(
        str(decision.hypothesis_type or "none"),
        "eligible" if decision.eligible else "ineligible",
    )
    if not decision.eligible or decision.hypothesis_type is None:
        return None

    hypothesis_type = decision.hypothesis_type
    hypothesis_id = f"hyp:v1:{hypothesis_type}:{episode_id}"
    existing = get_hypothesis(hypothesis_id)
    if existing is None:
        hyp = new_hypothesis(
            hypothesis_id,
            hypothesis_type,
            subject_ids,
            episode_id=episode_id,
        )
    else:
        if existing.episode_id != episode_id:
            raise ValueError("v1 hypothesis episode identity mismatch")
        hyp = existing

    evidence_links = tuple(dict.fromkeys((
        *signal_ids,
        *(str(v) for v in (props.get("cross_modal_evidence_ids") or ()) if v),
    )))
    hyp = replace(
        hyp,
        reason_codes=decision.reason_codes,
        counter_indicators=decision.counter_indicators,
        evidence_links=evidence_links,
        evidence_stage=decision.evidence_stage,
    )

    if hyp.state == "candidate" and decision.may_advance_collecting:
        from core.observability import record_hypothesis_transition

        hyp = transition(hyp, "collecting", actor="hypothesis_engine_v1")
        record_hypothesis_transition(hyp.hypothesis_type, hyp.state)

    # Crossing into review_ready is deliberately narrower than entering
    # collection. High-specificity single-lineage AIS cues and unmatched SAR
    # candidates may justify investigation, but only independently
    # corroborated evidence can become a reviewable public case.
    distinct_evidence = {str(value) for value in hyp.evidence_links if value}
    if (
        hyp.state == "collecting"
        and decision.evidence_stage in {"corroborated", "assessed", "confirmed"}
        and len(distinct_evidence) >= 2
        and bool(hyp.reason_codes)
    ):
        from core.observability import record_hypothesis_transition

        hyp = transition(hyp, "review_ready", actor="hypothesis_engine_v1")
        record_hypothesis_transition(hyp.hypothesis_type, hyp.state)

    save_hypothesis(hyp)
    return hyp
