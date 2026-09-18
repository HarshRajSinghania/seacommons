# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maritime-domain-awareness / dark-vessel engine API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request

from core.security import READ_ROLES, require_roles

router = APIRouter(prefix="/api/v1/mda", tags=["mda"])

_MDA_TYPES = {"ais_anomaly", "ais_rendezvous", "vessel_identity", "dark_candidate",
              "conflict_event", "navwarning", "correlated_alert"}


@router.get("/reference")
async def reference_geometry(kinds: str = Query(default="cable,pipeline,platform,sts_zone")):
    from core.mda.reference import reference

    want = {k.strip() for k in kinds.split(",") if k.strip()}
    return reference.to_geojson(kinds=want or None)


@router.get("/jamming")
async def jamming_zones():
    from core.mda.jamming import jamming

    return jamming.to_geojson()


def collect_mda_anomalies(hours: float, kind: str) -> dict:
    """Shared by both the authenticated operator route below and the public
    live-map projection (core/api/routes/live.py) — same data either way.
    Every field here is already public-interest (official sanctions-list
    hits, AIS-signal integrity flags derived from public broadcasts), so
    there is nothing to strip for the public projection; see live.py's
    docstring for the reasoning."""
    from core.intel.store import intel_store

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    candidates = []
    covered_ids: set[str] = set()
    for e in intel_store.events(limit=600):
        if e.type not in _MDA_TYPES:
            continue
        if (e.timestamp_utc or "") < cutoff:
            continue
        candidates.append(e)
        if e.type == "correlated_alert":
            covered_ids.update(e.metadata.get("contributing") or [])

    by_id = {c.id: c for c in candidates}
    out = []
    for e in candidates:
        if kind != "all" and e.type != kind:
            continue
        if e.type != "correlated_alert" and e.id in covered_ids:
            continue
        lat, lon = e.lat, e.lon
        if e.type == "correlated_alert" and (lat is None or lon is None):
            for cid in (e.metadata.get("contributing") or []):
                src = by_id.get(cid)
                if src is not None and src.lat is not None and src.lon is not None:
                    lat, lon = src.lat, src.lon
                    break
        out.append({
            "id": e.id, "type": e.type, "severity": e.severity,
            "lat": lat, "lon": lon, "title": e.title,
            "mmsi": e.linked_mmsi or None,
            "timestamp_utc": e.timestamp_utc,
            "anomaly_type": e.metadata.get("anomaly_type"),
            "maritime_domain": e.maritime_domain(),
            "metadata": {k: v for k, v in e.metadata.items()
                         if k in ("alert_type", "confidence", "contributing_sources",
                                  "tanker", "dark", "sts_zone", "jamming_score",
                                  "infrastructure", "identity", "darkship_cue",
                                  "spoof_reason", "case_id")},
        })
    out.sort(key=lambda x: str(x["timestamp_utc"]), reverse=True)
    return {"count": len(out), "anomalies": out}


@router.get("/anomalies")
async def anomalies(hours: int = Query(default=48, ge=1, le=720),
                    kind: str = Query(default="all")):
    return collect_mda_anomalies(hours, kind)


@router.get("/vessel/{mmsi}")
async def vessel_dossier(request: Request, mmsi: str, hours: float = Query(default=72.0, ge=1, le=24 * 90)):
    require_roles(request, READ_ROLES)
    return build_vessel_dossier(mmsi, hours=hours, include_behaviour=True)


@router.get("/vessel/{mmsi}/baseline")
async def vessel_baseline(request: Request, mmsi: str):
    require_roles(request, READ_ROLES)
    from core.mda.behavioural_baseline import latest_baseline

    baseline = latest_baseline(mmsi)
    return _baseline_payload(baseline, mmsi=mmsi)


def _baseline_payload(baseline, *, mmsi: str) -> dict:
    if baseline is None:
        return {"mmsi": mmsi, "available": False}
    return {
        "mmsi": mmsi, "available": True, "baseline_id": baseline.baseline_id,
        "subject_id": baseline.subject_id, "primary_imo": baseline.primary_imo,
        "window_start": baseline.window_start.isoformat(), "window_end": baseline.window_end.isoformat(),
        "sample_count": baseline.sample_count, "history_days": baseline.history_days,
        "route_model": baseline.route_model, "speed_model": baseline.speed_model,
        "port_model": baseline.port_model, "silence_model": baseline.silence_model,
        "method_version": baseline.method_version, "evidence_fingerprint": baseline.evidence_fingerprint,
    }


def _radio_associations_for_vessel(mmsi: str, *, limit: int = 20) -> list[dict]:
    try:
        from core.db.session import session_scope
        from core.db.models import RadioAISAssociationDB

        with session_scope() as db:
            rows = (
                db.query(RadioAISAssociationDB)
                .filter(RadioAISAssociationDB.mmsi == str(mmsi))
                .order_by(RadioAISAssociationDB.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "observation_id": row.observation_id,
                    "match_status": row.match_status,
                    "confidence": float(row.confidence),
                    "distance_km": row.distance_km,
                    "ais_observed_at": row.ais_observed_at,
                    "episode_eligible": bool(row.episode_eligible),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
    except Exception:
        return []


def build_vessel_dossier(mmsi: str, *, hours: float, track_limit: int = 5000, include_behaviour: bool = False) -> dict:
    from core.mda.identity import screen
    from core.vessels.registry import registry
    from core.vessels.track_store import track_store

    v = (getattr(registry, "_cache", {}) or {}).get(mmsi, {})
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    track = track_store.track(mmsi, since=since, limit=5000)
    if track_limit >= 2 and len(track) > track_limit:
        scale = (len(track) - 1) / (track_limit - 1)
        track = [track[round(index * scale)] for index in range(track_limit)]
    dossier = {
        "mmsi": mmsi,
        "static": {"name": v.get("ship_name"), "imo": v.get("imo"),
                   "ship_type": v.get("ship_type"), "flag": v.get("flag"),
                   "destination": v.get("destination"), "last_seen": v.get("last_seen")},
        "identity": screen(mmsi=mmsi, imo=v.get("imo"), name=v.get("ship_name") or "",
                           flag=v.get("flag") or ""),
        "track": {
            "type": "FeatureCollection",
            "features": ([{
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[p["lon"], p["lat"]] for p in track]},
                "properties": {"points": len(track)},
            }] if len(track) >= 2 else []),
        },
        "track_points": track,
        "recent_port_calls": _derive_recent_port_calls(track),
        "radio_associations": _radio_associations_for_vessel(mmsi),
    }
    if include_behaviour:
        from core.mda.behaviour_assessment import assess_behaviour
        from core.mda.behavioural_baseline import latest_baseline
        from core.mda.vessel_context import build_vessel_context

        baseline = latest_baseline(mmsi)
        assessment = assess_behaviour(track, baseline)
        dossier["context"] = build_vessel_context(mmsi, hours=hours)
        dossier["behaviour_assessment"] = {
            "status": assessment.status, "baseline_id": assessment.baseline_id,
            "method_version": assessment.method_version, "reason_codes": list(assessment.reason_codes),
            "dimensions": assessment.dimensions, "caveats": list(assessment.caveats),
            "evaluated_at": assessment.evaluated_at.isoformat(),
        }
    return dossier


def _derive_recent_port_calls(track: list[dict], *, limit: int = 8) -> list[dict]:
    """Conservatively infer port stays from AIS fixes inside known approaches.

    These are explicitly model-derived calls, not official port-authority
    records. A group is retained only if it has at least two AIS fixes, at
    least one fix is slow/anchored/moored, and the observed dwell is at least
    20 minutes. A single fix or fast transit is never a port call.
    """
    from core.mda.reference import reference

    groups: list[dict] = []
    current: dict | None = None
    for point in track:
        try:
            lat, lon = float(point["lat"]), float(point["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        port = reference.in_port_or_anchorage(lat, lon)
        if not port:
            if current is not None:
                current["departed_at"] = point.get("ts")
                if current.pop("_qualified", False):
                    groups.append(current)
                current = None
            continue
        try:
            slow = point.get("sog") is not None and float(point["sog"]) <= 2.0
        except (TypeError, ValueError):
            slow = False
        try:
            nav_status = point.get("nav_status")
            stationary_status = nav_status is not None and int(nav_status) in {1, 5}
        except (TypeError, ValueError):
            stationary_status = False
        if current is None or current["port"] != port:
            if current is not None and current.pop("_qualified", False):
                groups.append(current)
            current = {
                "port": port,
                "arrived_at": point.get("ts"),
                "departed_at": None,
                "last_seen_at": point.get("ts"),
                "ais_fixes": 1,
                "evidence_level": "derived",
                "method": "ais_port_approach",
                "_qualified": slow or stationary_status,
            }
        else:
            current["last_seen_at"] = point.get("ts")
            current["ais_fixes"] += 1
            current["_qualified"] = current["_qualified"] or slow or stationary_status
    if current is not None and current.pop("_qualified", False):
        groups.append(current)

    from core.intel.lifecycle import parse_utc

    qualified: list[dict] = []
    for group in groups:
        if int(group.get("ais_fixes") or 0) < 2:
            continue
        start = parse_utc(str(group.get("arrived_at") or ""))
        end = parse_utc(str(group.get("last_seen_at") or ""))
        if start is None or end is None:
            continue
        dwell_s = max(0.0, (end - start).total_seconds())
        if dwell_s < 20 * 60:
            continue
        group["dwell_minutes"] = round(dwell_s / 60.0, 1)
        qualified.append(group)
    return list(reversed(qualified[-limit:]))


@router.get("/chokepoints")
async def chokepoint_transits(hours: int = Query(default=24, ge=1, le=168)):
    from core.mda.chokepoints import chokepoint_transits as _ct

    return _ct(hours=hours)


@router.get("/status")
async def mda_status():
    from core.db.models import SanctionedVesselDB
    from core.db.session import session_scope
    from core.mda.jamming import jamming
    from core.vessels.track_store import track_store

    try:
        with session_scope() as db:
            from sqlalchemy import func
            sanctioned = db.query(func.count(SanctionedVesselDB.id)).scalar() or 0
    except Exception:
        sanctioned = 0
    return {
        "track_store": track_store.stats(),
        "jamming_as_of": jamming.as_of(),
        "sanctioned_vessels": sanctioned,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
