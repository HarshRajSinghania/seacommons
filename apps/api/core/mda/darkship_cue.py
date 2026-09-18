# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift-cued dark-ship search — the flagship cross-sensor product.

When a vessel goes dark (AIS gap) or spoofs, we know its last good position,
course and speed. From that we build a *reachable-area* polygon that grows with
time (kinematics widened by an ocean-current + wind allowance), then:

  1. check whether Global Fishing Watch has already published a Sentinel-1 SAR
     detection inside it (baseline coverage, free);
  2. query the Copernicus Sentinel-1 STAC for scenes that cover the area
     (recent acquisitions + an estimated next revisit);
  3. optionally (Phase 2 stretch) pull a cued GRD scene and run a light CFAR to
     find the unmatched bright target = the dark ship.

`build()` returns a dict attached to the alert / case. Everything is
best-effort — offline it still returns the ellipse and the revisit estimate.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Sentinel-1 (A+C) give roughly a 3-day effective revisit at Mediterranean
# latitudes counting ascending + descending passes.
_S1_REVISIT_HOURS = 72
_CURRENT_ALLOWANCE_KN = 1.5   # unknown set/drift while dark


def _dest_point(lat: float, lon: float, bearing_deg: float, dist_km: float) -> tuple[float, float]:
    r = 6371.0088
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d = dist_km / r
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180


def reachable_polygon(lat: float, lon: float, course_deg: Optional[float],
                      speed_kn: Optional[float], hours: float) -> dict[str, Any]:
    """A teardrop reachable area: full circle of (max_speed*t) radius, biased
    forward along the last course. `course_deg`/`speed_kn` may be None."""
    hours = max(0.25, min(hours, 48.0))
    max_kn = max((speed_kn or 6.0) * 1.6, 12.0) + _CURRENT_ALLOWANCE_KN
    radius_km = max_kn * 1.852 * hours
    back_km = min(radius_km, (speed_kn or 3.0) * 1.852 * hours + 5.0)
    ring: list[list[float]] = []
    for deg in range(0, 360, 15):
        # forward semicircle uses the full radius, aft uses the smaller one
        if course_deg is not None:
            rel = abs(((deg - course_deg + 180) % 360) - 180)
            rr = radius_km if rel <= 90 else back_km + (radius_km - back_km) * (1 - (rel - 90) / 90)
        else:
            rr = radius_km
        p_lat, p_lon = _dest_point(lat, lon, deg, rr)
        ring.append([round(p_lon, 4), round(p_lat, 4)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring], "_radius_km": round(radius_km, 1)}


def _bbox_of(poly: dict[str, Any]) -> tuple[float, float, float, float]:
    xs = [c[0] for c in poly["coordinates"][0]]
    ys = [c[1] for c in poly["coordinates"][0]]
    return min(xs), min(ys), max(xs), max(ys)


def _recent_s1_scenes(
    bbox: tuple[float, float, float, float],
    since: datetime,
    *,
    until: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    try:
        import httpx

        end = until or datetime.now(timezone.utc)
        body = {
            "collections": ["SENTINEL-1"],
            "bbox": list(bbox),
            "datetime": f"{since.isoformat()}/{end.isoformat()}",
            "limit": 20,
        }
        # docs/fixes.md M0.5: the old catalogue.dataspace.copernicus.eu/stac
        # endpoint is stale; use the current Copernicus Data Space STAC API.
        r = httpx.post("https://stac.dataspace.copernicus.eu/v1/search",
                       json=body, timeout=45)
        r.raise_for_status()
        feats = r.json().get("features", [])
        out = []
        for f in feats:
            props = f.get("properties", {})
            if "GRD" not in str(props.get("productType", f.get("id", ""))):
                continue
            out.append({
                "id": f.get("id"),
                "acquired": props.get("datetime") or props.get("startTimeFromAscendingNode"),
                "orbit_direction": props.get("orbitDirection"),
                "footprint": f.get("geometry"),
            })
        return out
    except Exception as exc:
        logger.info("darkship_cue: S1 STAC query skipped: %s", exc)
        return []


def _bbox_feature_collection(
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [min_lon, min_lat],
                    [max_lon, min_lat],
                    [max_lon, max_lat],
                    [min_lon, max_lat],
                    [min_lon, min_lat],
                ]],
            },
        }],
    }


def _gfw_sar_in_area(
    bbox: tuple[float, float, float, float],
    since: datetime,
    *,
    until: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Return GFW SAR presence cells for a bounded place/time window.

    GFW's legacy dataset-specific detections URL was removed. SAR presence is
    exposed through the v3 4Wings report API. The documented matched filter has
    intermittently failed server-side because the current dataset stores that
    field as a string, so this fetches the bounded report and classifies
    matched/unmatched locally from vessel identity fields instead.
    """
    from core.config import config

    token = getattr(config, "GFW_API_TOKEN", "") or ""
    if not token:
        return []

    end = until or datetime.now(timezone.utc)
    if end < since:
        return []
    try:
        import httpx

        response = httpx.post(
            "https://gateway.api.globalfishingwatch.org/v3/4wings/report",
            params={
                "spatial-resolution": "HIGH",
                "temporal-resolution": "HOURLY",
                "datasets[0]": "public-global-sar-presence:latest",
                "date-range": f"{since.date().isoformat()},{end.date().isoformat()}",
                "format": "JSON",
                "spatial-aggregation": "false",
            },
            json={"geojson": _bbox_feature_collection(bbox)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        rows: list[dict[str, Any]] = []
        for entry in payload.get("entries") or ():
            if not isinstance(entry, dict):
                continue
            for dataset_key, detections in entry.items():
                if not str(dataset_key).startswith("public-global-sar-presence:"):
                    continue
                if not isinstance(detections, list):
                    continue
                for detection in detections:
                    if not isinstance(detection, dict):
                        continue
                    lat, lon = detection.get("lat"), detection.get("lon")
                    if lat is None or lon is None:
                        continue
                    timestamp = (
                        detection.get("entryTimestamp")
                        or detection.get("date")
                        or detection.get("exitTimestamp")
                    )
                    if not timestamp:
                        continue
                    try:
                        observed_at = datetime.fromisoformat(
                            str(timestamp).replace("Z", "+00:00")
                        )
                        if observed_at.tzinfo is None:
                            observed_at = observed_at.replace(tzinfo=timezone.utc)
                        observed_at = observed_at.astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        continue
                    if not (since <= observed_at <= end):
                        continue
                    vessel_id = str(detection.get("vesselId") or "").strip()
                    mmsi = str(detection.get("mmsi") or "").strip()
                    matched = bool(vessel_id or mmsi)
                    rows.append({
                        "lat": float(lat),
                        "lon": float(lon),
                        "matched": matched,
                        "timestamp": timestamp,
                        "mmsi": mmsi or None,
                        "vessel_id": vessel_id or None,
                        "dataset": str(dataset_key),
                    })
        return rows
    except Exception as exc:
        logger.info("darkship_cue: GFW SAR query skipped: %s", exc)
        return []


def _point_in_polygon(lon: float, lat: float, polygon: dict[str, Any]) -> bool:
    """Small dependency-free point-in-polygon check for the reachable ring."""
    try:
        ring = polygon["coordinates"][0]
    except (KeyError, IndexError, TypeError):
        return False
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = float(point[0]), float(point[1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        intersects = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def build(*, lat: float, lon: float, course_deg: Optional[float] = None,
          speed_kn: Optional[float] = None, gap_start: Optional[datetime] = None,
          max_search_hours: float = 12.0, include_s1: bool = True) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = gap_start or now
    elapsed_hours = max(0.25, (now - start).total_seconds() / 3600.0)
    search_hours = min(elapsed_hours, max(0.25, float(max_search_hours)))
    search_until = min(now, start + timedelta(hours=search_hours))
    poly = reachable_polygon(lat, lon, course_deg, speed_kn, search_hours)
    bbox = _bbox_of(poly)
    since = start - timedelta(hours=2)

    s1 = (
        _recent_s1_scenes(bbox, since, until=search_until)
        if include_s1 else []
    )
    gfw = [
        detection
        for detection in _gfw_sar_in_area(bbox, since, until=search_until)
        if _point_in_polygon(
            float(detection["lon"]), float(detection["lat"]), poly
        )
    ]
    unmatched = [d for d in gfw if d.get("matched") is False]

    next_pass_h = _S1_REVISIT_HOURS
    if s1:
        try:
            last = max(datetime.fromisoformat(str(s["acquired"]).replace("Z", "+00:00"))
                       for s in s1 if s.get("acquired"))
            elapsed = (now - last).total_seconds() / 3600
            next_pass_h = max(0.0, _S1_REVISIT_HOURS - (elapsed % _S1_REVISIT_HOURS))
        except Exception:
            pass

    return {
        "generated_at": now.isoformat(),
        "last_known": {"lat": lat, "lon": lon, "course_deg": course_deg, "speed_kn": speed_kn},
        "dark_for_hours": round(elapsed_hours, 1),
        "search_window_hours": round(search_hours, 1),
        "search_until": search_until.isoformat(),
        "search_area": poly,
        "search_bbox": list(bbox),
        "radius_km": poly["_radius_km"],
        "sentinel1_scenes": s1,
        "gfw_sar_detections": gfw,
        "gfw_unmatched_in_area": unmatched,
        "next_s1_pass_estimate_hours": round(next_pass_h, 1),
        # docs/fixes.md M0.5: an unmatched SAR detection inside the reachable
        # area is a candidate, never "likely/confirmed" -- this stage has no
        # acquisition-time-propagated AIS position, no distance/uncertainty
        # score against it, and no check for another vessel that happens to
        # be transiting the same area (docs/fixes.md M7.2 is the stronger
        # association stage that would justify "likely"). association_status
        # mirrors that future stage's vocabulary so a caller can branch on it
        # without parsing this English sentence.
        "association_status": "unmatched_candidate" if unmatched else "no_detection",
        "recommendation": (
            f"{len(unmatched)} unmatched SAR detection(s) inside the reachable search area "
            f"({poly['_radius_km']} km radius, {round(search_hours, 1)}h search window) "
            "-- a candidate for the dark vessel, not a confirmed match. No acquisition-time "
            "AIS propagation or distance/uncertainty scoring has been run against it yet; "
            "the detection could belong to another vessel transiting the same area."
            if unmatched else
            f"No SAR detection yet. Next Sentinel-1 pass over the area in ~{round(next_pass_h)} h."
        ),
    }
