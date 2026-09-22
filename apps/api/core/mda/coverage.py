# SPDX-License-Identifier: AGPL-3.0-or-later
"""AIS reception-quality baseline (docs/fixes.md M4.2).

Whether a reporting gap is a genuine anomaly or an artefact of patchy AIS
reception can't be judged from the gap alone -- the same silence means
something different in a dense, well-covered shipping lane than it does
150nm offshore with one satellite pass an hour. This module computes that
context from the platform's own reception history (core.vessels.track_store),
not from a fixed global threshold.

Wired into core.mda.watch.scan_gaps() (docs/fixes.md M14.1) as the
reception-quality context behind each gap's core.mda.gap_reason
classification, replacing scan_gaps()'s former hard vessel-class
exclusions.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

_NM_PER_DEGREE_LAT = 60.0405  # ~ nautical miles per degree of latitude

# Local receiver/message density buckets -- how many distinct nearby
# vessels reported at all in the comparison window. Deliberately coarse
# (three buckets, not a raw count) since the boundary is a judgement call,
# not a measured constant.
_CONGESTION_LOW_MAX = 2
_CONGESTION_MEDIUM_MAX = 14


@dataclass(frozen=True)
class CoverageBaseline:
    mmsi: str
    at: datetime
    source_health: str  # "healthy" | "unknown" (see docstring below)
    expected_reporting_interval_s: Optional[float]
    local_receiver_density: int
    neighbour_message_ratio: Optional[float]
    coast_distance_km: Optional[float]
    congestion: str  # "low" | "medium" | "high" | "unknown"
    jamming_context: Optional[float]
    preceding_track_density: int


@dataclass(frozen=True)
class TrackCoverageContinuity:
    """Observed AIS traffic along a missing vessel's projected corridor.

    This is coverage evidence only. It never corroborates vessel behaviour and
    never creates a second evidence lineage. It answers a narrower question:
    while this vessel was silent, did SeaCommons continue receiving OTHER AIS
    traffic close to where the vessel could plausibly have travelled?
    """

    method_version: str
    checkpoint_count: int
    covered_checkpoints: int
    covered_fraction: float
    min_nearby_vessels: int
    median_nearby_vessels: float
    radius_nm: float
    time_window_min: float
    observed_sources: tuple[str, ...]

    @property
    def continuous(self) -> bool:
        return (
            self.checkpoint_count >= 2
            and self.covered_checkpoints >= 2
            and self.covered_fraction >= 0.6
            and self.median_nearby_vessels >= 3
        )

    def as_metadata(self) -> dict[str, object]:
        return {
            "method_version": self.method_version,
            "checkpoint_count": self.checkpoint_count,
            "covered_checkpoints": self.covered_checkpoints,
            "covered_fraction": self.covered_fraction,
            "min_nearby_vessels": self.min_nearby_vessels,
            "median_nearby_vessels": self.median_nearby_vessels,
            "radius_nm": self.radius_nm,
            "time_window_min": self.time_window_min,
            "observed_sources": list(self.observed_sources),
            "continuous": self.continuous,
            "coverage_role": "same_lineage_track_corridor_witness",
        }


def _bbox_for_radius(lat: float, lon: float, radius_nm: float) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) -- a simple equirectangular box,
    not a precise circle. Good enough for a comparison-window query; the
    same approximation core.intel.landmask's radial search already makes."""
    d_lat = radius_nm / _NM_PER_DEGREE_LAT
    lon_scale = max(0.2, math.cos(math.radians(lat)))
    d_lon = radius_nm / (_NM_PER_DEGREE_LAT * lon_scale)
    return (lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)


def _distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 3440.065 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def compute_track_corridor_coverage(
    track_store,
    *,
    mmsi: str,
    last_lat: float,
    last_lon: float,
    gap_start: datetime,
    now: datetime,
    course_deg: Optional[float],
    speed_kn: Optional[float],
    radius_nm: float = 30.0,
    time_window_min: float = 50.0,
    min_nearby_vessels: int = 3,
    max_gap_hours: float = 12.0,
) -> TrackCoverageContinuity:
    """Measure same-lineage coverage along a projected missing-vessel corridor.

    The target is projected at several times through the gap using its last
    reliable course/speed. Other-vessel AIS around those time/space checkpoints
    is evidence that our receiver/provider mesh remained alive where the target
    plausibly travelled. This is intentionally a coverage test, not behaviour
    corroboration.
    """
    empty = TrackCoverageContinuity(
        method_version="track-corridor-coverage/v1",
        checkpoint_count=0,
        covered_checkpoints=0,
        covered_fraction=0.0,
        min_nearby_vessels=0,
        median_nearby_vessels=0.0,
        radius_nm=radius_nm,
        time_window_min=time_window_min,
        observed_sources=(),
    )
    if course_deg is None or speed_kn is None or float(speed_kn) < 2.0:
        return empty
    if gap_start.tzinfo is None:
        gap_start = gap_start.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elapsed_h = min(max_gap_hours, max(0.0, (now - gap_start).total_seconds() / 3600.0))
    if elapsed_h < 1.0:
        return empty

    # 2-5 checkpoints. A 4h gap gets 2, a 7h gap 4, a 10-12h gap 5.
    checkpoint_count = max(2, min(5, int(math.ceil(elapsed_h / 2.0))))
    offsets_h = [
        elapsed_h * (index + 1) / checkpoint_count
        for index in range(checkpoint_count)
    ]
    from core.mda.sar_association import propagate_ais_state

    checkpoints: list[tuple[datetime, float, float]] = []
    for offset_h in offsets_h:
        at = gap_start + timedelta(hours=offset_h)
        projected = propagate_ais_state(
            last_lat=last_lat,
            last_lon=last_lon,
            last_observed_at=gap_start,
            target_time=at,
            course_deg=float(course_deg),
            speed_kn=float(speed_kn),
        )
        checkpoints.append((at, projected.lat, projected.lon))

    # One bounded DB read for the whole corridor, then evaluate checkpoints in
    # memory. This avoids N database queries per gap while preserving temporal
    # matching at every projected point.
    lats = [last_lat, *(point[1] for point in checkpoints)]
    lons = [last_lon, *(point[2] for point in checkpoints)]
    pad_lat = radius_nm / _NM_PER_DEGREE_LAT
    lon_scale = max(0.2, min(math.cos(math.radians(lat)) for lat in lats))
    pad_lon = radius_nm / (_NM_PER_DEGREE_LAT * lon_scale)
    bbox = (
        min(lons) - pad_lon,
        min(lats) - pad_lat,
        max(lons) + pad_lon,
        max(lats) + pad_lat,
    )
    rows = track_store.positions_between(
        gap_start,
        min(now, gap_start + timedelta(hours=max_gap_hours)),
        bbox=bbox,
        limit=100_000,
    )

    parsed_rows: list[tuple[str, datetime, float, float, str]] = []
    for row in rows:
        row_mmsi = str(row.get("mmsi") or "")
        if not row_mmsi or row_mmsi == mmsi:
            continue
        raw_ts = row.get("ts")
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        parsed_rows.append((row_mmsi, ts, lat, lon, str(row.get("source") or "")))

    counts: list[int] = []
    sources: set[str] = set()
    time_window_s = time_window_min * 60.0
    for checkpoint_at, checkpoint_lat, checkpoint_lon in checkpoints:
        nearby: set[str] = set()
        for row_mmsi, row_at, row_lat, row_lon, source in parsed_rows:
            if abs((row_at - checkpoint_at).total_seconds()) > time_window_s:
                continue
            if _distance_nm(checkpoint_lat, checkpoint_lon, row_lat, row_lon) > radius_nm:
                continue
            nearby.add(row_mmsi)
            if source:
                sources.add(source)
        counts.append(len(nearby))

    covered = sum(count >= min_nearby_vessels for count in counts)
    return TrackCoverageContinuity(
        method_version="track-corridor-coverage/v1",
        checkpoint_count=len(counts),
        covered_checkpoints=covered,
        covered_fraction=round(covered / len(counts), 3) if counts else 0.0,
        min_nearby_vessels=min(counts) if counts else 0,
        median_nearby_vessels=round(float(statistics.median(counts)), 1) if counts else 0.0,
        radius_nm=radius_nm,
        time_window_min=time_window_min,
        observed_sources=tuple(sorted(sources)),
    )


def compute_coverage_baseline(
    mmsi: str,
    lat: float,
    lon: float,
    *,
    at: Optional[datetime] = None,
    window_min: float = 60.0,
    radius_nm: float = 25.0,
) -> CoverageBaseline:
    """Best-effort, never raises -- every field degrades to its honest
    "don't know" value (None/"unknown") rather than fabricating one when a
    dependency (track_store, jamming, landmask) is unavailable.
    """
    at = at or datetime.now(timezone.utc)
    window_start = at - timedelta(minutes=window_min)

    preceding_track_density = 0
    local_receiver_density = 0
    neighbour_message_ratio: Optional[float] = None
    try:
        from core.vessels.track_store import track_store

        own_rows = track_store.track(mmsi, since=window_start, until=at)
        preceding_track_density = len(own_rows)

        bbox = _bbox_for_radius(lat, lon, radius_nm)
        nearby_rows = track_store.positions_between(window_start, at, bbox=bbox)
        per_mmsi_counts: dict[str, int] = {}
        for row in nearby_rows:
            row_mmsi = row.get("mmsi")
            if row_mmsi:
                per_mmsi_counts[row_mmsi] = per_mmsi_counts.get(row_mmsi, 0) + 1
        neighbour_counts = [count for m, count in per_mmsi_counts.items() if m != mmsi]
        local_receiver_density = len(neighbour_counts)
        if neighbour_counts:
            median = statistics.median(neighbour_counts)
            if median > 0:
                neighbour_message_ratio = round(preceding_track_density / median, 3)
    except Exception:
        pass

    if local_receiver_density == 0:
        congestion = "unknown"
    elif local_receiver_density <= _CONGESTION_LOW_MAX:
        congestion = "low"
    elif local_receiver_density <= _CONGESTION_MEDIUM_MAX:
        congestion = "medium"
    else:
        congestion = "high"

    jamming_context: Optional[float] = None
    try:
        from core.mda.jamming import jamming

        jamming_context = jamming.in_jamming_zone(lat, lon, at)
    except Exception:
        pass

    coast_distance_km: Optional[float] = None
    try:
        from core.intel.landmask import distance_to_coast_km

        coast_distance_km = distance_to_coast_km(lat, lon)
    except Exception:
        pass

    expected_reporting_interval_s: Optional[float] = None
    try:
        from core.config import config

        expected_reporting_interval_s = float(getattr(config, "VESSEL_TRACK_MIN_INTERVAL_S", 60))
    except Exception:
        pass

    # v0: "healthy" when there is any corroborating nearby traffic at all in
    # the window, "unknown" otherwise. "degraded" (a feed-wide, AOI-level
    # reception drop distinguishable from one vessel's own silence) needs a
    # historical per-AOI reporting-rate baseline this module doesn't build
    # yet -- reserved for a follow-up rather than guessed at here.
    source_health = "healthy" if local_receiver_density > 0 else "unknown"

    return CoverageBaseline(
        mmsi=mmsi,
        at=at,
        source_health=source_health,
        expected_reporting_interval_s=expected_reporting_interval_s,
        local_receiver_density=local_receiver_density,
        neighbour_message_ratio=neighbour_message_ratio,
        coast_distance_km=coast_distance_km,
        congestion=congestion,
        jamming_context=jamming_context,
        preceding_track_density=preceding_track_density,
    )
