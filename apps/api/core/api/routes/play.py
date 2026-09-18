"""Public Play: privacy-safe temporal reconstruction of incidents."""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from core.intel.humanitarian_incident import public_incident_status
from core.intel.lifecycle import parse_utc
from core.intel.public_policy import domains_for_mode
from core.intel.store import IntelEvent
from core.live.projection import (
    dedupe_public_case_items,
    is_useful_public_case_feature,
    public_archive_event_types,
    public_intel_feature,
)

router = APIRouter(prefix="/api/v1/play", tags=["play"])


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    parsed = parse_utc(str(value))
    return parsed.isoformat() if parsed else str(value)


def _status_for_row(row, *, now: datetime) -> str:
    return public_incident_status({
        "lifecycle": row.lifecycle,
        "incident_status": row.incident_status,
        "last_update_at": row.last_update_at,
    }, now=now)


def _incident_projection(row, event, *, now: datetime) -> dict[str, Any]:
    status = _status_for_row(row, now=now)
    geometry = None
    if event is not None and event.lat is not None and event.lon is not None:
        geometry = {"type": "Point", "coordinates": [event.lon, event.lat]}
    return {
        "incident_id": row.incident_id,
        "incident_status": status,
        "surface": "play",
        "case_type": row.case_type,
        "reported_at": row.reported_at,
        "last_update_at": row.last_update_at,
        "state_changed_at": _iso(row.state_changed_at),
        "resolved_at": _iso(row.resolved_at),
        "title": event.title if event is not None else "Humanitarian incident",
        "source": event.source if event is not None else None,
        "geometry": geometry,
        "domain": "humanitarian",
    }


def _generic_maritime_status(event) -> str:
    meta = dict(event.meta or {})
    raw = str(meta.get("incident_status") or meta.get("lifecycle") or "").lower()
    if raw == "resolved":
        return "resolved"
    if raw == "needs_review":
        return "needs_review"
    return "outcome_unknown"


def _intel_event_from_row(event) -> IntelEvent:
    meta = dict(event.meta or {})
    if event.maritime_domain and not meta.get("maritime_domain"):
        meta["maritime_domain"] = event.maritime_domain
    return IntelEvent(
        id=event.id, timestamp_utc=event.timestamp_utc, type=event.type,
        severity=event.severity, lat=event.lat, lon=event.lon,
        title=event.title or "", text=event.text or "", url=event.url or "",
        source=event.source or "", linked_mmsi=event.linked_mmsi or "", metadata=meta,
    )


def _is_public_catalog_maritime(event) -> bool:
    """Whether a durable maritime/intel row belongs in the public Play catalog.

    Catalog membership is a publication/privacy decision, not a geometry or
    age decision. Unpositioned records remain searchable/listable and current
    records may coexist with Live; only the map requires geometry.
    """
    feature = public_intel_feature(
        _intel_event_from_row(event), allowed_domains=domains_for_mode("all")
    )
    return is_useful_public_case_feature(feature)


def _generic_maritime_projection(event) -> dict[str, Any]:
    geometry = None
    if event.lat is not None and event.lon is not None:
        geometry = {"type": "Point", "coordinates": [event.lon, event.lat]}
    meta = dict(event.meta or {})
    return {
        "incident_id": event.id,
        "incident_status": _generic_maritime_status(event),
        "surface": "play",
        "case_type": event.type,
        "anomaly_type": str(meta.get("anomaly_type") or ""),
        "analysis_state": str(meta.get("analysis_state") or ""),
        "verification_status": str(meta.get("verification_status") or ""),
        "reported_at": event.timestamp_utc,
        "last_update_at": event.timestamp_utc,
        "state_changed_at": None,
        "resolved_at": None,
        "title": event.title or "Maritime incident",
        "source": event.source,
        "geometry": geometry,
        "domain": "maritime",
    }


def _is_public_play_investigation(hypothesis) -> bool:
    """Expose only investigation cases that have crossed the review boundary.

    Collecting hypotheses remain durable internal research state. Play is a
    public case archive, so a maritime investigation appears only after it is
    review-ready (or beyond), carries corroborated-or-better evidence, and has
    at least two distinct evidence references plus an explicit reason code.
    """
    if hypothesis.hypothesis_type not in {"dark_transit", "position_spoofing"}:
        return False
    if hypothesis.state not in {"review_ready", "assessed", "published"}:
        return False
    if hypothesis.evidence_stage not in {"corroborated", "assessed", "confirmed"}:
        return False
    if not (hypothesis.reason_codes or []):
        return False
    evidence_links = {str(value) for value in (hypothesis.evidence_links or []) if value}
    return len(evidence_links) >= 2


def _investigation_projection(hypothesis, episode) -> dict[str, Any]:
    title = {
        "dark_transit": "Dark transit investigation",
        "position_spoofing": "Position integrity investigation",
        "covert_rendezvous": "Covert rendezvous investigation",
        "infrastructure_pattern": "Infrastructure pattern investigation",
    }.get(hypothesis.hypothesis_type, "Maritime investigation")
    return {
        "incident_id": hypothesis.hypothesis_id,
        "incident_status": hypothesis.state,
        "surface": "play",
        "case_type": hypothesis.hypothesis_type,
        "reported_at": _iso(episode.start_at),
        "last_update_at": _iso(hypothesis.updated_at) or _iso(episode.end_at),
        "state_changed_at": _iso(hypothesis.updated_at),
        "resolved_at": None,
        "title": title,
        "source": "SeaCommons evidence engine",
        "geometry": episode.geometry,
        "domain": "investigation",
        "evidence_stage": hypothesis.evidence_stage,
        "reason_codes": list(hypothesis.reason_codes or []),
        "counter_indicators": list(hypothesis.counter_indicators or []),
    }


_PLAY_COUNTS_TTL_S = 60.0
_play_counts_cache: dict[str, Any] = {}
_play_counts_lock = Lock()


def _compute_play_catalog() -> list[dict[str, Any]]:
    """Compute the complete public SeaCommons catalog used by Play.

    Privacy/publication policy is evaluated before pagination. This is
    intentionally exact: a high-volume block of private/non-public rows can
    never crowd older public history out of the result.
    """
    from core.db.models import (
        HumanitarianIncidentDB,
        IntelEventDB,
        InvestigationHypothesisDB,
        MaritimeEpisodeDB,
    )
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    combined: list[dict[str, Any]] = []
    with session_scope() as db:
        from sqlalchemy import func
        from core.db.models import DriftResultDB, SatelliteObservationDB

        human_rows = db.query(HumanitarianIncidentDB).all()
        human_ids = {row.incident_id for row in human_rows}
        for row in human_rows:
            event = db.get(IntelEventDB, row.incident_id)
            event_meta = dict(event.meta or {}) if event is not None else {}
            if event_meta.get("translation_of") or event_meta.get("publication_status") == "internal":
                continue
            combined.append(_incident_projection(row, event, now=now))

        publication_status = IntelEventDB.meta["publication_status"].as_string()
        rows = (
            db.query(IntelEventDB)
            .filter(
                IntelEventDB.type.in_(public_archive_event_types()),
                (publication_status.is_(None)) | (publication_status != "internal"),
            )
            .yield_per(1000)
        )
        for event in rows:
            if event.id in human_ids:
                continue
            if _is_public_catalog_maritime(event):
                combined.append(_generic_maritime_projection(event))

        investigations = (
            db.query(InvestigationHypothesisDB, MaritimeEpisodeDB)
            .join(
                MaritimeEpisodeDB,
                InvestigationHypothesisDB.episode_id == MaritimeEpisodeDB.episode_id,
            )
            .filter(
                InvestigationHypothesisDB.state.in_(("review_ready", "assessed", "published")),
                InvestigationHypothesisDB.hypothesis_type.in_(("dark_transit", "position_spoofing")),
            )
            .all()
        )
        for hypothesis, episode in investigations:
            if _is_public_play_investigation(hypothesis):
                combined.append(_investigation_projection(hypothesis, episode))

        combined = dedupe_public_case_items(combined)
        public_ids = [str(item["incident_id"]) for item in combined]
        drift_counts: dict[str, int] = {}
        satellite_counts: dict[str, int] = {}
        if public_ids:
            drift_ids = [*public_ids, *(f"intel:{incident_id}" for incident_id in public_ids)]
            for event_id, count in (
                db.query(DriftResultDB.event_id, func.count(DriftResultDB.drift_id))
                .filter(DriftResultDB.status == "completed", DriftResultDB.event_id.in_(drift_ids))
                .group_by(DriftResultDB.event_id)
                .all()
            ):
                if not event_id:
                    continue
                incident_id = str(event_id)[6:] if str(event_id).startswith("intel:") else str(event_id)
                drift_counts[incident_id] = drift_counts.get(incident_id, 0) + int(count or 0)
            for incident_id, count in (
                db.query(SatelliteObservationDB.incident_id, func.count(SatelliteObservationDB.observation_id))
                .filter(SatelliteObservationDB.incident_id.in_(public_ids))
                .group_by(SatelliteObservationDB.incident_id)
                .all()
            ):
                satellite_counts[str(incident_id)] = int(count or 0)

        for item in combined:
            incident_id = str(item["incident_id"])
            item["evidence_counts"] = {
                "drift": drift_counts.get(incident_id, 0),
                "satellite": satellite_counts.get(incident_id, 0),
            }

    combined.sort(
        key=lambda item: str(item.get("last_update_at") or item.get("reported_at") or ""),
        reverse=True,
    )
    return combined


def _compute_play_counts() -> dict[str, Any]:
    """Compute an exact public catalog snapshot using the same eligibility as the index."""
    now = datetime.now(timezone.utc)
    catalog = _compute_play_catalog()
    humanitarian_count = sum(1 for item in catalog if item.get("domain") == "humanitarian")
    maritime_count = sum(1 for item in catalog if item.get("domain") == "maritime")
    investigation_count = sum(1 for item in catalog if item.get("domain") == "investigation")
    return {
        "total_count": len(catalog),
        "humanitarian_count": humanitarian_count,
        "maritime_count": maritime_count,
        "investigation_count": investigation_count,
        "generated_at": now.isoformat(),
    }


@router.get("/counts")
def play_counts():
    """Exact archive snapshot, isolated from the async request loop."""
    now_mono = monotonic()
    cached = _play_counts_cache.get("payload")
    cached_at = float(_play_counts_cache.get("at") or 0.0)
    if cached is not None and now_mono - cached_at < _PLAY_COUNTS_TTL_S:
        return cached

    with _play_counts_lock:
        now_mono = monotonic()
        cached = _play_counts_cache.get("payload")
        cached_at = float(_play_counts_cache.get("at") or 0.0)
        if cached is not None and now_mono - cached_at < _PLAY_COUNTS_TTL_S:
            return cached
        payload = _compute_play_counts()
        _play_counts_cache["payload"] = payload
        _play_counts_cache["at"] = monotonic()
        return payload


@router.get("/incidents")
def play_incidents(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    from core.db.models import IncidentTransitionDB
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    combined = _compute_play_catalog()
    page = [dict(item) for item in combined[offset:offset + limit]]

    page_human_ids = [item["incident_id"] for item in page if item.get("domain") == "humanitarian"]
    history: dict[str, list[dict[str, Any]]] = {incident_id: [] for incident_id in page_human_ids}
    if page_human_ids:
        with session_scope() as db:
            transitions = (
                db.query(IncidentTransitionDB)
                .filter(IncidentTransitionDB.incident_id.in_(page_human_ids))
                .order_by(IncidentTransitionDB.transition_at.asc())
                .all()
            )
            for transition in transitions:
                history.setdefault(transition.incident_id, []).append({
                    "at": _iso(transition.transition_at),
                    "from_state": transition.from_state,
                    "to_state": transition.to_state,
                })
    for item in page:
        item["status_history"] = history.get(item["incident_id"], [])

    next_offset = offset + len(page) if len(combined) > offset + len(page) else None
    return {
        "incidents": page,
        "offset": offset,
        "next_offset": next_offset,
        "total_count": len(combined),
        "generated_at": now.isoformat(),
    }



def _thread_item(incident_id: str, repost: dict, *, reported_at: str | None) -> dict[str, Any] | None:
    at = _iso(repost.get("posted_at"))
    if not at:
        return None
    note = str(repost.get("note") or "").strip()
    from core.intel.geoextract import is_concluded_incident

    if note and is_concluded_incident(note):
        item_type = "resolution"
    else:
        reported = parse_utc(reported_at or "")
        posted = parse_utc(at)
        item_type = (
            "attending_news"
            if reported and posted and (posted - reported).total_seconds() >= 24 * 3600
            else "update"
        )
    return {
        "id": str(repost.get("tweet_id") or f"update:{incident_id}:{at}"),
        "at": at,
        "type": item_type,
        "source": "source_update",
        "title": note or "Source update",
        "geometry": None,
        "properties": {
            "url": repost.get("url"),
            "kind": repost.get("kind"),
        },
    }


def _transition_item(row) -> dict[str, Any]:
    return {
        "id": row.transition_id,
        "at": _iso(row.transition_at),
        "type": "resolution" if row.to_state == "resolved" else "status",
        "source": "incident_state",
        "title": f"Status: {row.to_state}",
        "geometry": None,
        "properties": {
            "from_state": row.from_state,
            "to_state": row.to_state,
            "reason_code": row.reason_code,
            "review_required": bool(row.review_required),
        },
    }


def _drift_item(row) -> dict[str, Any]:
    metadata = dict(row.metadata_json or {})
    return {
        "id": row.drift_id,
        "at": _iso(row.created_at),
        "type": "drift",
        "source": "SeaCommons/OpenDrift",
        "title": "Drift forecast computed",
        "geometry": row.trajectory,
        "properties": {
            "drift_id": row.drift_id,
            "domain": row.domain,
            "model": metadata.get("model") or row.model_version or "OpenDrift",
            "forecast": True,
            "cone_24h": row.cone_24h,
            "impact_point": row.impact_point,
        },
    }


@router.get("/incidents/{incident_id}/timeline")
def play_incident_timeline(incident_id: str):
    from sqlalchemy import or_

    from core.db.models import (
        DriftResultDB,
        HumanitarianIncidentDB,
        IncidentTransitionDB,
        IntelEventDB,
        InvestigationHypothesisDB,
        MaritimeEpisodeDB,
        SatelliteObservationDB,
    )
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        hypothesis = db.get(InvestigationHypothesisDB, incident_id)
        if hypothesis is not None and _is_public_play_investigation(hypothesis):
            episode = db.get(MaritimeEpisodeDB, hypothesis.episode_id) if hypothesis.episode_id else None
            if episode is None:
                raise HTTPException(status_code=404, detail="Investigation not found")
            timeline = [{
                "id": hypothesis.hypothesis_id,
                "at": _iso(episode.start_at),
                "type": "hypothesis",
                "source": "SeaCommons evidence engine",
                "title": _investigation_projection(hypothesis, episode)["title"],
                "geometry": episode.geometry,
                "properties": {
                    "state": hypothesis.state,
                    "evidence_stage": hypothesis.evidence_stage,
                    "reason_codes": list(hypothesis.reason_codes or []),
                    "counter_indicators": list(hypothesis.counter_indicators or []),
                },
            }]
            for evidence_id in hypothesis.evidence_links or []:
                evidence = db.get(IntelEventDB, evidence_id)
                if evidence is None:
                    continue
                meta = dict(evidence.meta or {})
                geometry = ({"type": "Point", "coordinates": [evidence.lon, evidence.lat]}
                            if evidence.lat is not None and evidence.lon is not None else None)
                timeline.append({
                    "id": f"evidence:{evidence.id}", "at": _iso(evidence.timestamp_utc),
                    "type": "evidence", "source": evidence.source,
                    "title": str(meta.get("anomaly_type") or evidence.type).replace("_", " "),
                    "geometry": geometry,
                    "properties": {"evidence_stage": hypothesis.evidence_stage},
                })
            timeline = [item for item in timeline if item.get("at")]
            timeline.sort(key=lambda item: item["at"])
            return {
                "incident_id": incident_id, "incident_status": hypothesis.state,
                "surface": "play", "domain": "investigation",
                "timeline": timeline, "generated_at": now.isoformat(),
            }

        incident = db.get(HumanitarianIncidentDB, incident_id)
        event = db.get(IntelEventDB, incident_id)
        generic_maritime = incident is None and event is not None and _is_public_catalog_maritime(event)
        if incident is None and not generic_maritime:
            raise HTTPException(status_code=404, detail="Incident not found")
        incident_status = _status_for_row(incident, now=now) if incident is not None else _generic_maritime_status(event)
        domain = "humanitarian" if incident is not None else "maritime"
        timeline: list[dict[str, Any]] = []
        if event is not None:
            geometry = None
            if event.lat is not None and event.lon is not None:
                geometry = {"type": "Point", "coordinates": [event.lon, event.lat]}
            timeline.append({
                "id": f"report:{incident_id}",
                "at": _iso(event.timestamp_utc),
                "type": "report",
                "source": event.source,
                "title": event.title,
                "geometry": geometry,
                "properties": {"url": event.url or None},
            })
            for repost in (event.meta or {}).get("thread_reposts") or []:
                item = _thread_item(incident_id, repost, reported_at=incident.reported_at if incident is not None else event.timestamp_utc)
                if item is not None:
                    timeline.append(item)
        if incident is not None:
            transitions = (
                db.query(IncidentTransitionDB)
                .filter(IncidentTransitionDB.incident_id == incident_id)
                .order_by(IncidentTransitionDB.transition_at.asc())
                .all()
            )
            timeline.extend(_transition_item(row) for row in transitions)

        drifts = (
            db.query(DriftResultDB)
            .filter(
                DriftResultDB.status == "completed",
                or_(
                    DriftResultDB.event_id == incident_id,
                    DriftResultDB.event_id == f"intel:{incident_id}",
                ),
            )
            .order_by(DriftResultDB.created_at.asc())
            .all()
        )
        timeline.extend(_drift_item(row) for row in drifts)

        satellites = (
            db.query(SatelliteObservationDB)
            .filter(SatelliteObservationDB.incident_id == incident_id)
            .order_by(SatelliteObservationDB.acquisition_time.asc())
            .all()
        )
        timeline.extend(_satellite_item(row) for row in satellites)

    timeline = [item for item in timeline if item.get("at")]
    timeline.sort(key=lambda item: item["at"])
    return {
        "incident_id": incident_id,
        "incident_status": incident_status,
        "surface": "play",
        "domain": domain,
        "timeline": timeline,
        "generated_at": now.isoformat(),
    }


def _satellite_item(row) -> dict[str, Any]:
    return {
        "id": row.observation_id,
        "at": _iso(row.acquisition_time),
        "type": "satellite",
        "source": row.provider,
        "title": f"{row.mission} observation",
        "geometry": row.footprint,
        "properties": {
            "mission": row.mission,
            "product_id": row.product_id,
            "sensor_type": row.sensor_type,
            "temporal_relation": row.temporal_relation,
            "temporal_delta_s": row.temporal_delta_s,
            "asset_ref": row.asset_ref,
            "source_url": row.source_url,
            "bbox": row.bbox,
            "resolution_m": row.resolution_m,
            "cloud_cover": row.cloud_cover,
            "polarisation": row.polarisation,
            "evidence_status": row.evidence_status,
            "provenance": row.provenance or {},
        },
    }
