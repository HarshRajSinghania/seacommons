# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-episode NGO response analysis.

For a live distress episode, cross-check which known SAR NGO / coastguard
vessels (see ngo_registry.py) are present in the live AIS registry and whether
they are heading toward the episode. The analysis is computed on demand and is
stateless — it reads:

  * the live vessel registry snapshot (core/vessels/registry.py),
  * recent motion-pattern observations already emitted by the AIS spike
    detector (core/intel/ais_spike_detector.py) and stored in the intel store,
  * other public signals near the episode (cross-check / corroboration).

Exposed publicly through GET /api/v1/live/signals/{event_id}/response. The
module deliberately returns only AIS/NGO public telemetry — never raw source
messages or identifiers.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from core.intel.ngo_registry import get_ngo_info, is_ngo

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
MAX_RADIUS_NM = 250.0        # maximum analysis range around the episode
APPROACHING_WINDOW_DEG = 45.0  # course within this window of the bearing = heading toward
SPEED_SPIKE_KN = 18.0        # rescue sprint: above this, flag a speed spike
RELATED_SIGNAL_RADIUS_NM = 50.0  # cross-check radius for other live signals
SPIKE_LOOKBACK_HOURS = 6     # how far back to pull detector motion flags
_MIN_ETA_SPEED_KN = 1.0      # never divide by zero when computing ETA
ON_SCENE_SPEED_MAX_KN = 5.0  # slow manoeuvring threshold near a distress/drift envelope

# spike_type (from ais_spike_detector) → motion flag label shown in the UI
_SPIKE_FLAGS = {
    "ngo_search_pattern": "search_pattern",
    "sudden_stop": "sudden_stop",
    "vessel_loiter": "loitering",
    "rescue_cluster": "rescue_cluster",
}


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2 (degrees true)."""
    la1, lo1, la2, lo2 = map(math.radians, [lat1, lon1, lat2, lon2])
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = (math.cos(la1) * math.sin(la2)
         - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _bearing_delta(b1: float, b2: float) -> float:
    """Smallest angular difference between two bearings (0–180°)."""
    d = abs(b1 - b2) % 360
    return d if d <= 180 else 360 - d


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _iter_lonlat(value: Any):
    """Yield [lon, lat] coordinate pairs from a GeoJSON-ish object."""
    if isinstance(value, dict):
        if value.get("type") == "Feature":
            yield from _iter_lonlat(value.get("geometry"))
            return
        if "coordinates" in value:
            yield from _iter_lonlat(value.get("coordinates"))
            return
        for item in value.values():
            yield from _iter_lonlat(item)
        return
    if not isinstance(value, (list, tuple)):
        return
    if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
        yield float(value[0]), float(value[1])
        return
    for item in value:
        yield from _iter_lonlat(item)


def _current_drift_context(incident_id: str) -> dict[str, Any] | None:
    """Load the canonical current drift owned by one HumanitarianIncident."""
    try:
        from core.db.models import DriftResultDB, HumanitarianIncidentDB
        from core.db.session import session_scope
        with session_scope() as db:
            incident = db.get(HumanitarianIncidentDB, incident_id)
            if incident is None or not incident.current_drift_id:
                return None
            drift = db.get(DriftResultDB, incident.current_drift_id)
            if drift is None or drift.status != "completed":
                return None
            return {
                "drift_id": drift.drift_id,
                "trajectory": drift.trajectory,
                "cone_24h": drift.cone_24h,
                "impact_point": drift.impact_point,
            }
    except Exception:
        logger.debug("ngo_response: current drift unavailable for %s", incident_id, exc_info=True)
        return None


def _nearest_drift_target(
    lat: float, lon: float, drift_context: dict[str, Any] | None,
) -> tuple[float | None, tuple[float, float] | None]:
    if not drift_context:
        return None, None
    best_distance: float | None = None
    best_point: tuple[float, float] | None = None
    for key in ("trajectory", "cone_24h", "impact_point"):
        for p_lon, p_lat in _iter_lonlat(drift_context.get(key)):
            distance = _haversine_nm(lat, lon, p_lat, p_lon)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_point = (p_lat, p_lon)
    return best_distance, best_point


def _sar_zone_context(lat: float, lon: float) -> list[dict[str, Any]]:
    try:
        from core.zones.classifier import classify_point
        return [
            {"id": str(zone.get("id") or ""), "name": str(zone.get("name") or "")}
            for zone in classify_point(lat, lon)
        ]
    except Exception:
        return []


def _recent_spike_flags(mmsi: str, now: datetime) -> list[str]:
    """Reuse the spike detector's stored observations for this MMSI."""
    try:
        from core.intel.store import intel_store
    except Exception:
        return []
    flags: list[str] = []
    for ev in intel_store.events(type_filter="ais_spike", limit=500, max_age_days=1):
        if str(ev.linked_mmsi or "") != str(mmsi):
            continue
        seen = _parse_iso(ev.timestamp_utc)
        if seen is None or (now - seen).total_seconds() / 3600 > SPIKE_LOOKBACK_HOURS:
            continue
        spike_type = str(ev.metadata.get("spike_type") or "")
        flag = _SPIKE_FLAGS.get(spike_type)
        if flag and flag not in flags:
            flags.append(flag)
    return flags


def _vessel_row(props: dict[str, Any], coords: list[Any],
                lat: float, lon: float, now: datetime,
                drift_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """One NGO vessel's cross-check against the episode."""
    mmsi = str(props.get("mmsi") or "")
    name = str(props.get("ship_name") or mmsi or "Unknown")
    speed_kn = float(props.get("speed") if props.get("speed") is not None else props.get("sog") or 0)
    course_deg = float(props.get("course") if props.get("course") is not None else props.get("cog") or 0)
    v_lat = float(coords[1])
    v_lon = float(coords[0])

    distance_nm = _haversine_nm(lat, lon, v_lat, v_lon)
    drift_distance_nm, drift_target = _nearest_drift_target(v_lat, v_lon, drift_context)
    operational_distance_nm = min(
        distance_nm,
        drift_distance_nm if drift_distance_nm is not None else distance_nm,
    )
    target_lat, target_lon = (lat, lon)
    target_kind = "distress_origin"
    if drift_target is not None and drift_distance_nm is not None and drift_distance_nm < distance_nm:
        target_lat, target_lon = drift_target
        target_kind = "current_drift"

    bearing_to_episode = _bearing_deg(v_lat, v_lon, lat, lon)
    bearing_to_operational_target = _bearing_deg(v_lat, v_lon, target_lat, target_lon)
    course_known = speed_kn > 0.1
    heading_toward = bool(
        course_known
        and _bearing_delta(course_deg, bearing_to_operational_target) <= APPROACHING_WINDOW_DEG
    )
    eta_h = (
        operational_distance_nm / max(speed_kn, _MIN_ETA_SPEED_KN)
        if speed_kn > 0.1 else None
    )

    last_seen = str(props.get("last_seen") or props.get("timestamp_utc") or "")
    seen_dt = _parse_iso(last_seen)
    fix_age_min = round(max(0.0, (now - seen_dt).total_seconds() / 60)) if seen_dt else None

    info = get_ngo_info(mmsi) or {}

    motion_flags = _recent_spike_flags(mmsi, now)
    if speed_kn >= SPEED_SPIKE_KN and "speed_spike" not in motion_flags:
        motion_flags.insert(0, "speed_spike")
    rescue_motion = any(
        flag in {"search_pattern", "rescue_cluster", "sudden_stop", "loitering"}
        for flag in motion_flags
    )

    sar_zones = _sar_zone_context(v_lat, v_lon)
    in_named_srr = any(zone.get("id") != "high_seas" for zone in sar_zones)
    in_port_or_land = False
    try:
        from core.mda.reference import reference
        in_port_or_land = bool(
            reference.in_port_or_anchorage(v_lat, v_lon) or reference.is_land(v_lat, v_lon)
        )
    except Exception:
        pass

    track_providers = sorted({str(v) for v in (props.get("sources") or []) if str(v)})
    upstream_sources = sorted({str(v) for v in (props.get("upstream_sources") or []) if str(v)})
    stations = sorted({str(v) for v in (props.get("stations") or []) if str(v)})
    try:
        from core.vessels.ais_coverage import coverage_state
        coverage = coverage_state.assess(nearby_traffic_seen=True, now=now)
        coverage_status = coverage.status
    except Exception:
        coverage_status = "coverage_unknown"

    if in_port_or_land:
        mission_state = "unrelated"
    elif coverage_status == "provider_degraded":
        mission_state = (
            "possible_response"
            if heading_toward or operational_distance_nm <= RELATED_SIGNAL_RADIUS_NM
            else ("search_candidate" if rescue_motion and in_named_srr else "unrelated")
        )
    elif operational_distance_nm <= 20 and rescue_motion:
        mission_state = "probable_rescue_activity"
    elif operational_distance_nm <= 20 and speed_kn <= ON_SCENE_SPEED_MAX_KN:
        mission_state = "on_scene"
    elif heading_toward:
        mission_state = "approaching"
    elif operational_distance_nm <= RELATED_SIGNAL_RADIUS_NM:
        mission_state = "possible_response"
    elif rescue_motion and in_named_srr:
        mission_state = "search_candidate"
    else:
        mission_state = "unrelated"

    return {
        "mmsi": mmsi,
        "name": name,
        "org": info.get("org", ""),
        "role": info.get("role", ""),
        "flag": info.get("flag", ""),
        "lat": v_lat,
        "lon": v_lon,
        "distance_nm": round(distance_nm, 2),
        "distance_to_drift_nm": round(drift_distance_nm, 2) if drift_distance_nm is not None else None,
        "operational_distance_nm": round(operational_distance_nm, 2),
        "operational_target": target_kind,
        "drift_target": (
            {"lat": round(target_lat, 5), "lon": round(target_lon, 5)}
            if target_kind == "current_drift" else None
        ),
        "sar_zones": sar_zones,
        "in_named_srr": in_named_srr,
        "in_port_or_land": in_port_or_land,
        "bearing_to_episode_deg": round(bearing_to_episode, 1),
        "bearing_to_operational_target_deg": round(bearing_to_operational_target, 1),
        "course_deg": round(course_deg, 1) if course_known else None,
        "speed_kn": round(speed_kn, 1),
        "heading_toward": heading_toward,
        "eta_h": round(eta_h, 2) if eta_h is not None else None,
        "last_seen": last_seen,
        "fix_age_min": fix_age_min,
        "track_saved": f"AIS fix recorded {fix_age_min} min ago" if fix_age_min is not None else "no AIS fix",
        "motion_flags": motion_flags,
        "track_providers": track_providers,
        "upstream_sources": upstream_sources,
        "stations": stations,
        "coverage_status": coverage_status,
        "mission_state": mission_state,
    }


def _build_geojson(episode_lat: float, episode_lon: float, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """GeoJSON for the map: episode→vessel lines + vessel points."""
    features: list[dict[str, Any]] = []
    for row in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [
                (
                    [row["drift_target"]["lon"], row["drift_target"]["lat"]]
                    if row.get("drift_target") else [episode_lon, episode_lat]
                ),
                [row["lon"], row["lat"]],
            ]},
            "properties": {
                "mmsi": row["mmsi"],
                "name": row["name"],
                "org": row["org"],
                "heading_toward": row["heading_toward"],
                "distance_nm": row["distance_nm"],
                "operational_distance_nm": row["operational_distance_nm"],
                "operational_target": row["operational_target"],
                "mission_state": row["mission_state"],
            },
        })
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": {
                "mmsi": row["mmsi"],
                "name": row["name"],
                "org": row["org"],
                "heading_toward": row["heading_toward"],
                "distance_nm": row["distance_nm"],
                "operational_distance_nm": row["operational_distance_nm"],
                "operational_target": row["operational_target"],
                "mission_state": row["mission_state"],
                "eta_h": row["eta_h"],
                "speed_kn": row["speed_kn"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


def analyze_ngo_response(
    event: Any,
    *,
    now: Optional[datetime] = None,
    registry_geojson: Optional[dict[str, Any]] = None,
    related_signals: Optional[list[Any]] = None,
    drift_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compute the NGO cross-check for a positioned episode.

    Args:
        event: IntelEvent (or duck-typed) with `.lat`/`.lon`/`.title`/`.id`.
        now: wall clock override (tests).
        registry_geojson: optional pre-fetched registry snapshot; fetched lazily
            from core.vessels.registry otherwise.
        related_signals: other events to cross-check nearby; the caller is
            expected to pre-filter these to public, positioned signals.
    """
    lat = event.lat
    lon = event.lon
    if lat is None or lon is None:
        raise ValueError("episode has no position")
    now = now or datetime.now(timezone.utc)

    if registry_geojson is None:
        try:
            from core.vessels.registry import registry
            registry_geojson = registry.get_geojson()
        except Exception as exc:
            logger.warning("ngo_response: registry unavailable: %s", exc)
            registry_geojson = {"features": []}

    rows: list[dict[str, Any]] = []
    assets_within_50nm = 0
    ngo_within_50nm = 0
    for feature in registry_geojson.get("features", []):
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) != 2:
            continue
        props = feature.get("properties") or {}
        mmsi = str(props.get("mmsi") or "")
        if not mmsi:
            continue
        v_lat = float(coords[1])
        v_lon = float(coords[0])
        dist = _haversine_nm(lat, lon, v_lat, v_lon)
        if dist <= RELATED_SIGNAL_RADIUS_NM:
            assets_within_50nm += 1
        if not is_ngo(mmsi):
            continue
        row = _vessel_row(props, coords, lat, lon, now, drift_context=drift_context)
        if float(row["operational_distance_nm"]) > MAX_RADIUS_NM:
            continue
        rows.append(row)
        if float(row["operational_distance_nm"]) <= RELATED_SIGNAL_RADIUS_NM:
            ngo_within_50nm += 1

    rows.sort(key=lambda r: (not r["heading_toward"], r["operational_distance_nm"]))

    # ── Cross-check: other live signals near this episode ─────────────────────
    related: list[dict[str, Any]] = []
    for other in (related_signals or []):
        other_id = getattr(other, "id", None)
        if not other_id or other_id == getattr(event, "id", None):
            continue
        if other.lat is None or other.lon is None:
            continue
        dist = _haversine_nm(lat, lon, other.lat, other.lon)
        if dist > RELATED_SIGNAL_RADIUS_NM:
            continue
        related.append({
            "id": other_id,
            "title": (getattr(other, "title", "") or "")[:120],
            "type": getattr(other, "type", ""),
            "distance_nm": round(dist, 2),
        })
    related.sort(key=lambda r: r["distance_nm"])

    approaching = [r for r in rows if r["heading_toward"]]
    nearest = min(rows, key=lambda r: r["distance_nm"]) if rows else None
    fastest = None
    for r in approaching:
        if r["eta_h"] is None:
            continue
        if fastest is None or r["eta_h"] < fastest["eta_h"]:
            fastest = r

    try:
        from core.intel import lifecycle
        same_source = [
            candidate
            for candidate in (related_signals or [])
            if getattr(candidate, "source", None) == getattr(event, "source", None)
        ]
        if event not in same_source:
            same_source.append(event)
        state = lifecycle.distress_lifecycle(event, now=now, same_source=same_source)
    except Exception:
        state = (
            str(event.metadata.get("incident_lifecycle") or "active")
            if getattr(event, "metadata", None)
            else "active"
        )

    return {
        "episode": {
            "id": getattr(event, "id", ""),
            "title": (getattr(event, "title", "") or "")[:200],
            "lat": lat,
            "lon": lon,
            "lifecycle": state,
        },
        "generated_at": now.isoformat(),
        "summary": {
            "approaching_ngo_vessels": len(approaching),
            "ngo_vessels_in_range": len(rows),
            "nearest_ngo": {
                "name": nearest["name"],
                "org": nearest["org"],
                "distance_nm": nearest["distance_nm"],
            } if nearest else None,
            "fastest_approach_eta_h": fastest["eta_h"] if fastest else None,
            "fastest_approach_name": fastest["name"] if fastest else None,
            "assets_within_50nm": assets_within_50nm,
            "related_signals": len(related),
            "current_drift_id": drift_context.get("drift_id") if drift_context else None,
        },
        "ngo_vessels": rows,
        "cross_check": {
            "related_signals": related,
            "total_vessels_within_50nm": assets_within_50nm,
            "ngo_vessels_within_50nm": ngo_within_50nm,
            "current_drift_id": drift_context.get("drift_id") if drift_context else None,
        },
        "geojson": _build_geojson(lat, lon, rows),
    }
