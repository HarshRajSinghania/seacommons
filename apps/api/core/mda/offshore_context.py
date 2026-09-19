from __future__ import annotations

from typing import Any

OFFSHORE_COAST_KM = 40.0
OFFSHORE_PORT_KM = 40.0


def build_offshore_context(lat: float, lon: float) -> dict[str, Any]:
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
    return {
        "distance_from_coast_km": coast_km,
        "nearest_port": nearest_port,
        "distance_from_port_km": port_km,
        "in_port_or_anchorage": anchorage,
        "sts_zone": sts_zone,
        "chokepoint": (chokepoint or {}).get("name") if isinstance(chokepoint, dict) else None,
        "offshore": offshore,
        "coastline_source": "Natural Earth lowres (context only)",
    }


def qualify_offshore_anomaly(anomaly_type: str, metadata: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative, explainable offshore evidence-candidate decision.

    This never concludes intent or illegality. It only decides whether a raw anomaly
    is informative enough to surface as a labelled Maritime evidence signal.
    """
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

    if anomaly in {"gap", "long_gap"}:
        gap = metadata.get("gap_reason") or metadata.get("anomaly_evidence") or {}
        silent_s = float(metadata.get("silent_seconds") or gap.get("silent_seconds") or 0.0)
        nearby_before = int(gap.get("nearby_vessels_reporting_before") or gap.get("nearby_vessels_before") or 0)
        nearby_after = int(gap.get("nearby_vessels_reporting_after") or gap.get("nearby_vessels_after") or 0)
        gap_hypothesis = str(gap.get("hypothesis") or "vessel_gap")
        gap_confidence = float(gap.get("confidence") or 0.0)
        jam = float(metadata.get("jamming_score") or 0.0)
        healthy_local_coverage = (
            nearby_before >= 5 and nearby_after >= 5
            and gap_hypothesis != "coverage_gap"
            and gap_confidence >= 0.70
            and jam < 0.3
        )
        baseline_unusual = bool(behaviour_reasons & {"ROUTE_DEVIATION", "UNUSUAL_AIS_SILENCE"})
        prolonged = silent_s >= 4 * 3600 and nearby_before >= 5 and nearby_after >= 5
        qualified = silent_s >= 3600 and healthy_local_coverage and (baseline_unusual or prolonged)
        if healthy_local_coverage:
            reasons.append("LOCAL_AIS_COVERAGE_HEALTHY")
        if prolonged:
            reasons.append("PROLONGED_OFFSHORE_GAP")
        reasons.extend(sorted(behaviour_reasons & {"ROUTE_DEVIATION", "UNUSUAL_AIS_SILENCE"}))
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
