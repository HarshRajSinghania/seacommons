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

    # Maritime: keep observation semantics separate from hypotheses.
    # Raw detector evidence must not inherit an allegation-strength label.
    if hypothesis_type == "dark_transit":
        return "dark_activity"
    if hypothesis_type == "position_spoofing":
        return "spoofing"
    if hypothesis_type == "covert_rendezvous":
        return "transfer"
    if hypothesis_type == "infrastructure_pattern":
        return "infrastructure_pattern"

    anomaly = str(meta.get("anomaly_type") or "").lower()
    if anomaly in {"gap", "long_gap", "ais_gap", "signal_gap", "transponder_off"}:
        return "ais_gap"
    if anomaly in {
        "impossible_speed", "position_jump", "teleport", "circle_spoof",
        "static_spoof", "gnss_manipulation",
    }:
        return "position_anomaly"
    if event_type == "ais_rendezvous" or anomaly in {"ais_rendezvous", "rendezvous", "sts"}:
        return "rendezvous"
    if anomaly in {"infra_proximity", "infrastructure_proximity", "platform_proximity"}:
        return "infrastructure_proximity"
    if event_type == "correlated_alert" and re.search(
        r"rendezvous|ship_to_ship|(^|_)sts(_|$)|transfer",
        tokens,
    ):
        return "transfer"
    if event_type == "correlated_alert" and re.search(
        r"position_spoof|spoof|teleport|impossible_speed|position_jump|gnss_manip",
        tokens,
    ):
        return "spoofing"
    if re.search(r"loiter|abnormal_dwell|stationary_anomaly", tokens):
        return "loitering"
    if str(meta.get("anomaly_type") or "") == "sanctioned_port_call":
        return "port_call"
    if str(meta.get("ais_nav_status_kind") or "") == "distress_beacon" or re.search(
        r"ais_sart|ais_mob|ais_epirb|distress_beacon", tokens,
    ):
        return "distress_beacon"
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
    independent = int(meta.get("independent_source_count") or 0)
    groups = {
        str(value) for value in (
            meta.get("contributing_independence_groups")
            or meta.get("independence_groups")
            or ()
        ) if value
    }
    corroborated = (
        verification == "multi_source_corroborated"
        or len(groups) >= 2
        or independent >= 2
    )
    sanctions = bool(
        meta.get("sanctions_matched")
        or meta.get("sanctions")
        or str(maritime_domain or meta.get("maritime_domain") or "").lower() == "sanctions"
        or str(meta.get("anomaly_type") or "") == "sanctioned_port_call"
    )
    result: dict[str, Any] = {
        "main_category": category,
        "incident_type": subtype,
        "corroborated": corroborated,
        "sanctions_matched": sanctions,
    }
    observation_type = str(meta.get("observation_type") or "").strip()
    if observation_type:
        result["observation_type"] = observation_type
    if hypothesis_type:
        result["hypothesis_type"] = str(hypothesis_type)
    evidence_state = str(
        meta.get("evidence_state")
        or meta.get("evidence_stage")
        or meta.get("analysis_state")
        or ""
    ).strip()
    if evidence_state:
        result["evidence_state"] = evidence_state
    if has_satellite is not None:
        result["has_satellite"] = bool(has_satellite)
    return result
