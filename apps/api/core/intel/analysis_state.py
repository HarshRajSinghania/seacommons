"""Canonical analysis-state projection for IntelEvent."""
from __future__ import annotations

from typing import Any

VALID_ANALYSIS_STATES = frozenset({"observation", "signal", "anomaly", "evidence_candidate", "evidence"})
VALID_PUBLICATION_STATES = frozenset({"internal", "reviewed", "published", "private"})
VALID_RESOLUTION_STATES = frozenset({"open", "explained", "superseded", "linked"})

_ANOMALY_TYPES = frozenset({
    "ais_anomaly", "ais_spike", "ais_rendezvous", "dark_candidate",
    "vessel_identity", "gfw_event",
})
_HUMANITARIAN_SIGNAL_TYPES = frozenset({"distress", "iom_incident", "ngo_activity"})


def _publication_state(meta: dict[str, Any]) -> str:
    explicit = str(meta.get("publication_state") or meta.get("publication_status") or "").lower()
    if explicit in VALID_PUBLICATION_STATES:
        return explicit
    if meta.get("explicit_review_done") or meta.get("reviewed_at"):
        return "reviewed"
    return "internal"
def _resolution_state(meta: dict[str, Any]) -> str:
    explicit = str(meta.get("resolution_state") or "").lower()
    if explicit in VALID_RESOLUTION_STATES:
        return explicit
    if meta.get("translation_of") or meta.get("linked_case_id"):
        return "linked"
    lifecycle = str(meta.get("incident_lifecycle") or "").lower()
    if lifecycle in {"resolved", "archived", "expired"}:
        return "superseded"
    gap_reason = meta.get("gap_reason") or {}
    if isinstance(gap_reason, dict) and gap_reason.get("hypothesis") == "coverage_gap":
        return "explained"
    try:
        jammed_gap = (
            float(meta.get("jamming_score") or 0.0) >= 0.3
            and meta.get("anomaly_type") in {"gap", "long_gap"}
        )
        if jammed_gap:
            return "explained"
    except (TypeError, ValueError):
        pass
    return "open"


def _analysis_state(event_type: str, meta: dict[str, Any]) -> str:
    explicit = str(meta.get("analysis_state") or "").lower()
    if explicit in VALID_ANALYSIS_STATES:
        return explicit
    verification = str(meta.get("verification_status") or "").lower()
    anomaly_type = str(meta.get("anomaly_type") or "").lower()
    if meta.get("sanctions_matched") and anomaly_type in {
        "sdn_match", "sanctioned_vessel", "sanctioned_port_call",
    }:
        return "evidence"
    if event_type == "correlated_alert":
        return (
            "evidence"
            if verification == "multi_source_corroborated"
            else "evidence_candidate"
        )
    if event_type in _ANOMALY_TYPES or anomaly_type:
        return "anomaly"
    if event_type in _HUMANITARIAN_SIGNAL_TYPES or meta.get("is_distress"):
        return "signal"
    return "observation"


def _lineage_ids(event: Any, meta: dict[str, Any]) -> list[str]:
    for key in (
        "lineage_ids",
        "contributing_independence_groups",
        "independence_groups",
    ):
        values = meta.get(key)
        if isinstance(values, (list, tuple, set)):
            clean = [str(v) for v in values if str(v).strip()]
            if clean:
                return list(dict.fromkeys(clean))
    policy = str(meta.get("source_policy") or "").strip()
    source = str(getattr(event, "source", "") or "").strip().lower().replace(" ", "_")
    if policy:
        return [f"source_policy:{policy}"]
    if source:
        return [f"source:{source}"]
    return ["source:unknown"]


def annotate_event_analysis(event: Any) -> None:
    """Attach analytical state without altering the underlying observation."""
    meta = dict(getattr(event, "metadata", {}) or {})
    meta.setdefault("analysis_schema_version", 1)
    meta.setdefault(
        "analysis_state",
        _analysis_state(str(getattr(event, "type", "") or ""), meta),
    )
    meta.setdefault("publication_state", _publication_state(meta))
    meta.setdefault("resolution_state", _resolution_state(meta))
    meta.setdefault("lineage_ids", _lineage_ids(event, meta))
    event.metadata = meta
