# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public, privacy-preserving projection of SeaCommons live signals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from core.config import config
from core.intel.store import intel_store
from core.live.feed import public_drift_collection, public_signal_collection
from core.live.projection import _public_intel_feature

router = APIRouter(prefix="/api/v1/live", tags=["live"])


@router.get("/signals")
async def live_signals(
    limit: int = Query(300, ge=1, le=500),
    days: int = Query(30, ge=1, le=365),
    since: Optional[str] = Query(None),
    mode: str = Query(
        "humanitarian",
        pattern="^(humanitarian|maritime|security|all)$",
        description=(
            "humanitarian: public-eligible SAR/humanitarian output. "
            "maritime: public-eligible Safety plus reviewed/published Maritime output. "
            "security is a temporary alias for maritime. all: both compartments."
        ),
    ),
):
    """Public map-ready signal feed. No private inbound content is returned."""
    return public_signal_collection(limit=limit, days=days, since=since, mode=mode)


@router.get("/sanctioned-vessels")
async def live_sanctioned_vessels(request: Request):
    """Fresh AIS positions for vessels matched to public sanctions lists.

    This is a narrow public overlay, not the raw AIS fleet. Only strong
    identifier matches (MMSI or IMO) and positions no older than 10 minutes
    are returned. A list match is identity context, not evidence that current
    vessel behaviour is unlawful.
    """
    from sqlalchemy import or_

    from core.api.ratelimit import rate_limit
    from core.db.models import SanctionedVesselDB
    from core.db.session import session_scope
    from core.vessels.registry import registry

    rate_limit(request, max_per_minute=60, scope="live-sanctioned-vessels")
    current = registry.get_geojson()
    vessel_features = list(current.get("features") or [])
    mmsis = {
        str((feature.get("properties") or {}).get("mmsi") or "").strip()
        for feature in vessel_features
        if str((feature.get("properties") or {}).get("mmsi") or "").strip()
    }
    imos = {
        str((feature.get("properties") or {}).get("imo") or "").strip()
        for feature in vessel_features
        if str((feature.get("properties") or {}).get("imo") or "").strip()
    }
    if not mmsis and not imos:
        return {
            "type": "FeatureCollection",
            "features": [],
            "meta": {
                "total": 0,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "position_policy": "fresh_only_10m",
            },
        }

    conditions = []
    if mmsis:
        conditions.append(SanctionedVesselDB.mmsi.in_(mmsis))
    if imos:
        conditions.append(SanctionedVesselDB.imo.in_(imos))
    with session_scope() as db:
        rows = [
            {
                "source_list": row.source_list,
                "program": row.program,
                "mmsi": row.mmsi,
                "imo": row.imo,
            }
            for row in db.query(SanctionedVesselDB).filter(or_(*conditions)).all()
        ]

    by_mmsi: dict[str, list[dict]] = {}
    by_imo: dict[str, list[dict]] = {}
    for row in rows:
        if row["mmsi"]:
            by_mmsi.setdefault(str(row["mmsi"]), []).append(row)
        if row["imo"]:
            by_imo.setdefault(str(row["imo"]), []).append(row)

    now = datetime.now(timezone.utc)
    fresh_cutoff = now - timedelta(minutes=10)
    out = []
    for feature in vessel_features:
        properties = dict(feature.get("properties") or {})
        mmsi = str(properties.get("mmsi") or "").strip()
        imo = str(properties.get("imo") or "").strip()
        matches = list(by_mmsi.get(mmsi, []))
        if imo:
            matches.extend(by_imo.get(imo, []))
        if not matches or not feature.get("geometry"):
            continue
        seen_raw = properties.get("last_seen")
        try:
            seen = datetime.fromisoformat(str(seen_raw).replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            seen = seen.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if seen < fresh_cutoff or seen > now + timedelta(minutes=5):
            continue

        unique_matches = {
            (
                str(row.get("source_list") or ""),
                str(row.get("program") or ""),
                str(row.get("mmsi") or ""),
                str(row.get("imo") or ""),
            ): row
            for row in matches
        }.values()
        match_basis = []
        if mmsi and mmsi in by_mmsi:
            match_basis.append("mmsi")
        if imo and imo in by_imo:
            match_basis.append("imo")
        lists = sorted(
            {str(row["source_list"]) for row in unique_matches if row.get("source_list")}
        )
        programs = sorted(
            {str(row["program"]) for row in unique_matches if row.get("program")}
        )
        out.append({
            "type": "Feature",
            "geometry": feature["geometry"],
            "properties": {
                "mmsi": mmsi,
                "imo": imo or None,
                "ship_name": properties.get("ship_name") or properties.get("name") or mmsi,
                "ship_type": properties.get("ship_type"),
                "speed": properties.get("speed"),
                "course": properties.get("course"),
                "heading": properties.get("heading"),
                "nav_status": properties.get("nav_status"),
                "last_seen": seen.isoformat(),
                "position_age_s": round(max(0.0, (now - seen).total_seconds()), 1),
                "main_category": "maritime",
                "facet": "sanctions",
                "sanctions_matched": True,
                "sanctions_lists": lists,
                "sanctions_programs": programs,
                "match_basis": match_basis,
                "position_policy": "fresh_only_10m",
                "note": (
                    "Public sanctions-list identity match; this does not by itself "
                    "establish unlawful current behaviour."
                ),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": out,
        "meta": {
            "total": len(out),
            "generated_at": now.isoformat(),
            "position_policy": "fresh_only_10m",
        },
    }


@router.get("/signals/{event_id}/response")
async def live_signal_response(event_id: str, request: Request):
    """
    On-demand NGO cross-check for a single live episode.

    Returns which known SAR NGO / coastguard vessels are within range and
    heading toward the episode (bearing/ETA/heading), when each track was last
    saved to the AIS registry, recent motion flags (speed spike, search
    pattern, sudden stop, loitering, rescue cluster — reused from the AIS
    spike detector's stored observations), and other live signals nearby
    (cross-check). Also returns a GeoJSON FeatureCollection (`geojson`) of the
    episode→vessel lines and vessel points for direct map rendering.

    Privacy: only public AIS telemetry and the already-public signal metadata
    are returned — never raw messages or identifiers. Anonymous + rate limited
    like the other live read endpoints.
    """
    from core.api.ratelimit import rate_limit
    from core.intel.ngo_response import analyze_ngo_response

    rate_limit(request, max_per_minute=30, scope="live-response")
    normalized = event_id.removeprefix("intel:")
    event = intel_store.get(normalized)
    if event is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    if event.lat is None or event.lon is None:
        raise HTTPException(status_code=422, detail="Signal has no position")
    if _public_intel_feature(event) is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    related_signals = [
        other
        for other in intel_store.events(limit=300, max_age_days=3)
        if _public_intel_feature(other) is not None
    ]
    try:
        return analyze_ngo_response(event, related_signals=related_signals)
    except ValueError:
        raise HTTPException(status_code=422, detail="Signal has no position")


@router.get("/vessels/{mmsi}/context")
async def live_vessel_context(
    mmsi: str,
    request: Request,
    hours: float = Query(default=168.0, ge=1, le=24 * 30),
):
    """Public-AIS identity, observed track and sanctions-list explanation."""
    from core.api.ratelimit import rate_limit
    from core.api.routes.mda import build_vessel_dossier

    rate_limit(request, max_per_minute=30, scope="live-vessel-context")
    if re.fullmatch(r"\d{9}", mmsi) is None:
        raise HTTPException(status_code=422, detail="MMSI must contain exactly 9 digits")
    return build_vessel_dossier(mmsi, hours=hours, track_limit=240)


@router.get("/drifts")
async def live_drifts(limit: int = Query(100, ge=1, le=200)):
    """Public map-ready drift products, kept separate from received signals."""
    return public_drift_collection(limit=limit)


@router.get("/hypotheses")
async def live_hypotheses(limit: int = Query(100, ge=1, le=200)):
    """Published Maritime Intelligence hypotheses (docs/fixes.md M14.3/
    M14.4) -- core.intel.publication_policy.project_public_maritime_
    assessed() is the sole authority for what this returns; a hypothesis
    still in candidate/collecting/review_ready/assessed never appears
    here."""
    from core.intel.hypothesis_publication import public_hypothesis_collection

    return public_hypothesis_collection(limit=limit)


@router.get("/archives")
async def live_archives(limit: int = Query(40, ge=1, le=200)):
    """Anonymised incident index for the Play archive timeline.

    Two kinds of archived incident carry a computed drift: SAR cases opened
    through /api/v1/alert, and OSINT distress events (Alarm Phone, ...) whose
    auto-drift completed. Both are surfaced here, newest first, with only the
    coarse fields Play needs — never the source message or any identifier.
    """
    from core.db.models import DriftResultDB, IntelEventDB
    from core.db.session import session_scope
    from core.db.store import list_alerts

    seen: set[str] = set()
    archives: list[dict] = []

    for alert in list_alerts(limit=limit * 3):
        if alert.get("status") != "completed":
            continue
        event = alert.get("event") or {}
        archives.append({
            "id": alert["event_id"],
            "timestamp": event.get("timestamp"),
            "lat": event.get("lat"),
            "lon": event.get("lon"),
            "vessel_type": event.get("vessel_type") or "case",
            "persons": event.get("persons") or 1,
            "kind": "sar_case",
        })
        seen.add(alert["event_id"])

    try:
        from sqlalchemy import func, literal, or_

        with session_scope() as db:
            # An intel auto-drift persists drift_results.event_id as
            # "intel:<id>" (see core.intel.drift_service); the seeded SAR
            # archives use the bare id. Match both.
            rows = (
                db.query(DriftResultDB, IntelEventDB)
                .join(
                    IntelEventDB,
                    or_(
                        DriftResultDB.event_id == IntelEventDB.id,
                        DriftResultDB.event_id == literal("intel:") + IntelEventDB.id,
                    ),
                )
                .filter(DriftResultDB.status == "completed")
                .filter(IntelEventDB.lat.isnot(None))
                .order_by(func.coalesce(DriftResultDB.created_at, IntelEventDB.created_at).desc())
                .limit(limit * 4)
                .all()
            )
            for drift, ev in rows:
                if ev.id in seen:
                    continue
                meta = ev.meta or {}
                if not (meta.get("is_distress") or ev.type in {"distress", "vessel_incident"}):
                    continue
                archives.append({
                    "id": ev.id,
                    "timestamp": ev.timestamp_utc,
                    "lat": ev.lat,
                    "lon": ev.lon,
                    "vessel_type": meta.get("vessel_type") or "rubber_boat",
                    "persons": meta.get("persons") or meta.get("people") or 1,
                    "kind": "osint_distress",
                })
                seen.add(ev.id)
    except Exception:  # pragma: no cover - archive listing is best-effort
        pass

    archives.sort(key=lambda a: str(a.get("timestamp") or ""), reverse=True)
    return {
        "archives": archives[:limit],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/archives/{event_id}/geojson")
async def live_archive_geojson(event_id: str):
    """Derived geometry for a public Play archive; no source message or identity."""
    from fastapi import HTTPException

    from core.db.store import get_alert, get_drift

    alert = get_alert(event_id)
    drift = get_drift(event_id)  # seeded SAR archives: drift_id == event_id
    if drift is None:
        # An intel auto-drift row is keyed by job id; its event_id is
        # "intel:<id>" (or the bare id). Take the newest completed one.
        from core.db.models import DriftResultDB
        from core.db.session import session_scope
        from core.db.store import drift_to_dict
        from sqlalchemy import func, or_

        with session_scope() as db:
            row = (
                db.query(DriftResultDB)
                .filter(
                    DriftResultDB.status == "completed",
                    or_(
                        DriftResultDB.event_id == event_id,
                        DriftResultDB.event_id == f"intel:{event_id}",
                    ),
                )
                .order_by(func.coalesce(DriftResultDB.created_at, None).desc())
                .first()
            )
            drift = drift_to_dict(row)

    alert_ok = alert is not None and alert.get("status") == "completed"
    drift_ok = bool(drift) and drift.get("status") == "completed"
    if not drift_ok or (alert is not None and not alert_ok):
        raise HTTPException(status_code=404, detail="Archive not found")
    features = [
        feature
        for feature in (
            drift.get("trajectory"),
            drift.get("cone_6h"),
            drift.get("cone_12h"),
            drift.get("cone_24h"),
        )
        if feature
    ]
    features.extend((drift.get("impact_point") or {}).get("features", []))
    return {"type": "FeatureCollection", "features": features}


@router.get("/pipeline")
async def live_pipeline():
    """Public-safe health for the single SeaCommons acquisition pipeline."""
    from core.acquisition.status import (
        acquisition_status_sources,
        ensure_default_acquisition_status,
    )

    ensure_default_acquisition_status()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": acquisition_status_sources(),
    }


@router.get("/receivers/catalog")
async def live_receiver_catalog(
    zone: str = Query("central_med", pattern="^(central_med|sicily_channel|malta|tunisia_north|ionian)$"),
    limit: int = Query(16, ge=1, le=64),
):
    """Public-safe ranked catalog of free/open receiver candidates."""
    from core.radio.catalog import rank_catalog

    rows = rank_catalog(zone, frequency_hz=2_187_500, limit=limit)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "zone": zone,
        "frequency_hz": 2_187_500,
        "receivers": [
            {
                "receiver_id": row.receiver_id,
                "station_label": row.public_label,
                "network_family": row.network_family,
                "country": row.country,
                "license_class": row.license_class,
                "activation_status": row.activation_status,
                "terms_status": row.terms_status,
                "priority_score": row.priority_score,
            }
            for row in rows
        ],
    }


@router.get("/receivers/discovery")
async def live_receiver_discovery(
    limit: int = Query(50, ge=1, le=100),
):
    """Public-safe snapshot of automatically discovered receiver candidates."""
    from core.radio.discovery import discovery_registry

    return discovery_registry.public_snapshot(limit=limit)


@router.get("/receivers/mesh")
async def live_receiver_mesh(
    limit: int = Query(16, ge=1, le=64),
):
    """Public-safe computed receiver mesh summary plus current active state."""
    from core.radio.catalog_store import public_catalog_summary
    from core.radio.runtime import get_remote_radio_status

    summary = public_catalog_summary(limit=limit)
    runtime = get_remote_radio_status(include_receivers=True)
    active_ids = {
        row.get("receiver_id")
        for row in runtime.get("receivers", [])
        if row.get("state") == "connected"
    }
    runtime_by_id = {
        str(row.get("receiver_id")): row
        for row in runtime.get("receivers", [])
        if row.get("receiver_id")
    }
    listen_ids: set[str] = set()
    if config.RADIO_LIVE_LISTEN_ENABLED:
        from core.radio.listen import listen_eligible_receiver_ids

        listen_ids = listen_eligible_receiver_ids({str(value) for value in active_ids if value})
    receivers = []
    for row in summary.get("receivers", []):
        item = dict(row)
        item["active"] = item.get("receiver_id") in active_ids
        item["listen_available"] = item.get("receiver_id") in listen_ids
        runtime_row = runtime_by_id.get(str(item.get("receiver_id") or ""), {})
        item["frequency_hz"] = runtime_row.get("frequency_hz") if item["active"] else None
        item["mode"] = runtime_row.get("mode") if item["active"] else None
        receivers.append(item)
    return {**summary, "active": len(active_ids), "receivers": receivers}


@router.get("/radio/events")
async def live_radio_events(
    limit: int = Query(50, ge=1, le=200),
):
    """Public-safe recent correlated RF events; no receiver lineage leakage."""
    from core.db.models import RadioEventDB
    from core.db.session import session_scope

    with session_scope() as db:
        rows = (
            db.query(RadioEventDB)
            .order_by(RadioEventDB.started_at.desc())
            .limit(limit)
            .all()
        )
        events = [
            {
                "event_id": row.event_id,
                "frequency_hz": row.frequency_hz,
                "started_at": row.started_at.isoformat(),
                "ended_at": row.ended_at.isoformat(),
                "independent_receivers": row.independent_receivers,
                "confidence": row.confidence,
            }
            for row in rows
        ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }


@router.get("/radio/messages")
async def live_radio_messages(
    limit: int = Query(50, ge=1, le=200),
):
    """Public-safe decoded DSC/NAVTEX messages; no receiver identity leakage."""
    from core.db.models import RadioAISAssociationDB, SourceObservationDB
    from core.db.session import session_scope
    from core.radio.public_messages import project_public_radio_message

    with session_scope() as db:
        rows = (
            db.query(SourceObservationDB)
            .filter(SourceObservationDB.observation_type.in_(("dsc_message", "navtex_message")))
            .order_by(SourceObservationDB.received_at.desc())
            .limit(limit)
            .all()
        )
        observation_ids = [row.observation_id for row in rows]
        associations = {}
        if observation_ids:
            for assoc in db.query(RadioAISAssociationDB).filter(
                RadioAISAssociationDB.observation_id.in_(observation_ids)
            ):
                associations[assoc.observation_id] = {
                    "mmsi": assoc.mmsi,
                    "match_status": assoc.match_status,
                    "confidence": assoc.confidence,
                    "distance_km": assoc.distance_km,
                    "ais_observed_at": assoc.ais_observed_at,
                    "episode_eligible": assoc.episode_eligible,
                }
        messages = []
        for row in rows:
            item = project_public_radio_message(row)
            if row.observation_id in associations:
                item["ais_association"] = associations[row.observation_id]
            messages.append(item)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
    }


@router.get("/radio/listen/{receiver_id}")
async def live_radio_listen_http(
    receiver_id: str,
    stream_seconds: int = Query(default=8, ge=1, le=10),
):
    """Short-lived ephemeral PCM stream for HTTPS clients; audio is never persisted."""
    import queue as queue_module

    from core.radio.listen import listen_broker, listen_eligible_receiver_ids
    from core.radio.runtime import get_remote_radio_status

    if not config.RADIO_LIVE_LISTEN_ENABLED:
        raise HTTPException(status_code=503, detail="live listen disabled")

    runtime = get_remote_radio_status(include_receivers=True)
    active_ids = {
        str(row.get("receiver_id"))
        for row in runtime.get("receivers", [])
        if row.get("receiver_id") and row.get("state") == "connected"
    }
    if receiver_id not in listen_eligible_receiver_ids(active_ids):
        raise HTTPException(status_code=404, detail="receiver unavailable")
    try:
        subscription = listen_broker.subscribe(receiver_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="listen capacity reached") from exc

    async def pcm_stream():
        deadline = asyncio.get_running_loop().time() + stream_seconds
        try:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    packet = await asyncio.to_thread(subscription.queue.get, True, 1.0)
                except queue_module.Empty:
                    continue
                yield packet
        finally:
            listen_broker.unsubscribe(subscription)

    return StreamingResponse(
        pcm_stream(),
        media_type="audio/L16",
        headers={
            "Cache-Control": "no-store",
            "X-SeaCommons-Audio-Encoding": "pcm_s16le",
            "X-SeaCommons-Sample-Rate": "12000",
            "X-SeaCommons-Channels": "1",
            "X-SeaCommons-Persistent": "false",
            "X-SeaCommons-Listen-Max-Seconds": str(max(1, min(int(config.RADIO_LIVE_LISTEN_MAX_SECONDS), 600))),
        },
    )


@router.websocket("/radio/listen/{receiver_id}")
async def live_radio_listen(websocket: WebSocket, receiver_id: str):
    """Ephemeral PCM listen stream for an already-active, terms-allowed receiver."""
    import queue as queue_module

    from core.radio.listen import listen_broker, listen_eligible_receiver_ids
    from core.radio.runtime import get_remote_radio_status

    if not config.RADIO_LIVE_LISTEN_ENABLED:
        await websocket.close(code=1008, reason="live listen disabled")
        return

    runtime = get_remote_radio_status(include_receivers=True)
    active_ids = {
        str(row.get("receiver_id"))
        for row in runtime.get("receivers", [])
        if row.get("receiver_id") and row.get("state") == "connected"
    }
    if receiver_id not in listen_eligible_receiver_ids(active_ids):
        await websocket.close(code=1008, reason="receiver unavailable")
        return
    try:
        subscription = listen_broker.subscribe(receiver_id)
    except RuntimeError:
        await websocket.close(code=1013, reason="listen capacity reached")
        return

    await websocket.accept()
    max_seconds = max(1, min(int(config.RADIO_LIVE_LISTEN_MAX_SECONDS), 600))
    await websocket.send_json({
        "type": "audio_format",
        "encoding": "pcm_s16le",
        "sample_rate_hz": 12_000,
        "channels": 1,
        "persistent": False,
        "max_seconds": max_seconds,
    })
    deadline = asyncio.get_running_loop().time() + max_seconds
    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                packet = await asyncio.to_thread(subscription.queue.get, True, 1.0)
            except queue_module.Empty:
                continue
            await websocket.send_bytes(packet)
    except WebSocketDisconnect:
        pass
    finally:
        listen_broker.unsubscribe(subscription)
        try:
            await websocket.close(code=1000)
        except Exception:
            pass


@router.get("/sources")
async def live_sources():
    """Public health summary without credentials, endpoint URLs or raw errors."""
    from core.intel.source_registry import source_registry

    registry_sources = {
        source["name"]: source
        for source in source_registry.get_all()
        if source["name"]
        in {
            "X / Twitter",
            "X / Twitter (twikit)",
            "Mastodon",
            "Official NGO RSS",
            "GDACS",
            "Bluesky",
        }
    }
    try:
        from core.connectors.service import status_counts
        from core.db.session import session_scope

        with session_scope() as db:
            whatsapp_connectors = status_counts(db, "whatsapp_cloud")
    except Exception:
        whatsapp_connectors = {}
    whatsapp_ready = bool(
        config.META_APP_ID
        and config.META_APP_SECRET
        and config.META_WEBHOOK_VERIFY_TOKEN
        and whatsapp_connectors.get("active", 0)
    )
    expected = (
        ("X / Twitter (twikit)", "twitter", bool(config.TWIKIT_ENABLED)),
        ("X / Twitter", "twitter", bool(config.TWITTER_BEARER_TOKEN)),
        ("WhatsApp partner intake", "whatsapp", whatsapp_ready),
        (
            "Telegram intake",
            "telegram",
            bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_WEBHOOK_SECRET),
        ),
        ("Partner webhook", "partner", bool(config.PARTNER_WEBHOOK_SECRET)),
    )
    sources = []
    for name, source_type, configured in expected:
        observed = registry_sources.pop(name, None)
        sources.append(
            {
                "name": name,
                "type": source_type,
                "status": observed["status"]
                if observed
                else ("pending" if configured else "offline"),
                "last_poll_at": observed["last_poll_at"] if observed else None,
                "events_last_hour": observed["events_last_hour"] if observed else 0,
                "total_events": observed["total_events"] if observed else 0,
                "consecutive_errors": observed["consecutive_errors"] if observed else 0,
                "pipeline_status": observed.get("pipeline_status", observed["status"])
                if observed
                else ("pending" if configured else "offline"),
                "source_status": observed.get("source_status", "unknown")
                if observed
                else "unknown",
                "configured": observed.get("configured", int(configured))
                if observed
                else int(configured),
                "reachable": observed.get("reachable", 0) if observed else 0,
                "handles": [
                    {
                        "name": handle["name"],
                        "status": handle["status"],
                        "last_poll_at": handle["last_poll_at"],
                        "total_events": handle["total_events"],
                    }
                    for handle in observed.get("handles", [])
                ]
                if observed
                else [],
            }
        )
    for observed in registry_sources.values():
        sources.append(
            {
                "name": observed["name"],
                "type": observed["type"],
                "status": observed["status"],
                "last_poll_at": observed["last_poll_at"],
                "events_last_hour": observed["events_last_hour"],
                "total_events": observed["total_events"],
                "consecutive_errors": observed["consecutive_errors"],
                "pipeline_status": observed.get("pipeline_status", observed["status"]),
                "source_status": observed.get("source_status", "unknown"),
                "configured": observed.get("configured", 0),
                "reachable": observed.get("reachable", 0),
                "handles": [
                    {
                        "name": handle["name"],
                        "status": handle["status"],
                        "last_poll_at": handle["last_poll_at"],
                        "total_events": handle["total_events"],
                    }
                    for handle in observed.get("handles", [])
                ],
            }
        )
    active = sum(1 for source in sources if source["status"] == "active")
    return {
        "sources": sources,
        "summary": {
            "total": len(sources),
            "active": active,
            "degraded": sum(1 for source in sources if source["status"] == "degraded"),
            "offline": sum(1 for source in sources if source["status"] == "offline"),
            "pending": sum(1 for source in sources if source["status"] == "pending"),
        },
        "channels": {
            "twitter": bool(config.TWITTER_BEARER_TOKEN),
            "twitter_alarm_phone": any(
                source["name"] == "X / Twitter (twikit)"
                and source["status"] in {"active", "degraded"}
                and source["reachable"] > 0
                for source in sources
            ),
            "whatsapp": whatsapp_ready,
            "whatsapp_active_connectors": whatsapp_connectors.get("active", 0),
            "telegram": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_WEBHOOK_SECRET),
            "partner_webhook": bool(config.PARTNER_WEBHOOK_SECRET),
        },
        "collector": {
            "mode": "continuous",
            "browser_independent": True,
            "persistence": "database",
            "supervisor": "systemd",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/platforms")
async def live_platforms():
    """Public projection of Mediterranean oil/gas platform positions
    (static public data — EMODnet/OGA/OSM). Mirrors /api/v1/zones/platforms,
    which is already unauthenticated, under the public Live namespace."""
    from core.api.routes.zones import get_platforms

    return await get_platforms()


@router.websocket("/stream")
async def live_stream(websocket: WebSocket, mode: str = "humanitarian"):
    """Public WebSocket snapshot stream; the browser falls back to REST polling."""
    selected_mode = mode if mode in {"humanitarian", "security", "all"} else "humanitarian"
    await websocket.accept()
    previous_digest = ""
    try:
        while True:
            snapshot = public_signal_collection(limit=500, days=30, mode=selected_mode)
            payload = json.dumps(
                {
                    "type": "snapshot",
                    "features": snapshot["features"],
                    "meta": snapshot["meta"],
                },
                separators=(",", ":"),
            )
            digest = hashlib.blake2s(payload.encode(), digest_size=8).hexdigest()
            if digest != previous_digest:
                await websocket.send_text(payload)
                previous_digest = digest
            else:
                await websocket.send_text('{"type":"ping"}')
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
