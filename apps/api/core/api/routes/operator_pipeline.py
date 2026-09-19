# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator-facing map of the canonical SeaCommons parsing chains.

This is documentation as data: each entry names the durable input, parser(s),
derived processors and the evidentiary meaning of the resulting outputs.
It does not execute a parallel pipeline.
"""
from __future__ import annotations

from typing import Any


PIPELINE_CHAINS: tuple[dict[str, Any], ...] = (
    {
        "id": "ais_position_integrity",
        "source_family": "AIS",
        "inputs": ["AISStream position/static/navigation messages"],
        "raw_observations": [
            "ais_position",
            "ais_nav_status",
            "ais_gap (reappearance after sampled silence; telemetry, not a dark-activity finding)",
        ],
        "normalizers": [
            "core.vessels.ais_runtime",
            "core.vessels.ais_source_observation.AISSourceObservationSampler",
            "core.vessels.track_store",
        ],
        "derived_processors": [
            "core.anomaly.ais.AISAnomalyDetector (telemetry/integrity cues)",
            "core.mda.watch.MdaWatch.scan_gaps (canonical dark-gap detector)",
            "core.mda.watch.MdaWatch.scan_spoofing",
            "core.mda.watch.MdaWatch.scan_rendezvous",
            "core.mda.watch.MdaWatch.scan_infra_loiter",
        ],
        "normalized_outputs": ["IntelEvent: ais_anomaly", "IntelEvent: ais_rendezvous"],
        "case_families": [
            "gap_episode → dark_transit",
            "spoofing_episode → position_spoofing",
            "rendezvous_episode → covert_rendezvous",
            "infrastructure_proximity_episode → infrastructure_pattern",
        ],
        "independence_group": "ais_sensor_lineage",
        "false_positive_controls": [
            "local AIS coverage witnesses",
            "port/anchorage context",
            "coast distance",
            "GNSS-jamming context",
            "speed/course plausibility",
            "coincident multi-vessel jumps",
            "behavioural baseline",
        ],
    },
    {
        "id": "ais_safety",
        "source_family": "AIS safety/navigation status",
        "inputs": ["AIS nav status", "AIS-SART/MOB/EPIRB MMSI prefixes 970/972/974"],
        "raw_observations": ["ais_nav_status"],
        "normalizers": ["core.intel.vessel_incident_monitor"],
        "derived_processors": ["core.intel.assessment"],
        "normalized_outputs": ["IntelEvent: distress", "IntelEvent: vessel safety status"],
        "case_families": ["safety_episode"],
        "independence_group": "ais_sensor_lineage",
        "false_positive_controls": [
            "sustained reports",
            "beacon freshness",
            "port/anchorage exclusion for uncorroborated dedicated beacons",
            "land-position exclusion",
            "independent SAR/DSC/human corroboration",
        ],
    },
    {
        "id": "humanitarian_social",
        "source_family": "Humanitarian public reports",
        "inputs": ["Alarm Phone/X public posts", "SOS Méditerranée/public NGO reports"],
        "raw_observations": ["source_post", "media_attachment"],
        "normalizers": [
            "core.intel.twikit_monitor",
            "core.intel.geoextract",
            "core.intel.media_evidence",
            "core.intel.humanitarian_truth_table",
        ],
        "derived_processors": [
            "coordinate extraction/review",
            "media OCR/image extraction",
            "humanitarian incident lifecycle reconciliation",
            "cross-source triangulation",
        ],
        "normalized_outputs": ["IntelEvent: twitter/source report", "HumanitarianIncident"],
        "case_families": ["humanitarian distress/SAR incident"],
        "independence_group": "human_report_lineage",
        "false_positive_controls": [
            "public-source allowlist",
            "translation/dedup",
            "coordinate provenance",
            "media preservation hash",
            "incident lifecycle",
            "independent source reconciliation",
        ],
    },
    {
        "id": "satellite_sar_optical",
        "source_family": "Satellite",
        "inputs": ["GFW SAR detections", "VIIRS VBD detections", "other configured satellite observations"],
        "raw_observations": ["satellite_detection", "ais_derived_event"],
        "normalizers": ["core.intel.gfw_monitor", "core.intel.viirs_monitor"],
        "derived_processors": [
            "core.mda.darkship_cue",
            "core.mda.sar_association",
            "cross-modal reachable-area matching",
        ],
        "normalized_outputs": ["SatelliteObservation", "IntelEvent: dark_candidate/cross-modal cue"],
        "case_families": ["cross-modal evidence attached to gap/spoofing investigations"],
        "independence_group": "satellite_sensor_lineage",
        "false_positive_controls": [
            "AIS association before treating SAR target as unmatched",
            "time/reachable-area gate",
            "sensor-specific uncertainty",
            "candidate status does not identify a vessel by itself",
        ],
    },
    {
        "id": "radio_rf",
        "source_family": "Radio / RF",
        "inputs": ["KiwiSDR/OpenWebRX PCM frames", "structured DSC/NAVTEX decoder output"],
        "raw_observations": ["remote_radio_signal", "dsc_message", "navtex_message"],
        "normalizers": [
            "core.radio.runtime",
            "core.radio.decoder_runtime",
            "core.radio.structured_source_observation",
        ],
        "derived_processors": ["core.radio.safety_projection", "core.radio.ais_association"],
        "normalized_outputs": ["RadioBurst", "RadioEvent", "IntelEvent: radio safety cue"],
        "case_families": ["distress/SAR corroboration", "radio-AIS association"],
        "independence_group": "radio_sensor_lineage",
        "false_positive_controls": [
            "decoder validity",
            "receiver/time-frequency correlation",
            "multi-receiver support",
            "AIS association is not assumed from proximity alone",
        ],
    },
    {
        "id": "identity_sanctions",
        "source_family": "Identity / sanctions",
        "inputs": ["AIS vessel identity", "configured sanctions/reference lists", "port-call track evidence"],
        "raw_observations": ["sanctions_list_match", "AIS identity/static data"],
        "normalizers": ["core.mda.identity"],
        "derived_processors": ["core.mda.watch.scan_sanctioned_port_calls"],
        "normalized_outputs": ["IntelEvent: vessel_identity"],
        "case_families": ["identity_integrity_episode", "port_call_episode"],
        "independence_group": "identity_reference_lineage",
        "false_positive_controls": [
            "strong IMO/MMSI identity",
            "name-only match rejected",
            "minimum AIS fixes for port call",
            "recent activity window",
        ],
    },
    {
        "id": "news_official_alerts",
        "source_family": "Public official/news feeds",
        "inputs": ["GDACS/RSS/news/official configured feeds"],
        "raw_observations": ["source_post"],
        "normalizers": ["core.intel.news_monitor", "core.intel.gdacs_monitor", "core.intel.ingestion_service"],
        "derived_processors": ["translation/dedup", "triangulation/correlation where applicable"],
        "normalized_outputs": ["IntelEvent: news/source alert"],
        "case_families": ["context or corroboration; never a public security case from one article alone"],
        "independence_group": "public_report_lineage",
        "false_positive_controls": [
            "source policy",
            "deduplication",
            "published URL proves provenance, not claim truth",
            "independent corroboration required for public Live news",
        ],
    },
)


HYPOTHESIS_SEMANTICS: dict[str, dict[str, str]] = {
    "dark_transit": {
        "label": "Dark-transit investigation",
        "possible_meaning": (
            "Vessel-specific AIS silence while surrounding coverage remains available. "
            "It can reflect equipment loss, security-driven shutdown, reception edge effects, or deliberate concealment."
        ),
        "illegal_activity_status": "Not an illegality finding. Intent and applicable law require independent evidence and review.",
    },
    "position_spoofing": {
        "label": "Position-integrity investigation",
        "possible_meaning": (
            "Implausible or reproducible AIS position behaviour. It can reflect bad fixes, GNSS interference, "
            "receiver/system faults, or deliberate position manipulation."
        ),
        "illegal_activity_status": "Not an illegality finding. The system must separate shared interference from vessel-specific manipulation.",
    },
    "covert_rendezvous": {
        "label": "Rendezvous / STS investigation",
        "possible_meaning": (
            "Sustained low-speed proximity between vessels offshore. It may be routine assistance, fishing/transshipment, "
            "lawful ship-to-ship transfer, or concealment-related activity depending on cargo, identity and other evidence."
        ),
        "illegal_activity_status": "Legality cannot be inferred from proximity alone.",
    },
    "infrastructure_pattern": {
        "label": "Infrastructure-proximity investigation",
        "possible_meaning": (
            "Repeated or sustained vessel behaviour near subsea infrastructure. Transit or work activity may be legitimate; "
            "route repetition, unexplained loitering and independent evidence are needed before escalation."
        ),
        "illegal_activity_status": "Proximity alone is not suspicious and is never treated as proof.",
    },
}


def pipeline_chains() -> list[dict[str, Any]]:
    return [dict(item) for item in PIPELINE_CHAINS]


def hypothesis_semantics(hypothesis_type: str) -> dict[str, str]:
    return dict(HYPOTHESIS_SEMANTICS.get(hypothesis_type, {
        "label": hypothesis_type.replace("_", " ").title(),
        "possible_meaning": "Investigation hypothesis derived from the canonical evidence pipeline.",
        "illegal_activity_status": "No automated illegality finding.",
    }))


def observation_chain_id(source_name: str, observation_type: str) -> str:
    source = str(source_name or "").lower()
    kind = str(observation_type or "").lower()
    if kind == "ais_nav_status":
        return "ais_safety"
    if kind.startswith("ais_") and kind != "ais_derived_event":
        return "ais_position_integrity"
    if kind in {"remote_radio_signal", "dsc_message", "navtex_message"}:
        return "radio_rf"
    if kind in {"satellite_detection", "ais_derived_event"}:
        return "satellite_sar_optical"
    if kind == "sanctions_list_match":
        return "identity_sanctions"
    if any(token in source for token in ("alarm", "sos mediterr", "open arms")):
        return "humanitarian_social"
    if kind in {"source_post", "media_attachment"}:
        return "news_official_alerts"
    return "unmapped"


def event_chain_id(source_name: str, event_type: str, metadata: dict[str, Any]) -> str:
    source = str(source_name or "").lower()
    kind = str(event_type or "").lower()
    anomaly = str(metadata.get("anomaly_type") or "").lower()
    nav_kind = str(metadata.get("ais_nav_status_kind") or "").lower()
    if nav_kind in {"distress_beacon", "not_under_command", "aground", "restricted_manoeuvrability"}:
        return "ais_safety"
    if kind in {"ais_anomaly", "ais_rendezvous", "correlated_alert"} or source in {"ais", "mda"}:
        if anomaly in {"sdn_match", "sanctioned_vessel", "sanctioned_port_call", "identity_anomaly"}:
            return "identity_sanctions"
        return "ais_position_integrity"
    if source in {"gfw", "viirs vbd"} or kind in {"dark_candidate", "satellite_detection"}:
        return "satellite_sar_optical"
    if "radio" in source or kind in {"dsc", "navtex", "radio_event"}:
        return "radio_rf"
    if any(token in source for token in ("alarm", "sos mediterr", "open arms", "twitter", "twikit")):
        return "humanitarian_social"
    if kind == "vessel_identity":
        return "identity_sanctions"
    if kind in {"news", "gdacs"}:
        return "news_official_alerts"
    return "unmapped"
