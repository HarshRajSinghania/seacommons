# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private operator view of the canonical ingestion pipeline.

This surface is deliberately separate from public Live/Play. It exposes the
durable source-observation envelope, inbound connector payloads and normalized
IntelEvent output so an operator can inspect parsing/ingestion end to end.

The route is authenticated by a gateway secret injected server-side by the
operator reverse proxy. The browser never receives that secret.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func

from core.config import config
from core.db.models import (
    IngestedSignalDB,
    IntelEventDB,
    InvestigationHypothesisDB,
    MaritimeEpisodeDB,
    SourceObservationDB,
)
from core.db.session import session_scope

router = APIRouter(prefix="/api/v1/operator/ingestion", tags=["operator-ingestion"])


def _require_gateway(request: Request) -> None:
    expected = str(config.OPERATOR_GATEWAY_SECRET or "")
    if not expected:
        raise HTTPException(status_code=503, detail="Operator ingestion gateway is not configured")
    supplied = request.headers.get("x-seacommons-operator-gateway", "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Operator gateway authentication required")


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _since(hours: int) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)


def _source_observation(row: SourceObservationDB) -> dict[str, Any]:
    return {
        "kind": "source_observation",
        "observation_id": row.observation_id,
        "service": row.service,
        "lane": row.lane,
        "observation_type": row.observation_type,
        "source_name": row.source_name,
        "source_policy": row.source_policy,
        "source_id": row.source_id,
        "source_url": row.source_url or "",
        "observed_at": row.observed_at,
        "received_at": _dt(row.received_at),
        "raw_payload_hash": row.raw_payload_hash,
        "raw_payload_ref": row.raw_payload_ref or "",
        "lat": row.lat,
        "lon": row.lon,
        "location_precision": row.location_precision,
        "uncertainty_m": row.uncertainty_m,
        "subject_refs": list(row.subject_refs or []),
        "provenance": dict(row.provenance or {}),
        "schema_version": row.schema_version,
        "preservation_status": row.preservation_status,
    }


def _intel_event(row: IntelEventDB) -> dict[str, Any]:
    return {
        "kind": "parsed_event",
        "event_id": row.id,
        "timestamp_utc": row.timestamp_utc,
        "source_timestamp_utc": row.source_timestamp_utc,
        "received_at": _dt(row.received_at or row.created_at),
        "type": row.type,
        "severity": row.severity,
        "source": row.source,
        "title": row.title,
        "text": row.text or "",
        "url": row.url or "",
        "lat": row.lat,
        "lon": row.lon,
        "linked_mmsi": row.linked_mmsi or "",
        "maritime_domain": row.maritime_domain,
        "operational_tier": row.operational_tier,
        "humanitarian_case_type": row.humanitarian_case_type,
        "incident_lifecycle": row.incident_lifecycle,
        "location_status": row.location_status,
        "coordinate_review_status": row.coordinate_review_status,
        "location_uncertainty_m": row.location_uncertainty_m,
        "schema_version": row.schema_version,
        "metadata": dict(row.meta or {}),
    }


def _ingested_signal(row: IngestedSignalDB) -> dict[str, Any]:
    return {
        "kind": "ingested_signal",
        "signal_id": row.signal_id,
        "organization_id": row.organization_id,
        "connector_id": row.connector_id,
        "source_channel": row.source_channel,
        "source_id": row.source_id,
        "provider_message_id": row.provider_message_id,
        "received_at": _dt(row.received_at),
        "payload": dict(row.payload or {}),
    }


@router.get("/overview")
def operator_ingestion_overview(
    request: Request,
    hours: int = Query(24, ge=1, le=720),
) -> dict[str, Any]:
    _require_gateway(request)
    cutoff = _since(hours)
    with session_scope() as db:
        raw_total = (
            db.query(func.count(SourceObservationDB.observation_id))
            .filter(SourceObservationDB.received_at >= cutoff)
            .scalar()
            or 0
        )
        event_total = (
            db.query(func.count(IntelEventDB.id))
            .filter(IntelEventDB.received_at >= cutoff)
            .scalar()
            or 0
        )
        signal_total = (
            db.query(func.count(IngestedSignalDB.signal_id))
            .filter(IngestedSignalDB.received_at >= cutoff)
            .scalar()
            or 0
        )
        raw_by_source = dict(
            db.query(SourceObservationDB.source_name, func.count(SourceObservationDB.observation_id))
            .filter(SourceObservationDB.received_at >= cutoff)
            .group_by(SourceObservationDB.source_name)
            .order_by(func.count(SourceObservationDB.observation_id).desc())
            .limit(100)
            .all()
        )
        raw_by_type = dict(
            db.query(SourceObservationDB.observation_type, func.count(SourceObservationDB.observation_id))
            .filter(SourceObservationDB.received_at >= cutoff)
            .group_by(SourceObservationDB.observation_type)
            .order_by(func.count(SourceObservationDB.observation_id).desc())
            .limit(100)
            .all()
        )
        events_by_source = dict(
            db.query(IntelEventDB.source, func.count(IntelEventDB.id))
            .filter(IntelEventDB.received_at >= cutoff)
            .group_by(IntelEventDB.source)
            .order_by(func.count(IntelEventDB.id).desc())
            .limit(100)
            .all()
        )
        events_by_type = dict(
            db.query(IntelEventDB.type, func.count(IntelEventDB.id))
            .filter(IntelEventDB.received_at >= cutoff)
            .group_by(IntelEventDB.type)
            .order_by(func.count(IntelEventDB.id).desc())
            .limit(100)
            .all()
        )
        newest_raw = db.query(func.max(SourceObservationDB.received_at)).scalar()
        newest_event = db.query(func.max(IntelEventDB.received_at)).scalar()
        newest_signal = db.query(func.max(IngestedSignalDB.received_at)).scalar()

    try:
        from core.acquisition.status import acquisition_status_sources
        acquisition = acquisition_status_sources()
    except Exception:
        acquisition = []
    try:
        from core.intel.source_registry import source_registry
        sources = source_registry.get_all()
    except Exception:
        sources = []

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": hours,
        "counts": {
            "source_observations": int(raw_total),
            "parsed_events": int(event_total),
            "ingested_signals": int(signal_total),
        },
        "newest": {
            "source_observation": _dt(newest_raw),
            "parsed_event": _dt(newest_event),
            "ingested_signal": _dt(newest_signal),
        },
        "source_observations": {
            "by_source": {str(k): int(v) for k, v in raw_by_source.items()},
            "by_type": {str(k): int(v) for k, v in raw_by_type.items()},
        },
        "parsed_events": {
            "by_source": {str(k): int(v) for k, v in events_by_source.items()},
            "by_type": {str(k): int(v) for k, v in events_by_type.items()},
        },
        "acquisition": acquisition,
        "sources": sources,
        "pipeline_status": __import__(
            "core.api.routes.status", fromlist=["build_public_status"]
        ).build_public_status(hours),
        "note": (
            "SourceObservation stores the canonical immutable envelope, hash/reference and provenance. "
            "Payload bytes are not stored inline; normalized text and parser output are visible in /events."
        ),
    }


@router.get("/observations")
def operator_source_observations(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    hours: int = Query(24, ge=1, le=720),
    source: str | None = None,
    observation_type: str | None = None,
    service: str | None = None,
    lane: str | None = None,
) -> dict[str, Any]:
    _require_gateway(request)
    with session_scope() as db:
        query = db.query(SourceObservationDB).filter(SourceObservationDB.received_at >= _since(hours))
        if source:
            query = query.filter(SourceObservationDB.source_name == source)
        if observation_type:
            query = query.filter(SourceObservationDB.observation_type == observation_type)
        if service:
            query = query.filter(SourceObservationDB.service == service)
        if lane:
            query = query.filter(SourceObservationDB.lane == lane)
        rows = query.order_by(SourceObservationDB.received_at.desc()).limit(limit).all()
    return {"observations": [_source_observation(row) for row in rows], "count": len(rows)}


@router.get("/signals")
def operator_ingested_signals(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    hours: int = Query(24, ge=1, le=720),
    source_channel: str | None = None,
) -> dict[str, Any]:
    _require_gateway(request)
    with session_scope() as db:
        query = db.query(IngestedSignalDB).filter(IngestedSignalDB.received_at >= _since(hours))
        if source_channel:
            query = query.filter(IngestedSignalDB.source_channel == source_channel)
        rows = query.order_by(IngestedSignalDB.received_at.desc()).limit(limit).all()
    return {"signals": [_ingested_signal(row) for row in rows], "count": len(rows)}


@router.get("/events")
def operator_parsed_events(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    hours: int = Query(24, ge=1, le=720),
    source: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    _require_gateway(request)
    with session_scope() as db:
        query = db.query(IntelEventDB).filter(IntelEventDB.received_at >= _since(hours))
        if source:
            query = query.filter(IntelEventDB.source == source)
        if event_type:
            query = query.filter(IntelEventDB.type == event_type)
        rows = query.order_by(IntelEventDB.received_at.desc()).limit(limit).all()
    return {"events": [_intel_event(row) for row in rows], "count": len(rows)}


@router.get("/stream")
def operator_ingestion_stream(
    request: Request,
    limit: int = Query(150, ge=1, le=500),
    hours: int = Query(24, ge=1, le=720),
) -> dict[str, Any]:
    _require_gateway(request)
    each = min(limit, 250)
    with session_scope() as db:
        observations = (
            db.query(SourceObservationDB)
            .filter(SourceObservationDB.received_at >= _since(hours))
            .order_by(SourceObservationDB.received_at.desc())
            .limit(each)
            .all()
        )
        events = (
            db.query(IntelEventDB)
            .filter(IntelEventDB.received_at >= _since(hours))
            .order_by(IntelEventDB.received_at.desc())
            .limit(each)
            .all()
        )
        signals = (
            db.query(IngestedSignalDB)
            .filter(IngestedSignalDB.received_at >= _since(hours))
            .order_by(IngestedSignalDB.received_at.desc())
            .limit(each)
            .all()
        )

    items = (
        [_source_observation(row) for row in observations]
        + [_intel_event(row) for row in events]
        + [_ingested_signal(row) for row in signals]
    )
    items.sort(key=lambda item: str(item.get("received_at") or item.get("timestamp_utc") or ""), reverse=True)
    return {"items": items[:limit], "count": min(len(items), limit)}


@router.get("/analysis")
def operator_analysis_outputs(
    request: Request,
    limit: int = Query(80, ge=1, le=250),
    hours: int = Query(24, ge=1, le=720),
) -> dict[str, Any]:
    _require_gateway(request)
    cutoff = _since(hours)
    with session_scope() as db:
        episodes = (
            db.query(MaritimeEpisodeDB)
            .filter(MaritimeEpisodeDB.updated_at >= cutoff)
            .order_by(MaritimeEpisodeDB.updated_at.desc())
            .limit(limit)
            .all()
        )
        hypotheses = (
            db.query(InvestigationHypothesisDB)
            .filter(InvestigationHypothesisDB.updated_at >= cutoff)
            .order_by(InvestigationHypothesisDB.updated_at.desc())
            .limit(limit)
            .all()
        )

    episode_rows = [
        {
            "kind": "maritime_episode",
            "id": row.episode_id,
            "family": row.episode_family,
            "status": row.status,
            "verification_status": row.verification_status,
            "subjects": len(row.subject_ids or []),
            "observations": len(row.observation_ids or []),
            "independence_groups": len(row.independence_groups or []),
            "start_at": _dt(row.start_at),
            "end_at": _dt(row.end_at),
            "updated_at": _dt(row.updated_at),
        }
        for row in episodes
    ]
    hypothesis_rows = [
        {
            "kind": "investigation_hypothesis",
            "id": row.hypothesis_id,
            "episode_id": row.episode_id,
            "hypothesis_type": row.hypothesis_type,
            "state": row.state,
            "evidence_stage": row.evidence_stage,
            "evidence_links": len(row.evidence_links or []),
            "reason_codes": list(row.reason_codes or []),
            "updated_at": _dt(row.updated_at),
        }
        for row in hypotheses
    ]
    items = episode_rows + hypothesis_rows
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("end_at") or ""), reverse=True)
    return {
        "items": items[:limit],
        "episodes": len(episode_rows),
        "hypotheses": len(hypothesis_rows),
        "count": min(len(items), limit),
    }
