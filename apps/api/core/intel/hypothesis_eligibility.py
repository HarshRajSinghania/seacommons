# SPDX-License-Identifier: AGPL-3.0-or-later
"""Episode-level eligibility for Maritime Intelligence hypotheses."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.intel.hypothesis import evaluate_gate

_FAMILY_HYPOTHESIS_TYPE = {
    "gap_episode": "dark_transit",
    "rendezvous_episode": "covert_rendezvous",
    "spoofing_episode": "position_spoofing",
    "infrastructure_proximity_episode": "infrastructure_pattern",
}
_LOW_SPECIFICITY = frozenset({
    "gap_episode", "rendezvous_episode", "infrastructure_proximity_episode",
})


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    hypothesis_type: Optional[str]
    may_advance_collecting: bool
    evidence_stage: str
    reason_codes: tuple[str, ...]
    counter_indicators: tuple[str, ...]
    explanation: str


def _reason_codes(events: list[Any]) -> tuple[str, ...]:
    return tuple(sorted({
        str((getattr(event, "metadata", {}) or {}).get("anomaly_type") or "")
        for event in events
        if (getattr(event, "metadata", {}) or {}).get("anomaly_type")
    }))


def _counter_indicators(props: dict[str, Any]) -> tuple[str, ...]:
    values = {str(v) for v in (props.get("alternative_explanations") or ()) if v}
    behaviour = props.get("behaviour_context")
    if isinstance(behaviour, dict) and behaviour.get("status") == "expected":
        values.add("BEHAVIOUR_EXPECTED")
    return tuple(sorted(values))


def _event_counter_indicators(events: list[Any]) -> tuple[str, ...]:
    values: set[str] = set()
    for event in events:
        meta = getattr(event, "metadata", {}) or {}
        if meta.get("coincident_teleport_peers"):
            values.add("COINCIDENT_MULTI_VESSEL_POSITION_JUMPS")
        if meta.get("teleport_near_port"):
            values.add("PORT_OR_ANCHORAGE_CONTEXT")
        if meta.get("teleport_pattern") == "transient_outlier":
            values.add("TRANSIENT_POSITION_OUTLIER")
    return tuple(sorted(values))


def _high_specificity_dark_gap_ready(events: list[Any], props: dict[str, Any]) -> bool:
    """A long isolated AIS silence can justify collection, not corroboration.

    This is deliberately stricter than the base dark-transit gate: it requires
    four hours of vessel-specific silence, healthy neighbouring traffic on
    both sides of the gap, no port/anchorage context, no material jamming, and
    a vessel that was underway before disappearing.
    """
    for event in events:
        meta = getattr(event, "metadata", {}) or {}
        if meta.get("anomaly_type") not in {"gap", "long_gap"}:
            continue
        gap = meta.get("gap_reason")
        if not isinstance(gap, dict) or gap.get("hypothesis") != "vessel_gap":
            continue
        if props.get("gap_still_open") is False:
            continue
        effective_silent = props.get("current_silent_seconds")
        if effective_silent is None:
            effective_silent = meta.get("silent_seconds")
        effective_silent = float(effective_silent or 0.0)
        if effective_silent < 4 * 3600 or effective_silent > 12 * 3600:
            continue
        if float(gap.get("confidence") or 0.0) < 0.7:
            continue
        if int(gap.get("nearby_vessels_reporting_before") or 0) < 5:
            continue
        if int(gap.get("nearby_vessels_reporting_after") or 0) < 5:
            continue
        if float(gap.get("coverage_ratio") or 0.0) < 0.7:
            continue
        if float(meta.get("jamming_score") or 0.0) >= 0.3:
            continue
        port_context = meta.get("port_or_anchorage")
        if "port_or_anchorage" not in meta:
            lat, lon = getattr(event, "lat", None), getattr(event, "lon", None)
            if lat is not None and lon is not None:
                from core.mda.reference import reference
                port_context = reference.in_port_or_anchorage(float(lat), float(lon))
        if port_context:
            continue
        speed = meta.get("pre_gap_speed_kn")
        if speed is None:
            speed = ((meta.get("darkship_cue") or {}).get("last_known") or {}).get("speed_kn")
        if float(speed or 0.0) < 2.0:
            continue
        return True
    return False


def _high_specificity_spoofing_ready(events: list[Any]) -> bool:
    """A reproducible teleport is investigation-worthy, not corroborated.

    Circular/frozen tracks remain single-lineage cues until another sensor or
    lineage supports them; they are far too common to auto-open investigations.
    """
    for event in events:
        meta = getattr(event, "metadata", {}) or {}
        if meta.get("anomaly_type") != "position_jump":
            continue
        classification = meta.get("ais_integrity_classification")
        if not isinstance(classification, dict):
            continue
        if (
            classification.get("label") == "position_anomaly"
            and meta.get("teleport_pattern") == "sustained_relocation"
            and not meta.get("coincident_teleport_peers")
            and not meta.get("teleport_near_port")
        ):
            return True
    return False


def _base_gate(hypothesis_type: str, events: list[Any]) -> tuple[bool, str]:
    if hypothesis_type == "dark_transit":
        gap_reasons = [
            (getattr(event, "metadata", {}) or {}).get("gap_reason")
            for event in events
            if isinstance((getattr(event, "metadata", {}) or {}).get("gap_reason"), dict)
        ]
        has_isolated = any(reason.get("hypothesis") == "vessel_gap" for reason in gap_reasons)
        confidence = max((float(reason.get("confidence") or 0.0) for reason in gap_reasons), default=0.0)
        return evaluate_gate(
            "dark_transit",
            has_isolated_gap_feature=has_isolated,
            coverage_confidence=confidence,
        )
    if hypothesis_type == "covert_rendezvous":
        metadata = [getattr(event, "metadata", {}) or {} for event in events]
        has_rendezvous = any(m.get("anomaly_type") in {"ais_rendezvous", "rendezvous", "sts"} for m in metadata)
        irregularities = tuple(sorted({"dark_party" for m in metadata if m.get("dark")}))
        return evaluate_gate(
            "covert_rendezvous",
            has_sustained_rendezvous_episode=has_rendezvous,
            independent_irregularities=irregularities,
        )
    if hypothesis_type == "position_spoofing":
        metadata = [getattr(event, "metadata", {}) or {} for event in events]
        implausible = any(m.get("anomaly_type") in {"position_jump", "circle_spoof", "static_spoof"} for m in metadata)
        classifications = [m.get("ais_integrity_classification") for m in metadata if isinstance(m.get("ais_integrity_classification"), dict)]
        counter_checked = any(c.get("label") not in (None, "not_alertable") for c in classifications)
        return evaluate_gate(
            "position_spoofing",
            implausible_movement=implausible,
            reproducible_inputs=True,
            counter_evidence_checked=counter_checked,
        )
    if hypothesis_type == "infrastructure_pattern":
        metadata = [getattr(event, "metadata", {}) or {} for event in events]
        dwell = any(m.get("loiter_minutes") for m in metadata)
        corroboration = tuple(sorted({"sanctions_match" for m in metadata if m.get("sanctions_matched")}))
        return evaluate_gate(
            "infrastructure_pattern",
            dwell_or_route_repetition=dwell,
            independent_corroboration=corroboration,
        )
    return False, f"no gate wired for hypothesis_type={hypothesis_type!r}"


def evaluate_hypothesis_eligibility(
    episode: dict[str, Any], events: list[Any],
) -> EligibilityDecision:
    props = episode.get("properties") or {}
    family = str(props.get("episode_family") or "")
    hypothesis_type = _FAMILY_HYPOTHESIS_TYPE.get(family)
    reasons = tuple(sorted(set(_reason_codes(events)) | {
        str(v) for v in (props.get("cross_modal_reason_codes") or ()) if v
    }))
    counters = tuple(sorted(set(_counter_indicators(props)) | set(_event_counter_indicators(events))))
    if hypothesis_type is None:
        return EligibilityDecision(False, None, False, "observed", reasons, counters, "episode family has no intelligence hypothesis mapping")

    base_ok, base_reason = _base_gate(hypothesis_type, events)
    if not base_ok:
        return EligibilityDecision(False, hypothesis_type, False, "derived", reasons, counters, base_reason)

    verification_status = str(props.get("verification_status") or "single_source_observed")
    corroborated = verification_status == "multi_source_corroborated"
    investigation_ready = bool(props.get("cross_modal_investigation_ready"))
    high_specificity_dark_gap = (
        hypothesis_type == "dark_transit"
        and _high_specificity_dark_gap_ready(events, props)
    )
    if family in _LOW_SPECIFICITY and not (corroborated or investigation_ready or high_specificity_dark_gap):
        return EligibilityDecision(
            False,
            hypothesis_type,
            False,
            "derived",
            reasons,
            counters,
            "independent corroboration required for low-specificity episode",
        )

    # A physically independent candidate (e.g. unmatched SAR target inside a
    # gap's reachable area) is enough to start collection, not enough to call
    # the vessel identity corroborated or make the allegation publishable.
    evidence_stage = "corroborated" if corroborated else "derived"
    high_specificity_spoof = (
        hypothesis_type == "position_spoofing"
        and _high_specificity_spoofing_ready(events)
    )
    may_advance = (
        corroborated or investigation_ready or high_specificity_spoof or high_specificity_dark_gap
    )
    return EligibilityDecision(
        True,
        hypothesis_type,
        may_advance,
        evidence_stage,
        reasons,
        counters,
        "eligible",
    )
