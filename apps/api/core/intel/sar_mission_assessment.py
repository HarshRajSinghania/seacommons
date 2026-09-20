from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

METHOD_VERSION = "sar-mission-assessment-v1"
_ALLOWED_STATES = {
    "unrelated", "possible_response", "approaching", "on_scene",
    "probable_rescue_activity", "departing_scene", "post_rescue_transit",
    "insufficient_evidence",
}
_STATE_CONFIDENCE = {
    "unrelated": 0.4,
    "possible_response": 0.5,
    "approaching": 0.65,
    "on_scene": 0.7,
    "probable_rescue_activity": 0.78,
    "departing_scene": 0.65,
    "post_rescue_transit": 0.65,
    "insufficient_evidence": 0.2,
}


def mission_assessment_id(incident_id: str, asset_identity: str) -> str:
    digest = hashlib.blake2s(
        f"{incident_id}:{asset_identity}:sar_mission".encode(), digest_size=16,
    ).hexdigest()
    return f"sar:{digest}"


def _normalize_state(vessel: dict[str, Any]) -> tuple[str, list[str]]:
    raw = str(vessel.get("mission_state") or "insufficient_evidence")
    reasons = [f"MISSION_STATE_{raw.upper()}"]
    if str(vessel.get("coverage_status") or "") == "provider_degraded":
        return "possible_response", ["PROVIDER_DEGRADED_CAP"]
    if raw == "rescue_confirmed":
        return "probable_rescue_activity", ["AIS_CONFIRMATION_CAP"]
    if raw not in _ALLOWED_STATES:
        return "insufficient_evidence", ["UNKNOWN_MISSION_STATE"]
    return raw, reasons


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "assessment_id": row.assessment_id,
        "incident_id": row.incident_id,
        "field_type": row.field_type,
        "value": dict(row.value or {}),
        "method_version": row.method_version,
        "confidence": row.confidence,
        "review_state": row.review_state,
    }


def persist_sar_mission_assessments(
    incident_id: str,
    ngo_response: dict[str, Any],
    *,
    behaviour_context_by_asset: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    from core.db.models import AssessmentDB
    from core.db.session import session_scope

    contexts = behaviour_context_by_asset or {}
    persisted: list[dict[str, Any]] = []
    with session_scope() as db:
        for vessel in list(ngo_response.get("ngo_vessels") or []):
            asset_identity = str(vessel.get("mmsi") or vessel.get("name") or "").strip()
            if not asset_identity:
                continue
            state, reason_codes = _normalize_state(vessel)
            providers = sorted({str(v) for v in (vessel.get("track_providers") or []) if str(v)})
            upstream = sorted({str(v) for v in (vessel.get("upstream_sources") or []) if str(v)})
            value = {
                "asset_identity": asset_identity,
                "asset_name": str(vessel.get("name") or ""),
                "org": str(vessel.get("org") or ""),
                "mission_state": state,
                "reason_codes": reason_codes,
                "coverage_status": str(vessel.get("coverage_status") or "coverage_unknown"),
                "motion_flags": sorted({str(v) for v in (vessel.get("motion_flags") or []) if str(v)}),
                "track_providers": providers,
                "upstream_sources": upstream,
                "independence_groups": ["ais_sensor_lineage"],
                "distance_nm": vessel.get("distance_nm"),
                "heading_toward": bool(vessel.get("heading_toward")),
                "eta_h": vessel.get("eta_h"),
                "fix_age_min": vessel.get("fix_age_min"),
                "behaviour_context": dict(contexts.get(asset_identity) or {}),
            }
            aid = mission_assessment_id(incident_id, asset_identity)
            row = db.get(AssessmentDB, aid)
            if row is None:
                row = AssessmentDB(
                    assessment_id=aid, incident_id=incident_id, field_type="sar_mission",
                    value=value, supporting_claim_ids=[], contradicting_claim_ids=[],
                    method_version=METHOD_VERSION, confidence=_STATE_CONFIDENCE[state],
                    review_state="unreviewed",
                )
                db.add(row)
                db.flush()
            else:
                row.value = value
                row.method_version = METHOD_VERSION
                row.confidence = _STATE_CONFIDENCE[state]
            persisted.append(_row_to_dict(row))
            try:
                from core.observability import record_humanitarian_verification_event
                record_humanitarian_verification_event(
                    stage="mission", source_role="verification", outcome=state,
                )
            except Exception:
                pass
    return persisted


def enrich_active_humanitarian_incidents_with_sar_mission(
    *, limit: int = 200,
) -> int:
    """Cross-check every active/needs_review Humanitarian incident against
    the SAR/NGO responder registry and persist the result.

    persist_sar_mission_assessments() and analyze_ngo_response() both
    already existed and were fully implemented, but nothing ever called
    this combination in the running system -- a Humanitarian case's
    "current assessment" had no SAR-responder context at all no matter how
    much relevant NGO AIS activity was nearby. This is the missing caller.

    SAR movement observed only through AIS remains a single lineage
    (independence_groups always ["ais_sensor_lineage"] here, see
    persist_sar_mission_assessments) -- it enriches the case's operational
    picture, it never creates corroboration on its own.
    """
    from core.db.models import HumanitarianIncidentDB, IntelEventDB
    from core.db.session import session_scope
    from core.intel.ngo_response import analyze_ngo_response
    from core.intel.store import IntelEvent

    enriched = 0
    with session_scope() as db:
        rows = (
            db.query(HumanitarianIncidentDB)
            .filter(HumanitarianIncidentDB.lifecycle.in_(("active", "needs_review")))
            .order_by(HumanitarianIncidentDB.incident_id.asc())
            .limit(limit)
            .all()
        )
        incident_ids = [row.incident_id for row in rows]
        if not incident_ids:
            return 0
        events_by_id = {
            row.id: row
            for row in db.query(IntelEventDB).filter(IntelEventDB.id.in_(incident_ids)).all()
        }
        for row in rows:
            event_row = events_by_id.get(row.incident_id)
            if event_row is None or event_row.lat is None or event_row.lon is None:
                continue
            event = IntelEvent(
                id=event_row.id, timestamp_utc=event_row.timestamp_utc,
                type=event_row.type or "", severity=event_row.severity or "",
                lat=event_row.lat, lon=event_row.lon, title=event_row.title or "",
                source=event_row.source or "", metadata=dict(event_row.meta or {}),
            )
            try:
                ngo_response = analyze_ngo_response(event)
            except Exception:
                logger.warning("SAR mission enrichment failed for %s", row.incident_id, exc_info=True)
                continue
            if not ngo_response.get("ngo_vessels"):
                continue
            persist_sar_mission_assessments(row.incident_id, ngo_response)
            enriched += 1
    return enriched


def get_sar_mission_assessments(incident_id: str) -> list[dict[str, Any]]:
    from core.db.models import AssessmentDB
    from core.db.session import session_scope

    with session_scope() as db:
        rows = (
            db.query(AssessmentDB)
            .filter(AssessmentDB.incident_id == incident_id, AssessmentDB.field_type == "sar_mission")
            .order_by(AssessmentDB.assessment_id.asc())
            .all()
        )
        return [_row_to_dict(row) for row in rows]
