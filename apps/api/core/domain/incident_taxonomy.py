# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public product taxonomy for SeaCommons incidents.

The analytical pipeline keeps detailed internal domains, confidence, severity,
verification and sensor lineage. Public Live/Play expose a simpler orthogonal
model:

- main_category: humanitarian | maritime
- incident_type: what happened / what pattern is being investigated
- facets: sanctions/corroboration/source evidence, never top-level categories

This module is intentionally pure so API, edge and tests can share it.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

MAIN_HUMANITARIAN = "humanitarian"
MAIN_MARITIME = "maritime"

# The stable, closed, UI-facing topic buckets `incident_type` may return.
# `observation_type` is evidence-level and open-ended (whatever a detector
# calls its own finding); `incident_type` is the fixed vocabulary the public
# selector taxonomy (apps/web/src/main.jsx SIGNALS_MACRO_GROUPS) is built
# from and must never grow silently just because a new detector label
# reached this module. A bucket name is a topic ("Spoofing / position
# integrity"), never a certainty claim -- confidence lives in
# evidence_state/hypothesis_type/verification_status instead.
STABLE_HUMANITARIAN_INCIDENT_TYPES = frozenset({
    "rescue", "distress", "missing", "shipwreck", "pushback",
    "land_humanitarian", "resolution", "migration_incident", "sar_activity",
    "humanitarian_context",
})
STABLE_MARITIME_INCIDENT_TYPES = frozenset({
    "ais_gap", "position_integrity", "dark_activity", "spoofing",
    "transfer", "infrastructure_proximity",
    "loitering", "navigation_safety", "identity_integrity", "port_call",
    "piracy_security", "environmental_hazard", "context_report",
    "public_observation", "maritime_context",
})

# observation_type() -> the closed maritime bucket it belongs to. Every value
# observation_type() can produce for a maritime signal must be mapped here
# (or fall through incident_type()'s regex chain below); the property test
# in tests/test_incident_taxonomy.py fails closed if a new detector label is
# added to observation_type() without a matching bucket.
_OBSERVATION_TYPE_BUCKETS: dict[str, str] = {
    "distress_beacon": "navigation_safety",
    "ais_gap": "ais_gap",
    "position_anomaly": "position_integrity",
    "rendezvous": "transfer",
    "infrastructure_proximity": "infrastructure_proximity",
    "loitering": "loitering",
    "port_call": "port_call",
}


def is_independently_corroborated(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether metadata carries independent-source corroboration.

    Review/evidence maturity and detector counts are deliberately excluded:
    they answer different questions. ``independent_source_count`` is retained
    as a supported producer contract because the fusion layer defines it as
    the number of independent lineage groups, not the number of observations.
    """
    meta = metadata or {}
    if str(meta.get("verification_status") or "") == "multi_source_corroborated":
        return True
    raw_groups = meta.get("contributing_independence_groups") or meta.get(
        "independence_groups"
    ) or ()
    if isinstance(raw_groups, (list, tuple, set, frozenset)):
        groups = {str(group).strip() for group in raw_groups if str(group).strip()}
        if len(groups) >= 2:
            return True
    try:
        return int(meta.get("independent_source_count") or 0) >= 2
    except (TypeError, ValueError):
        return False


def _tokens(
    *,
    event_type: str,
    maritime_domain: str | None,
    humanitarian_case_type: str | None,
    metadata: Mapping[str, Any],
    hypothesis_type: str | None = None,
) -> str:
    values: list[Any] = [
        event_type,
        maritime_domain,
        humanitarian_case_type,
        hypothesis_type,
        metadata.get("anomaly_type"),
        metadata.get("alert_type"),
        metadata.get("activity_kind"),
        metadata.get("observation_type"),
        metadata.get("ais_nav_status_kind"),
        metadata.get("episode_family"),
        metadata.get("detection_reason"),
        metadata.get("visual_category"),
    ]
    anomaly_types = metadata.get("anomaly_types")
    if isinstance(anomaly_types, (list, tuple)):
        values.extend(anomaly_types)
    return re.sub(r"[\s-]+", "_", " ".join(str(v) for v in values if v).lower())


def main_category(
    *,
    event_type: str = "",
    maritime_domain: str | None = None,
    humanitarian_case_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    meta = metadata or {}
    hct = str(humanitarian_case_type or meta.get("humanitarian_case_type") or "").lower()
    domain = str(maritime_domain or meta.get("maritime_domain") or "").lower()
    observation_type = str(meta.get("observation_type") or "").lower()

    if (
        hct
        or domain in {"sar", "humanitarian"}
        or event_type == "iom_incident"
        or (
            event_type == "ngo_activity"
            and observation_type == "sar_responder_activity"
        )
    ):
        return MAIN_HUMANITARIAN
    return MAIN_MARITIME


def observation_type(
    *,
    event_type: str = "",
    maritime_domain: str | None = None,
    humanitarian_case_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Describe what was observed without upgrading it into a hypothesis."""
    meta = metadata or {}
    category = main_category(
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=humanitarian_case_type,
        metadata=meta,
    )
    explicit = str(meta.get("observation_type") or "").strip().lower()
    if explicit:
        return explicit
    if category == MAIN_HUMANITARIAN:
        hct = str(
            humanitarian_case_type or meta.get("humanitarian_case_type") or ""
        ).lower()
        return hct or ("migration_incident" if event_type == "iom_incident" else "humanitarian_context")

    anomaly = str(meta.get("anomaly_type") or "").strip().lower()
    alert = str(meta.get("alert_type") or "").strip().lower()
    nav_kind = str(meta.get("ais_nav_status_kind") or "").strip().lower()
    if nav_kind == "distress_beacon":
        return "distress_beacon"
    if anomaly in {"gap", "long_gap", "ais_gap", "signal_gap", "transponder_off"}:
        return "ais_gap"
    if anomaly in {
        "position_jump", "impossible_speed", "teleport", "circle_spoof",
        "static_spoof", "circular_pattern", "static_position_inconsistency",
    }:
        return "position_anomaly"
    if anomaly in {"rendezvous", "ais_rendezvous", "sts"} or event_type == "ais_rendezvous":
        return "rendezvous"
    if anomaly in {"infra_proximity", "infrastructure_proximity"} or alert == "infra_proximity":
        return "infrastructure_proximity"
    if anomaly in {"loiter", "abnormal_dwell", "stationary_anomaly"}:
        return "loitering"
    if anomaly == "sanctioned_port_call":
        return "port_call"
    return "maritime_context"


def incident_type(
    *,
    event_type: str = "",
    maritime_domain: str | None = None,
    humanitarian_case_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    hypothesis_type: str | None = None,
) -> str:
    meta = metadata or {}
    category = main_category(
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=humanitarian_case_type,
        metadata=meta,
    )
    hct = str(humanitarian_case_type or meta.get("humanitarian_case_type") or "").lower()
    tokens = _tokens(
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=hct,
        metadata=meta,
        hypothesis_type=hypothesis_type,
    )

    if category == MAIN_HUMANITARIAN:
        if hct in {"rescue_update", "rescue_completed", "rescue"}:
            return "rescue"
        if hct in {
            "distress",
            "missing",
            "shipwreck",
            "pushback",
            "land_humanitarian",
            "resolution",
        }:
            return hct
        if event_type == "iom_incident":
            return "migration_incident"
        if event_type == "ngo_activity" or "sar_responder_activity" in tokens:
            return "sar_activity"
        if event_type == "distress" or "distress" in tokens:
            return "distress"
        return "humanitarian_context"

    # Maritime: raw observations keep neutral semantics. Stronger language is
    # reserved for an explicit InvestigationHypothesis.
    if hypothesis_type == "dark_transit":
        return "dark_activity"
    if hypothesis_type == "position_spoofing":
        return "spoofing"
    if hypothesis_type == "covert_rendezvous":
        return "transfer"
    if hypothesis_type == "infrastructure_pattern":
        return "infrastructure_proximity"
    observed = observation_type(
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=humanitarian_case_type,
        metadata=meta,
    )
    bucket = _OBSERVATION_TYPE_BUCKETS.get(observed)
    if bucket is not None:
        return bucket
    # Derived/fused event families may describe an assessed activity while
    # retaining the underlying observation separately.
    if event_type == "correlated_alert" and re.search(
        r"rendezvous|ship_to_ship|(^|_)sts(_|$)|transfer", tokens
    ):
        return "transfer"
    if event_type == "correlated_alert" and re.search(
        r"infrastructure|pipeline|cable|platform_proximity", tokens
    ):
        return "infrastructure_proximity"
    if re.search(r"loiter|abnormal_dwell|stationary_anomaly", tokens):
        return "loitering"
    if str(meta.get("anomaly_type") or "") == "sanctioned_port_call":
        return "port_call"
    if re.search(
        r"not_under_command|unable_to_man|restricted_man|aground|engine_failure|"
        r"mechanical_failure|disabled_vessel|vessel_casualty",
        tokens,
    ):
        return "navigation_safety"
    if re.search(r"identity|flag_hopping|mmsi_mismatch|imo_mismatch|false_flag", tokens):
        return "identity_integrity"
    if re.search(r"piracy|hijack|armed_robbery", tokens):
        return "piracy_security"
    if re.search(r"pollution|oil_spill|environmental|gdacs", tokens):
        return "environmental_hazard"
    if event_type == "news":
        return "context_report"
    if event_type in {"twitter", "mastodon", "bluesky"}:
        return "public_observation"
    return "maritime_context"


def taxonomy_fields(
    *,
    event_type: str = "",
    maritime_domain: str | None = None,
    humanitarian_case_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    hypothesis_type: str | None = None,
    has_satellite: bool | None = None,
) -> dict[str, Any]:
    meta = metadata or {}
    category = main_category(
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=humanitarian_case_type,
        metadata=meta,
    )
    subtype = incident_type(
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=humanitarian_case_type,
        metadata=meta,
        hypothesis_type=hypothesis_type,
    )
    verification = str(meta.get("verification_status") or "")
    evidence_state = str(
        meta.get("evidence_state")
        or meta.get("evidence_stage")
        or meta.get("analysis_state")
        or ""
    )
    corroborated = is_independently_corroborated(meta)
    sanctions = bool(
        meta.get("sanctions_matched")
        or meta.get("sanctions")
        or str(maritime_domain or meta.get("maritime_domain") or "").lower() == "sanctions"
        or str(meta.get("anomaly_type") or "") == "sanctioned_port_call"
    )
    result: dict[str, Any] = {
        "main_category": category,
        "incident_type": subtype,
        "observation_type": observation_type(
            event_type=event_type,
            maritime_domain=maritime_domain,
            humanitarian_case_type=humanitarian_case_type,
            metadata=meta,
        ),
        "hypothesis_type": hypothesis_type or meta.get("hypothesis_type"),
        "evidence_state": evidence_state,
        "corroborated": corroborated,
        "sanctions_matched": sanctions,
        "facets": [
            facet
            for facet, present in (
                ("corroborated", corroborated),
                ("sanctions", sanctions),
            )
            if present
        ],
    }
    if verification:
        result["verification_status"] = verification
    if has_satellite is not None:
        result["has_satellite"] = bool(has_satellite)
    return result
