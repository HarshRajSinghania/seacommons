from __future__ import annotations

from typing import Any

OFFSHORE_COAST_KM = 40.0
OFFSHORE_PORT_KM = 40.0

_PASSENGER_HSC_TYPES = frozenset((*range(40, 50), *range(60, 70)))
_LOW_SPECIFICITY_FAMILIES = frozenset({
    "gap", "long_gap", "circular_pattern", "static_position_inconsistency",
    "rendezvous", "ais_rendezvous", "sts", "loiter", "infra_proximity",
})


def _coerce_ship_type(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _has_passenger_hsc_context(metadata: dict[str, Any]) -> bool:
    primary = _coerce_ship_type(
        metadata.get("vessel_type_context", metadata.get("ship_type"))
    )
    if primary in _PASSENGER_HSC_TYPES:
        return True
    raw_types = metadata.get("vessel_type_contexts") or ()
    if not isinstance(raw_types, (list, tuple, set, frozenset)):
        return False
    types = [_coerce_ship_type(value) for value in raw_types]
    known = [value for value in types if value is not None]
    return bool(known) and len(known) == len(types) and all(
        value in _PASSENGER_HSC_TYPES for value in known
    )


def _healthy_prolonged_gap(
    metadata: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    gap = metadata.get("gap_reason") or metadata.get("anomaly_evidence") or {}
    silent_s = float(metadata.get("silent_seconds") or gap.get("silent_seconds") or 0.0)
    nearby_before = int(gap.get("nearby_vessels_reporting_before") or gap.get("nearby_vessels_before") or 0)
    nearby_after = int(gap.get("nearby_vessels_reporting_after") or gap.get("nearby_vessels_after") or 0)
    confidence = float(gap.get("confidence") or 0.0)
    jam = float(metadata.get("jamming_score") or 0.0)
    neighbour_coverage = nearby_before >= 5 and nearby_after >= 5
    community_coverage = bool(context.get("ais_coverage_witnesses"))
    return (
        silent_s >= 4 * 3600
        and (neighbour_coverage or community_coverage)
        and str(gap.get("hypothesis") or "vessel_gap") != "coverage_gap"
        and confidence >= 0.70
        and jam < 0.3
    )


def anomaly_context_suppression(
    anomaly_type: str,
    metadata: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, ...]:
    """Explain a class/baseline false-positive without granting immunity.

    This gate applies only to low-specificity detector families. Dedicated
    distress, independent evidence, sanctions identity, sustained relocation,
    and prolonged vessel-specific silence bypass it.
    """
    anomaly = str(anomaly_type or "").lower()
    if not _has_passenger_hsc_context(metadata) or anomaly not in _LOW_SPECIFICITY_FAMILIES:
        return ()

    from core.domain.incident_taxonomy import is_independently_corroborated

    if (
        str(metadata.get("ais_nav_status_kind") or "") == "distress_beacon"
        or metadata.get("is_distress")
        or metadata.get("sanctions_matched")
        or is_independently_corroborated(metadata)
        or _healthy_prolonged_gap(metadata, context)
        or (
            anomaly in {"position_jump", "impossible_speed", "teleport"}
            and str(metadata.get("teleport_pattern") or "") == "sustained_relocation"
        )
    ):
        return ()

    behaviour = metadata.get("behaviour_context") or {}
    behaviour_status = str(behaviour.get("status") or "") if isinstance(behaviour, dict) else ""
    scheduled = bool(
        metadata.get("scheduled_service")
        or metadata.get("routine_scheduled_traffic")
        or metadata.get("scheduled_traffic")
    )
    terminal = bool(
        metadata.get("terminal_manoeuvre")
        or metadata.get("terminal_maneuver")
        or metadata.get("port_or_anchorage")
        or context.get("in_port_or_anchorage")
    )
    class_contextual_family = anomaly in {
        "gap", "long_gap", "rendezvous", "ais_rendezvous", "sts",
        "circular_pattern", "static_position_inconsistency",
    }
    if not (scheduled or terminal or behaviour_status == "expected" or class_contextual_family):
        return ()
    reasons = ["PASSENGER_HSC_LOW_SPECIFICITY"]
    if scheduled:
        reasons.append("ROUTINE_SCHEDULED_TRAFFIC")
    if behaviour_status == "expected":
        reasons.append("BASELINE_ROUTE_CONSISTENT")
    if terminal:
        reasons.append("TERMINAL_MANOEUVRE_CONTEXT")
    return tuple(reasons)


def _vessel_type_context(metadata: dict[str, Any]) -> int | None:
    raw = metadata.get("vessel_type_context", metadata.get("ship_type"))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_low_specificity_scheduled_context(metadata: dict[str, Any]) -> bool:
    # Public-qualification control only: raw detector evidence stays durable.
    ship_type = _vessel_type_context(metadata)
    if ship_type not in _PASSENGER_HSC_TYPES:
        return False
    behaviour = metadata.get("behaviour_context") or {}
    reasons = set(behaviour.get("reason_codes") or ()) if isinstance(behaviour, dict) else set()
    return not bool(reasons & {"ROUTE_DEVIATION", "UNUSUAL_AIS_SILENCE"})


def build_offshore_context(
    lat: float,
    lon: float,
    *,
    include_ais_coverage: bool = False,
) -> dict[str, Any]:
    from core.mda.reference import reference

    nearest_port, port_km = reference.nearest_port_km(lat, lon)
    coast_km = reference.distance_from_coast_km(lat, lon)
    anchorage = reference.in_port_or_anchorage(lat, lon)
    sts_zone = reference.in_sts_zone(lat, lon)
    chokepoint = reference.chokepoint_of(lat, lon)
    offshore = (
        coast_km is not None
        and coast_km >= OFFSHORE_COAST_KM
        and port_km >= OFFSHORE_PORT_KM
        and not anchorage
    )
    coverage = []
    if include_ais_coverage:
        try:
            from core.mda.ais_coverage import coverage_witnesses

            coverage = coverage_witnesses(lat, lon)
        except Exception:
            coverage = []
    return {
        "distance_from_coast_km": coast_km,
        "nearest_port": nearest_port,
        "distance_from_port_km": port_km,
        "in_port_or_anchorage": anchorage,
        "sts_zone": sts_zone,
        "chokepoint": (chokepoint or {}).get("name") if isinstance(chokepoint, dict) else None,
        "offshore": offshore,
        "ais_coverage_witnesses": coverage,
        "coastline_source": "Natural Earth lowres (context only)",
    }


def qualify_offshore_anomaly(anomaly_type: str, metadata: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative, explainable offshore evidence-candidate decision.

    This never concludes intent or illegality. It only decides whether a raw anomaly
    is informative enough to surface as a labelled Maritime evidence signal.
    """
    suppression_reasons = anomaly_context_suppression(anomaly_type, metadata, context)
    if suppression_reasons:
        return {
            "qualified": False,
            "reason_codes": list(suppression_reasons),
            "stage": "anomaly",
            "rationale": (
                "Observation is consistent with routine passenger or high-speed "
                "scheduled traffic context; it remains durable internal evidence."
            ),
        }
    if not context.get("offshore"):
        return {
            "qualified": False,
            "reason_codes": ["NOT_OFFSHORE"],
            "stage": "anomaly",
            "rationale": (
                "Observation is not offshore under the current coast/port context gate; "
                "it remains internal context and is not promoted to Live."
            ),
        }

    reasons: list[str] = ["OFFSHORE_CONTEXT"]
    qualified = False
    anomaly = str(anomaly_type or "").lower()
    behaviour = metadata.get("behaviour_context") or {}
    behaviour_reasons = set(behaviour.get("reason_codes") or ()) if isinstance(behaviour, dict) else set()
    coverage_witnesses = context.get("ais_coverage_witnesses") or []
    if coverage_witnesses:
        # Same AIS sensor family: useful evidence about receiver coverage, but
        # never an independent corroborating lineage for vessel behaviour.
        reasons.append("COMMUNITY_AIS_COVERAGE_PRESENT")

    if anomaly in {"gap", "long_gap"}:
        gap = metadata.get("gap_reason") or metadata.get("anomaly_evidence") or {}
        silent_s = float(metadata.get("silent_seconds") or gap.get("silent_seconds") or 0.0)
        nearby_before = int(gap.get("nearby_vessels_reporting_before") or gap.get("nearby_vessels_before") or 0)
        nearby_after = int(gap.get("nearby_vessels_reporting_after") or gap.get("nearby_vessels_after") or 0)
        gap_hypothesis = str(gap.get("hypothesis") or "vessel_gap")
        gap_confidence = float(gap.get("confidence") or 0.0)
        jam = float(metadata.get("jamming_score") or 0.0)
        neighbour_coverage = nearby_before >= 5 and nearby_after >= 5
        community_coverage = bool(coverage_witnesses)
        healthy_local_coverage = (
            (neighbour_coverage or community_coverage)
            and gap_hypothesis != "coverage_gap"
            and gap_confidence >= 0.70
            and jam < 0.3
        )
        baseline_unusual = bool(behaviour_reasons & {"ROUTE_DEVIATION", "UNUSUAL_AIS_SILENCE"})
        prolonged = silent_s >= 4 * 3600 and healthy_local_coverage
        qualified = silent_s >= 3600 and healthy_local_coverage and (baseline_unusual or prolonged)
        if healthy_local_coverage:
            reasons.append("LOCAL_AIS_COVERAGE_HEALTHY")
        if prolonged:
            reasons.append("PROLONGED_OFFSHORE_GAP")
        reasons.extend(sorted(behaviour_reasons & {"ROUTE_DEVIATION", "UNUSUAL_AIS_SILENCE"}))
        if (
            qualified
            and not baseline_unusual
            and silent_s < 6 * 3600
            and _is_low_specificity_scheduled_context(metadata)
        ):
            qualified = False
            reasons.extend([
                "PASSENGER_HSC_LOW_SPECIFICITY",
                "BASELINE_ROUTE_CONSISTENT",
            ])
    elif anomaly in {"ais_rendezvous", "rendezvous", "sts"}:
        duration = float(metadata.get("duration_min") or 0.0)
        dark = bool(metadata.get("dark"))
        tanker = bool(metadata.get("tanker"))
        qualified = duration >= 60 and (dark or tanker)
        if duration >= 60:
            reasons.append("SUSTAINED_OFFSHORE_RENDEZVOUS")
        if dark:
            reasons.append("AIS_GAP_CONTEXT_ON_PARTY")
        if tanker:
            reasons.append("TANKER_PARTICIPANT")
    elif anomaly in {"impossible_speed", "position_jump", "teleport"}:
        confidence = float(metadata.get("anomaly_confidence") or metadata.get("confidence") or 0.0)
        evidence = metadata.get("anomaly_evidence") or {}
        temporal_separation_s = float(evidence.get("gap_s") or metadata.get("gap_s") or 0.0)
        qualified = confidence >= 0.8 and temporal_separation_s >= 30.0
        if qualified:
            reasons.extend([
                "HIGH_CONFIDENCE_POSITION_INTEGRITY_ANOMALY",
                "TEMPORALLY_SEPARATED_FIXES",
            ])

    return {
        "qualified": qualified,
        "reason_codes": list(dict.fromkeys(reasons)),
        "stage": "evidence_candidate" if qualified else "anomaly",
        "rationale": (
            "Offshore anomaly passed contextual gates; it is evidence for investigation, not proof of intent."
            if qualified else
            "Offshore observation retained as an anomaly; contextual evidence is not yet strong enough for Live."
        ),
    }
