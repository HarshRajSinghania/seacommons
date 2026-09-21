"""Bounded satellite enrichment for operationally meaningful intel events."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from core.intel.lifecycle import parse_utc
from core.intel.satellite_observation import persist_observations
from core.intel.satellite_resolver import resolve_for_incident

logger = logging.getLogger(__name__)

SATELLITE_HISTORY_DAYS = 7
PLAY_HUMANITARIAN_HISTORY_DAYS = 30
SATELLITE_RECHECK_MINUTES = 30
MAX_CASE_TARGETS = 4
_DIRECTIONS = ("reverse", "nearest", "forward")


def _sample_indices(length: int, wanted: int) -> list[int]:
    if length <= 0 or wanted <= 0:
        return []
    if length <= wanted:
        return list(range(length))
    if wanted == 1:
        return [length - 1]
    return sorted({
        round(index * (length - 1) / (wanted - 1))
        for index in range(wanted)
    })


def _trajectory_targets(
    trajectory: dict[str, Any] | None,
    *,
    drift_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not isinstance(trajectory, dict):
        return []
    geometry = trajectory.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    properties = trajectory.get("properties") or {}
    timestamps = properties.get("timestamps_utc") or properties.get("times") or []
    if (
        geometry.get("type") != "LineString"
        or not isinstance(coordinates, list)
        or not isinstance(timestamps, list)
        or len(coordinates) != len(timestamps)
        or len(coordinates) < 2
    ):
        return []
    candidates = list(range(1, len(coordinates)))
    selected_positions = _sample_indices(len(candidates), limit)
    targets: list[dict[str, Any]] = []
    for pos in selected_positions:
        index = candidates[pos]
        point = coordinates[index]
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        target_time = parse_utc(str(timestamps[index]))
        if target_time is None:
            continue
        targets.append({
            "role": "drift_trajectory",
            "lat": float(point[1]),
            "lon": float(point[0]),
            "at": target_time,
            "drift_id": drift_id,
            "trajectory_index": index,
        })
    return targets


def _case_satellite_targets(event) -> list[dict[str, Any]]:
    event_time = parse_utc(event.timestamp_utc) or datetime.now(timezone.utc)
    targets: list[dict[str, Any]] = [{
        "role": "reported_origin",
        "lat": float(event.lat),
        "lon": float(event.lon),
        "at": event_time,
    }]
    try:
        from core.db.models import DriftResultDB, HumanitarianIncidentDB
        from core.db.session import session_scope

        with session_scope() as db:
            incident = db.get(HumanitarianIncidentDB, event.id)
            if incident is None or not incident.current_drift_id:
                return targets
            drift = db.get(DriftResultDB, incident.current_drift_id)
            if drift is None or drift.status != "completed":
                return targets
            trajectory = dict(drift.trajectory or {})
            drift_id = str(drift.drift_id)
        targets.extend(_trajectory_targets(
            trajectory,
            drift_id=drift_id,
            limit=max(0, MAX_CASE_TARGETS - 1),
        ))
    except Exception:
        logger.debug("Satellite case targets unavailable for %s", event.id, exc_info=True)
    return targets[:MAX_CASE_TARGETS]


def _annotate_target(observation, target: dict[str, Any], direction: str):
    provenance = dict(observation.provenance or {})
    target_record = {
        "role": str(target.get("role") or "case_target"),
        "lat": round(float(target["lat"]), 6),
        "lon": round(float(target["lon"]), 6),
        "at": target["at"].astimezone(timezone.utc).isoformat(),
        "search_direction": direction,
    }
    if target.get("drift_id"):
        target_record["drift_id"] = str(target["drift_id"])
    if target.get("trajectory_index") is not None:
        target_record["trajectory_index"] = int(target["trajectory_index"])
    provenance["case_targets"] = [target_record]
    return replace(observation, provenance=provenance)


def is_satellite_enrichment_candidate(
    event, *, now: datetime | None = None, history_days: int = SATELLITE_HISTORY_DAYS
) -> bool:
    """Select sparse incident-level signals, never raw high-volume AIS pings."""
    if event.lat is None or event.lon is None:
        return False
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observed = parse_utc(event.timestamp_utc)
    if observed is not None and now - observed > timedelta(days=max(1, history_days)):
        return False

    meta = event.metadata or {}
    meaningful = bool(
        meta.get("is_distress")
        or meta.get("publication_status") == "published"
        or meta.get("drift_eligible")
    )
    if not meaningful:
        return False
    return True


def _is_due(event, *, now: datetime) -> bool:
    checked = parse_utc(str((event.metadata or {}).get("satellite_last_checked_at") or ""))
    return checked is None or now - checked >= timedelta(minutes=SATELLITE_RECHECK_MINUTES)


def enrich_event(
    event,
    *,
    now: datetime | None = None,
    provider=None,
    include_viirs: bool = True,
) -> dict[str, int]:
    """Collect bounded satellite context along the case's temporal path.

    The reported origin gets reverse/nearest/forward searches. When a
    Humanitarian incident owns a current drift trajectory, up to three sampled
    trajectory points are searched with nearest only. Scene metadata stays
    contextual until an actual vessel/detection association is made elsewhere.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    targets = _case_satellite_targets(event)
    report = {"persisted": 0, "errors": 0}
    for target_index, target in enumerate(targets):
        directions = _DIRECTIONS if target_index == 0 else ("nearest",)
        for direction in directions:
            try:
                observations = resolve_for_incident(
                    incident_id=event.id,
                    lat=float(target["lat"]),
                    lon=float(target["lon"]),
                    event_time=target["at"],
                    direction=direction,
                    provider=provider,
                    include_viirs=include_viirs,
                    now=now,
                )
                annotated = [
                    _annotate_target(observation, target, direction)
                    for observation in observations
                ]
                report["persisted"] += persist_observations(annotated)
            except Exception as exc:
                report["errors"] += 1
                logger.info(
                    "Satellite %s lookup unavailable for event=%s target=%s: %s",
                    direction,
                    event.id,
                    target.get("role"),
                    type(exc).__name__,
                )
    return report


def enrich_recent_events(*, limit: int = 6, now: datetime | None = None) -> dict[str, int]:
    """Enrich a bounded batch so public providers are never hammered."""
    from core.intel.store import intel_store

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recent = intel_store.events(limit=300, max_age_days=SATELLITE_HISTORY_DAYS)
    humanitarian_history = intel_store.persisted_events(
        source_in=["Alarm Phone", "alarm_phone"],
        max_age_days=PLAY_HUMANITARIAN_HISTORY_DAYS,
        limit=120,
    )
    priority_ids = {event.id for event in humanitarian_history}
    ordered = [*humanitarian_history, *recent]
    events = list({event.id: event for event in ordered}.values())
    report = {"scanned": len(events), "enriched": 0, "persisted": 0, "errors": 0}

    for event in events:
        if report["enriched"] >= limit:
            break
        history_days = (
            PLAY_HUMANITARIAN_HISTORY_DAYS if event.id in priority_ids
            else SATELLITE_HISTORY_DAYS
        )
        if not is_satellite_enrichment_candidate(event, now=now, history_days=history_days) or not _is_due(event, now=now):
            continue
        result = enrich_event(event, now=now)
        report["enriched"] += 1
        report["persisted"] += result["persisted"]
        report["errors"] += result["errors"]
        intel_store.update_metadata(
            event.id,
            metadata={
                "satellite_last_checked_at": now.isoformat(),
                "satellite_last_new_observations": result["persisted"],
                "satellite_last_errors": result["errors"],
            },
        )
    return report
