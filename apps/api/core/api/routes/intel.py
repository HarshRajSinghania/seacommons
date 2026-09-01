# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Maritime intelligence API routes.

GET  /api/v1/intel                   All events GeoJSON (filterable)
GET  /api/v1/intel/stats             Event counts by type/severity
GET  /api/v1/intel/sources           OSINT source health (registry)
GET  /api/v1/intel/ngo               NGO/coastguard vessel positions (GeoJSON)
GET  /api/v1/intel/ais-spikes        AIS anomaly events only
POST /api/v1/intel/extract-image     Extract GPS coords from an image URL
POST /api/v1/intel/auto-drift        Trigger SAR drift from a geolocated event
POST /api/v1/intel/manual            Manually inject an intel event
WS   /ws/intel                       Real-time event stream (JSON per event)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from core.api.ratelimit import rate_limit
from core.intel.drift_service import schedule_intel_drift
from core.intel.ingestion_service import store_external_event, store_manual_event
from core.intel.query_service import (
    ArchiveQueryError,
    archive_collection,
    geolocated_event_collection,
    intel_collection,
    intel_drift_collection,
    source_health,
)
from core.intel.store import intel_store

router = APIRouter()
logger = logging.getLogger(__name__)


# ── REST endpoints ─────────────────────────────────────────────────────────────


@router.get("/api/v1/intel")
async def get_intel(
    severity: Optional[str] = Query(None, description="critical|high|medium|low"),
    type_filter: Optional[str] = Query(
        None, alias="type", description="twitter|news|iom_incident|ais_spike|ngo_activity"
    ),
    tier: Optional[str] = Query(None, description="operational|news|signal"),
    limit: int = Query(200, ge=1, le=500),
    days: int = Query(30, ge=1, le=365, description="Only return events from the last N days"),
):
    """All intelligence events, ordered by operational priority, as GeoJSON."""
    return intel_collection(
        severity=severity,
        type_filter=type_filter,
        tier=tier,
        limit=limit,
        days=days,
    )


@router.get("/api/v1/intel/stats")
async def get_intel_stats():
    """Event counts by type and severity."""
    return intel_store.stats()


@router.get("/api/v1/intel/sources")
async def get_intel_sources():
    """OSINT source health: last poll time, events/h, error count per monitor."""
    return source_health()


@router.get("/api/v1/intel/ngo")
async def get_ngo_positions():
    """
    Live positions of known NGO and coastguard vessels from AIS registry.
    Enriched with org, role, and website data.
    Returns GeoJSON FeatureCollection.
    """
    from core.intel.ngo_registry import ngo_vessel_geojson

    return ngo_vessel_geojson()


@router.get("/api/v1/intel/ais-spikes")
async def get_ais_spikes(limit: int = Query(50, ge=1, le=200)):
    """AIS anomaly events (sudden stops, clusters, NGO search patterns)."""
    return geolocated_event_collection(type_filter="ais_spike", limit=limit)


@router.get("/api/v1/intel/twitter")
async def get_twitter_events(
    severity: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=300),
):
    """Social media (Twitter/X) distress signals only."""
    return geolocated_event_collection(
        type_filter="twitter",
        severity=severity,
        limit=limit,
        include_missing_count=True,
    )


@router.get("/api/v1/intel/incidents")
async def get_iom_incidents(limit: int = Query(50, ge=1, le=200)):
    """IOM Missing Migrants verified incidents."""
    return geolocated_event_collection(type_filter="iom_incident", limit=limit)


@router.get("/api/v1/intel/archive")
async def get_intel_archive(
    days: int = Query(7, ge=1, le=90, description="How many days back to query"),
    severity: Optional[str] = Query(None),
    type_filter: Optional[str] = Query(None, alias="type"),
    limit: int = Query(500, ge=1, le=2000),
    fmt: str = Query("geojson", description="geojson | json"),
):
    """Historical intelligence events from durable storage."""
    try:
        return archive_collection(
            days=days,
            severity=severity,
            type_filter=type_filter,
            limit=limit,
            fmt=fmt,
        )
    except ArchiveQueryError as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc


# ── Manual intel event injection ─────────────────────────────────────────────


class ManualIntelRequest(BaseModel):
    title: str
    text: str = ""
    source: str = "manual"
    severity: str = "medium"  # critical | high | medium | low
    type: str = "manual"  # manual | distress | news | twitter | ...
    lat: Optional[float] = None
    lon: Optional[float] = None
    url: str = ""
    linked_mmsi: str = ""


@router.post("/api/v1/intel/manual", status_code=201)
async def inject_manual_intel(body: ManualIntelRequest, request: Request):
    """Persist and broadcast an operator-supplied intelligence event."""
    rate_limit(request, max_per_minute=10, scope="intel-manual")
    if body.severity not in ("critical", "high", "medium", "low"):
        raise HTTPException(status_code=422, detail="severity must be critical|high|medium|low")

    event = store_manual_event(
        title=body.title,
        text=body.text,
        source=body.source,
        severity=body.severity,
        event_type=body.type,
        lat=body.lat,
        lon=body.lon,
        url=body.url,
        linked_mmsi=body.linked_mmsi,
    )
    if event is None:
        raise HTTPException(status_code=409, detail="Duplicate event — already in store")
    return {"id": event.id, "timestamp_utc": event.timestamp_utc, "stored": True}


# ── External shared-secret intel ingestion ───────────────────────────────────
# For an operator's own external script/service that produces already-parsed
# text reports (e.g. reading some feed the operator runs independently).
# SeaCommons has no visibility into and makes no claim about how that data
# was produced — this endpoint only accepts a finished {text, url, source}
# report and runs it through the same pipeline (dedup, coordinate
# extraction, triangulation, optional auto-drift) as any other monitor.
# Auth mirrors /api/v1/ingest/webhook: HMAC-SHA256 over the raw body using
# EXTERNAL_INTEL_INGEST_SECRET, not the OIDC operator-role login that
# /api/v1/intel/manual requires — meant for a standalone script, not a human
# in the console.


class ExternalIntelPayload(BaseModel):
    source: str  # short label for this feed, e.g. "personal-x-relay"
    source_id: str = ""  # upstream ID (e.g. tweet id) for dedup, if known
    text: str
    title: str = ""
    url: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    timestamp_utc: Optional[str] = None
    # Default False: the event still lands in intel_store (visible on the
    # authenticated console, feeds triangulation/auto-drift) but does not
    # appear on the public live.seacommons.org map until the operator
    # explicitly opts an item in — same conservative default as manual
    # console entries, which also don't auto-publish.
    publish: bool = False


@router.post("/api/v1/intel/external", status_code=201)
async def ingest_external_intel(request: Request):
    """Authenticate and ingest an operator-supplied text report."""
    import hashlib
    import hmac as hmac_lib

    from core.config import config

    expected = config.EXTERNAL_INTEL_INGEST_SECRET
    if not expected:
        raise HTTPException(status_code=503, detail="External intel ingest is not configured")
    raw = await request.body()
    if len(raw) > 20_000:
        raise HTTPException(status_code=413, detail="Payload too large")
    supplied = request.headers.get("x-seacommons-signature", "")
    digest = "sha256=" + hmac_lib.new(expected.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac_lib.compare_digest(supplied, digest):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        body = ExternalIntelPayload.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    rate_limit(request, max_per_minute=60, scope="intel-external")
    event, added = store_external_event(
        source=body.source,
        source_id=body.source_id,
        text=body.text,
        title=body.title,
        url=body.url,
        lat=body.lat,
        lon=body.lon,
        timestamp_utc=body.timestamp_utc,
        publish=body.publish,
    )
    return {"id": event.id, "stored": added, "published": body.publish}


# ── Image coordinate extraction ───────────────────────────────────────────────


class ImageExtractRequest(BaseModel):
    url: str


@router.get("/api/v1/intel/drifts")
async def get_intel_drifts():
    """All completed drift results linked to intelligence events."""
    return intel_drift_collection()


@router.get("/api/v1/media/{key}")
async def get_stored_media(key: str, request: Request):
    """Serve the PUBLIC, re-encoded thumbnail of an Alarm Phone source image
    (docs/prompt.md P1 C).

    This route serves ONLY the intentionally-public derivative written to the
    ``media/pub/`` prefix by media_evidence.capture_media_evidence -- never the
    private durable original (``media/orig/``) and never an arbitrary object
    store path. Key is ``<sha256>.<jpg|png>``; the lookup is content-addressed.

    Cache is short and revalidatable, not ``immutable``: there is no takedown
    workflow yet, so a stale-forever CDN copy of a distress-scene screenshot
    would be unremovable.
    """
    from fastapi import Response

    import re

    if not re.fullmatch(r"[0-9a-f]{64}\.(jpg|png)", key):
        raise HTTPException(status_code=404, detail="Not found")
    rate_limit(request, max_per_minute=120, scope="media")
    from core.object_store import get

    try:
        data = get(f"media/pub/{key}")
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise HTTPException(status_code=404, detail="Not found")
    mime = "image/png" if key.endswith(".png") else "image/jpeg"
    return Response(
        content=bytes(data),
        media_type=mime,
        headers={
            "Cache-Control": "public, max-age=3600, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(data)),
        },
    )


@router.post("/api/v1/intel/extract-image")
async def extract_image_coords(body: ImageExtractRequest, request: Request):
    """
    Fetch an image URL and extract GPS coordinates.
    Pipeline: EXIF metadata → Claude Vision (claude-haiku-4-5).
    Returns {lat, lon, method, confidence} or 404 if nothing found.
    """
    rate_limit(request, max_per_minute=10, scope="intel-image")
    from core.intel.vision import extract_from_url

    result = await extract_from_url(body.url)
    if result is None:
        raise HTTPException(status_code=404, detail="No coordinates found in image")
    return result


# ── Auto-drift from intel event ────────────────────────────────────────────────


class AutoDriftRequest(BaseModel):
    intel_event_id: str
    lat: float
    lon: float
    persons: Optional[int] = None
    vessel_type: Optional[str] = "rubber_boat"


@router.post("/api/v1/intel/auto-drift")
async def intel_auto_drift(body: AutoDriftRequest, request: Request):
    """
    Trigger a SAR drift simulation from an intel event's known position.
    The drift runs in a daemon thread so the response returns immediately.

    Public (unauthenticated) endpoint — reachable from the anonymous Live map.
    Protected the same way /api/v1/alert is: a per-IP rate limit plus the
    shared global concurrency semaphore (MAX_CONCURRENT_DRIFTS), so a burst of
    anonymous clicks cannot exhaust CPU/RAM on the pilot VM.
    """
    rate_limit(request, max_per_minute=6, scope="intel-drift")
    normalized_id = body.intel_event_id.removeprefix("intel:")
    # get_durable, not get: an Alarm Phone distress incident older than a few
    # hours is routinely evicted from the in-memory deque, and get() then
    # returned None -- which skipped the F-01 gate entirely and let an
    # anonymous caller's raw lat/lon through.
    stored = intel_store.get_durable(normalized_id)
    if stored is not None:
        from core.intel.drift_service import is_auto_drift_eligible

        # SeaCommons Drift is a humanitarian SAR model seeded from verified
        # location evidence only (docs/deep-research-report.md #17 hard
        # requirement; docs/fixes.md F-01). One positive gate covers domain
        # (not "not security" -- piracy is in the env-widenable public
        # allow-list), lifecycle, land/sea, and coordinate review quality, so
        # an anonymous caller cannot spin up a drift for a security event or
        # from a disputed/unverified OCR coordinate just by supplying an id.
        eligible, reason = is_auto_drift_eligible(stored)
        if not eligible:
            raise HTTPException(status_code=400, detail=f"Drift not eligible: {reason}")
    else:
        # No such event anywhere -- never seed a drift from a caller-supplied
        # id + lat/lon that matches nothing.
        raise HTTPException(status_code=404, detail="Unknown intel event")
    lat = stored.lat if stored.lat is not None else body.lat
    lon = stored.lon if stored.lon is not None else body.lon
    observed_at = stored.timestamp_utc or datetime.now(timezone.utc).isoformat()
    if not schedule_intel_drift(
        normalized_id,
        lat,
        lon,
        body.persons,
        body.vessel_type,
        observed_at,
    ):
        raise HTTPException(
            status_code=429,
            detail="Drift engine busy — too many concurrent simulations. Retry shortly.",
            headers={"Retry-After": "30"},
        )
    return {"status": "queued", "intel_event_id": normalized_id, "observed_at": observed_at}


# ── WebSocket real-time stream ─────────────────────────────────────────────────


@router.websocket("/ws/intel")
async def ws_intel(websocket: WebSocket):
    """
    Real-time intel stream.  Sends a JSON payload for every new IntelEvent
    as it arrives.  Client receives GeoJSON Feature objects.

    On connect: sends the last 50 events as a batch (type="snapshot").
    Then: individual events streamed as type="event".
    """
    from core.security import READ_ROLES, authorize_websocket

    protocol = await authorize_websocket(websocket, READ_ROLES)
    if protocol == "closed":
        return
    await websocket.accept(subprotocol=protocol)
    loop = asyncio.get_event_loop()
    intel_store.register_ws(websocket, loop)
    logger.info("Intel WebSocket client connected")

    try:
        # Snapshot of recent events on connect
        recent = intel_store.events(limit=50)
        import json

        snapshot = json.dumps(
            {
                "type": "snapshot",
                "features": [e.to_geojson_feature() for e in recent],
            }
        )
        await websocket.send_text(snapshot)

        # Keep connection alive; new events are pushed via intel_store.broadcast()
        while True:
            await asyncio.sleep(30)
            await websocket.send_text('{"type":"ping"}')

    except WebSocketDisconnect:
        logger.info("Intel WebSocket client disconnected")
    except Exception as exc:
        logger.warning("Intel WebSocket error: %s", exc)
    finally:
        intel_store.unregister_ws(websocket)
