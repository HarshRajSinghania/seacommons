# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AIS Spike Detector — identifies rescue operations and distress events
by analysing anomalous patterns in the live vessel registry.

Detection rules (all generate IntelEvents with type="ais_spike"):

  1. SUDDEN_STOP
     Vessel with last_speed > SPEED_THRESHOLD_KN drops to < STOP_THRESHOLD_KN
     while in open water (not inside a port bbox) and not reporting an
     anchored/moored/aground AIS nav status. A single-sample transition emits
     `possible_sudden_stop` (a cue); it is promoted to `sudden_stop` only once
     the stop has held AIS_SUDDEN_STOP_MIN_SAMPLES scans / _PERSISTENCE_S
     seconds without the vessel moving off its stop point.
     Confidence boosted if position is within a known SAR hotspot.

  2. RESCUE_CLUSTER
     Two or more vessels converge within CLUSTER_RADIUS_NM of each other
     within the last CLUSTER_AGE_S seconds.  Confidence is 'high' if at
     least one vessel is a known NGO/coastguard.

  3. NGO_SEARCH_PATTERN
     A known NGO vessel shows erratic course changes (bearing delta > 60°
     between consecutive readings) at low speed (1–5 kn) — indicative of
     active search pattern.

  4. VESSEL_LOITER
     A vessel stops for > LOITER_MIN_S in open water near a known hotspot.
     Suppressed when the vessel's AIS nav status is anchored / moored /
     aground; when nav status is absent the loiter still emits but is marked
     `nav_status_known: false`.

Runs in a background daemon thread; polls registry every POLL_INTERVAL_S.
State (previous speeds/courses/positions) kept in-memory.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from core.intel.ngo_registry import is_ngo, get_ngo_info, ngo_mmsi_set
from core.intel.store import IntelEvent, intel_store

logger = logging.getLogger(__name__)

# ── Tunable parameters ────────────────────────────────────────────────────────
POLL_INTERVAL_S    = 5 * 60     # scan every 5 minutes
SPEED_THRESHOLD_KN = 3.0        # vessel was "underway" above this speed
STOP_THRESHOLD_KN  = 0.4        # now considered stopped
CLUSTER_RADIUS_NM  = 3.0        # vessels this close = possible cluster
CLUSTER_AGE_S      = 30 * 60    # both readings must be this fresh
LOITER_MIN_S       = 45 * 60    # stopped > 45 min in open water = loiter
SEARCH_BEARING_DELTA = 60.0     # NGO course change > this = search pattern
SEARCH_SPEED_MAX_KN  = 5.0

# ── Circular search-pattern fit (track history, not just 2 samples) ──────────
# A genuine expanding-square / sector search box leaves the same geometric
# signature the AIS spoof detector flags (core.mda.watch._circle_fit): a
# tight, near-closed ring. Fitting the recent track catches a real search
# pattern the single-sample bearing-delta check above misses between polls;
# on a known NGO/coastguard hull the same signature reads as "searching",
# not "spoofed".
SEARCH_TRACK_WINDOW_MIN = 90
SEARCH_TRACK_MIN_FIXES  = 5
SEARCH_CIRCLE_MIN_M     = 150.0
SEARCH_CIRCLE_MAX_M     = 4000.0
SEARCH_CIRCLE_MAX_RESID = 0.20   # looser than the spoof detector's 0.12 --
                                  # a worked search box is not a perfect ring

# ── Cross-check against active humanitarian distress cases ───────────────────
# Context only -- never proof of an actual response. See
# core.live.vessel_episodes.add_nearby_humanitarian_context for the same
# non-causal "nearby, not confirmed" pattern applied to the public feed.
RESPONSE_CROSSCHECK_RADIUS_NM = 30.0
RESPONSE_CROSSCHECK_MAX_AGE_DAYS = 2
_DISTRESS_TYPES = frozenset({"distress", "iom_incident"})
_RESOLVED_LIFECYCLES = frozenset({"resolved", "archived"})
_RESCUE_RELEVANT_SPIKES = frozenset(
    {"sudden_stop", "ngo_search_pattern", "rescue_cluster", "possible_rescue_cluster"}
)
# A cluster's mutual distance must fall by at least this much between scans to
# count as convergence rather than two vessels that merely happen to be close.
CLUSTER_CONVERGENCE_NM = 0.1

# AIS navigational status codes that themselves explain a stationary vessel --
# an anchored / moored / aground vessel is not a "sudden stop" casualty and is
# not "loitering". Codes: 1 at anchor, 5 moored, 6 aground.
_ANCHORED_NAV_STATUS = frozenset({1, 5})
_AGROUND_NAV_STATUS = 6
_STATIONARY_NAV_STATUS = _ANCHORED_NAV_STATUS | {_AGROUND_NAV_STATUS}

# A one-sample speed drop is a cue, not an alert (audit SP-6, prompt.md
# PHASE 7B). Promote possible_sudden_stop -> sudden_stop only once the stop has
# held this many scans / seconds without the vessel moving off its stop point.
_STOP_SETTLE_DISPLACEMENT_NM = 0.25  # ~460 m -- still "stopped", not drifting away


def _sudden_stop_min_samples() -> int:
    try:
        from core.config import config as _cfg
        return int(_cfg.AIS_SUDDEN_STOP_MIN_SAMPLES)
    except Exception:
        return 2


def _sudden_stop_persistence_s() -> float:
    try:
        from core.config import config as _cfg
        return float(_cfg.AIS_SUDDEN_STOP_PERSISTENCE_S)
    except Exception:
        return 300.0

# ── Known SAR hotspot zones (simple lat/lon/radius) ───────────────────────────
SAR_HOTSPOTS: list[dict[str, Any]] = [
    {"name": "Central Med / Lampedusa",   "lat": 35.50, "lon": 12.60, "radius_nm": 80},
    {"name": "Strait of Sicily",          "lat": 37.00, "lon": 11.50, "radius_nm": 90},
    {"name": "Zuwara departure zone",     "lat": 32.92, "lon": 12.08, "radius_nm": 60},
    {"name": "Libyan coast corridor",     "lat": 32.50, "lon": 14.00, "radius_nm": 120},
    {"name": "Tunisian coast SAR zone",   "lat": 33.90, "lon": 11.00, "radius_nm": 80},
    {"name": "Aegean / Lesvos",           "lat": 39.10, "lon": 26.55, "radius_nm": 50},
    {"name": "Aegean / Chios",            "lat": 38.37, "lon": 26.14, "radius_nm": 40},
    {"name": "Gulf of Aden",              "lat": 12.00, "lon": 47.00, "radius_nm": 200},
]

# ── Port exclusion zones (stop in port is not an anomaly) ────────────────────
PORT_ZONES: list[dict[str, Any]] = [
    {"lat": 35.50, "lon": 12.60, "radius_nm": 2,  "name": "Lampedusa port"},
    {"lat": 37.50, "lon": 15.09, "radius_nm": 3,  "name": "Catania port"},
    {"lat": 38.12, "lon": 13.35, "radius_nm": 4,  "name": "Palermo port"},
    {"lat": 35.90, "lon": 14.51, "radius_nm": 3,  "name": "Valletta port"},
    {"lat": 37.29, "lon": 13.53, "radius_nm": 2,  "name": "Porto Empedocle"},
    {"lat": 36.73, "lon": 14.85, "radius_nm": 2,  "name": "Pozzallo port"},
    {"lat": 32.90, "lon": 13.18, "radius_nm": 5,  "name": "Tripoli port"},
    {"lat": 34.74, "lon": 10.76, "radius_nm": 3,  "name": "Sfax port"},
    {"lat": 39.10, "lon": 26.55, "radius_nm": 3,  "name": "Mytilene port"},
    {"lat": 38.42, "lon": 27.14, "radius_nm": 5,  "name": "İzmir port"},
]


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


def _bearing_delta(b1: float, b2: float) -> float:
    """Smallest angular difference between two bearings (0–180°)."""
    d = abs(b1 - b2) % 360
    return d if d <= 180 else 360 - d


def _in_hotspot(lat: float, lon: float) -> Optional[str]:
    for hs in SAR_HOTSPOTS:
        if _haversine_nm(lat, lon, hs["lat"], hs["lon"]) <= hs["radius_nm"]:
            return hs["name"]
    return None


def _ngo_circular_pattern(mmsi: str) -> Optional[tuple[float, str]]:
    """Fit the vessel's last SEARCH_TRACK_WINDOW_MIN of track. Returns
    (radius_m, detail) when it looks like a worked search box (a tight,
    near-closed ring), else None."""
    try:
        from datetime import timedelta

        from core.mda.watch import _circle_fit
        from core.vessels.track_store import track_store

        pts = track_store.track(
            mmsi,
            since=datetime.now(timezone.utc) - timedelta(minutes=SEARCH_TRACK_WINDOW_MIN),
            limit=200,
        )
        if len(pts) < SEARCH_TRACK_MIN_FIXES:
            return None
        r_m, resid = _circle_fit([(p["lat"], p["lon"]) for p in pts])
        if r_m and SEARCH_CIRCLE_MIN_M <= r_m <= SEARCH_CIRCLE_MAX_M and resid < SEARCH_CIRCLE_MAX_RESID:
            return r_m, f"ring radius ~{r_m:.0f} m over {len(pts)} fixes in {SEARCH_TRACK_WINDOW_MIN} min"
    except Exception as exc:  # pragma: no cover
        logger.debug("_ngo_circular_pattern(%s) failed: %s", mmsi, exc)
    return None


def _nearby_active_distress(lat: float, lon: float) -> Optional[dict[str, Any]]:
    """Nearest ACTIVE distress/IOM-incident intel event within
    RESPONSE_CROSSCHECK_RADIUS_NM, or None. Context only -- proximity is never
    proof that a vessel is responding to it."""
    try:
        best: Optional[dict[str, Any]] = None
        for ev in intel_store.events(limit=300, max_age_days=RESPONSE_CROSSCHECK_MAX_AGE_DAYS):
            if ev.type not in _DISTRESS_TYPES and not ev.metadata.get("is_distress"):
                continue
            if str(ev.metadata.get("incident_lifecycle") or "active") in _RESOLVED_LIFECYCLES:
                continue
            if ev.lat is None or ev.lon is None:
                continue
            dist = _haversine_nm(lat, lon, ev.lat, ev.lon)
            if dist > RESPONSE_CROSSCHECK_RADIUS_NM:
                continue
            if best is None or dist < best["distance_nm"]:
                best = {"case_id": ev.id, "title": (ev.title or "")[:120], "distance_nm": round(dist, 1)}
        return best
    except Exception as exc:  # pragma: no cover
        logger.debug("_nearby_active_distress failed: %s", exc)
        return None


def _in_port(lat: float, lon: float) -> bool:
    for pz in PORT_ZONES:
        if _haversine_nm(lat, lon, pz["lat"], pz["lon"]) <= pz["radius_nm"]:
            return True
    return False


class AISSpikeDetector:
    """
    Scans the live VesselRegistry every POLL_INTERVAL_S seconds.
    Maintains minimal state (prev speed/course/position per MMSI).
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # mmsi → {speed, course, lat, lon, ts, loiter_start}
        self._prev: dict[str, dict[str, Any]] = {}
        self._ngo_mmsis = ngo_mmsi_set()
        # Track already-emitted spikes to avoid storm of duplicates
        self._emitted: dict[str, float] = {}   # key → monotonic timestamp
        self._emit_cooldown_s = 30 * 60         # same spike re-emitted at most once per 30 min

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="intel-ais-spike"
        )
        self._thread.start()
        logger.info("AISSpikeDetector started (poll=%ds)", POLL_INTERVAL_S)

    def stop(self) -> None:
        self._running = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Wait for AIS data to accumulate
        time.sleep(60)
        try:
            restored = backfill_recent_sar_activity()
            if restored:
                logger.info("AIS SAR normalization backfilled %d recent observations", restored)
        except Exception as exc:  # pragma: no cover - reconciliation is best effort
            logger.warning("AIS SAR normalization backfill failed: %s", exc)
        while self._running:
            try:
                self._scan()
            except Exception as exc:
                logger.warning("AISSpikeDetector scan error: %s", exc)
            time.sleep(POLL_INTERVAL_S)

    def _scan(self) -> None:
        from core.vessels.registry import registry  # lazy to avoid circular import

        geojson = registry.get_geojson()
        features = geojson.get("features", [])
        if not features:
            return

        # Build current vessel snapshot
        vessels: list[dict[str, Any]] = []
        for feat in features:
            props = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates", [None, None])
            if not coords or coords[0] is None or coords[1] is None:
                continue
            # registry.get_geojson emits `speed`/`course`; some callers and the
            # test fixtures use `last_speed`/`last_course` -- accept either.
            nav_raw = props.get("nav_status")
            v: dict[str, Any] = {
                "mmsi":        str(props.get("mmsi", "")),
                "name":        props.get("ship_name", ""),
                "lat":         float(coords[1]),
                "lon":         float(coords[0]),
                "speed":       float(props.get("last_speed", props.get("speed", 0)) or 0),
                "course":      float(props.get("last_course", props.get("course", 0)) or 0),
                "nav_status":  int(nav_raw) if nav_raw is not None else None,
                "last_seen":   str(props.get("last_seen", "")),
                "destination": str(props.get("destination", "") or ""),
            }
            vessels.append(v)

        now_mono = time.monotonic()
        # Snapshot the previous positions BEFORE the state-update loop below
        # overwrites them -- the cluster check needs current-vs-previous to
        # tell convergence from mere proximity (audit SP-2).
        prev_positions = {
            m: (s["lat"], s["lon"])
            for m, s in self._prev.items()
            if s.get("lat") is not None and s.get("lon") is not None
        }

        # ── Rule 1: Sudden stop ────────────────────────────────────────────────
        for v in vessels:
            mmsi = v["mmsi"]
            prev = self._prev.get(mmsi)

            # Stop-tracking state carried across scans (audit SP-6): a stop is
            # only promoted from a cue to an alert once it has held.
            stop_fields: dict[str, Any] = {}

            if prev:
                was_underway = prev["speed"] >= SPEED_THRESHOLD_KN
                now_stopped  = v["speed"] <= STOP_THRESHOLD_KN
                nav_status   = v.get("nav_status")
                # The vessel's own AIS status already explains a stationary
                # hull -- anchored / moored / aground is not a casualty and not
                # loitering (prompt.md PHASE 7B/7C, audit SP-3).
                stationary_by_status = nav_status in _STATIONARY_NAV_STATUS
                open_water = not _in_port(v["lat"], v["lon"])

                if now_stopped and open_water and not stationary_by_status:
                    stop_since = prev.get("stop_since")
                    if stop_since is None and was_underway:
                        # Fresh transition: a single-sample cue, never a
                        # high-confidence alert on its own.
                        stop_since = now_mono
                        stop_fields = {
                            "stop_since": stop_since,
                            "stop_lat": v["lat"],
                            "stop_lon": v["lon"],
                            "stop_prev_speed": prev["speed"],
                            "stop_samples": 1,
                        }
                        self._emit_sudden_stop(
                            v, mmsi, promoted=False,
                            prev_speed=prev["speed"], samples=1,
                            persistence_s=0.0, displacement_nm=0.0,
                            nav_status=nav_status,
                        )
                    elif stop_since is not None:
                        samples = int(prev.get("stop_samples", 1)) + 1
                        persistence_s = now_mono - stop_since
                        displacement_nm = _haversine_nm(
                            prev.get("stop_lat", v["lat"]), prev.get("stop_lon", v["lon"]),
                            v["lat"], v["lon"],
                        )
                        stop_fields = {
                            "stop_since": stop_since,
                            "stop_lat": prev.get("stop_lat", v["lat"]),
                            "stop_lon": prev.get("stop_lon", v["lon"]),
                            "stop_prev_speed": prev.get("stop_prev_speed", prev["speed"]),
                            "stop_samples": samples,
                        }
                        settled = displacement_nm <= _STOP_SETTLE_DISPLACEMENT_NM
                        if (
                            not prev.get("stop_promoted")
                            and samples >= _sudden_stop_min_samples()
                            and persistence_s >= _sudden_stop_persistence_s()
                            and settled
                        ):
                            self._emit_sudden_stop(
                                v, mmsi, promoted=True,
                                prev_speed=stop_fields["stop_prev_speed"],
                                samples=samples, persistence_s=persistence_s,
                                displacement_nm=displacement_nm, nav_status=nav_status,
                            )
                            stop_fields["stop_promoted"] = True
                        else:
                            stop_fields["stop_promoted"] = prev.get("stop_promoted", False)
                # else: moving again, in port, or nav-status stationary -- drop
                # any stop state (stop_fields left empty).

                # Track loiter start -- only when the vessel is not reporting
                # itself anchored / moored / aground.
                if now_stopped and open_water and not stationary_by_status:
                    if not prev.get("loiter_start"):
                        prev["loiter_start"] = now_mono
                    elif now_mono - prev["loiter_start"] >= LOITER_MIN_S:
                        hotspot = _in_hotspot(v["lat"], v["lon"])
                        if hotspot:
                            nav_known = nav_status is not None
                            self._emit(
                                spike_type="vessel_loiter",
                                mmsi=mmsi,
                                name=v["name"],
                                lat=v["lat"],
                                lon=v["lon"],
                                severity="medium",
                                detail=(
                                    f"Vessel {v['name'] or mmsi} loitering "
                                    f"{(now_mono - prev['loiter_start'])/60:.0f} min "
                                    f"in {hotspot}"
                                    + ("" if nav_known
                                       else " (AIS nav status unknown — cannot rule out anchoring)")
                                ),
                                ngo_info=get_ngo_info(mmsi),
                                metadata={
                                    "nav_status": nav_status,
                                    "nav_status_known": nav_known,
                                    "anchored_excluded": True,
                                },
                            )
                    stop_fields["loiter_start"] = prev.get("loiter_start")
                elif "loiter_start" in prev:
                    del prev["loiter_start"]

                # ── Rule 3: NGO search pattern ─────────────────────────────────
                if is_ngo(mmsi):
                    delta = _bearing_delta(prev["course"], v["course"])
                    zigzag = (delta >= SEARCH_BEARING_DELTA
                              and STOP_THRESHOLD_KN < v["speed"] <= SEARCH_SPEED_MAX_KN)
                    # The 2-sample bearing check above only ever sees the two
                    # most recent 5-min polls -- too coarse to catch a real
                    # search box unfolding between them. Fit the actual track.
                    circular = (
                        _ngo_circular_pattern(mmsi)
                        if STOP_THRESHOLD_KN < v["speed"] <= SEARCH_SPEED_MAX_KN else None
                    )
                    if zigzag or circular:
                        detail = (
                            f"{v['name'] or mmsi} ({get_ngo_info(mmsi)['org']}) "
                            f"executing search pattern"
                        )
                        if circular:
                            detail += f" — {circular[1]}"
                        elif zigzag:
                            detail += f" — bearing Δ {delta:.0f}° at {v['speed']:.1f} kn"
                        self._emit(
                            spike_type="ngo_search_pattern",
                            mmsi=mmsi,
                            name=v["name"],
                            lat=v["lat"],
                            lon=v["lon"],
                            severity="high",
                            detail=detail,
                            ngo_info=get_ngo_info(mmsi),
                            metadata={"pattern": "circular" if circular else "course_change"},
                        )

            # Update state. stop_fields carries the stop-tracking keys
            # (stop_since / stop_samples / stop_promoted / loiter_start) when
            # the vessel is still stopped; an empty stop_fields means the
            # vessel is moving again and the stop state is dropped.
            new_state: dict[str, Any] = {
                "speed":  v["speed"],
                "course": v["course"],
                "lat":    v["lat"],
                "lon":    v["lon"],
                "nav_status": v.get("nav_status"),
            }
            if "loiter_start" not in stop_fields:
                stop_fields["loiter_start"] = self._prev.get(mmsi, {}).get("loiter_start")
            new_state.update(stop_fields)
            self._prev[mmsi] = new_state

        # ── Rule 2: Rescue cluster ────────────────────────────────────────────
        self._check_clusters(vessels, prev_positions)

        # Prune stale spike cooldowns
        cutoff = now_mono - self._emit_cooldown_s
        self._emitted = {k: v for k, v in self._emitted.items() if v > cutoff}

    def _emit_sudden_stop(
        self,
        v: dict[str, Any],
        mmsi: str,
        *,
        promoted: bool,
        prev_speed: float,
        samples: int,
        persistence_s: float,
        displacement_nm: float,
        nav_status: int | None,
    ) -> None:
        """A single-sample speed drop emits `possible_sudden_stop` (a cue); the
        same stop, once it has held (`samples`/`persistence_s`) without the
        vessel moving off, emits `sudden_stop` (audit SP-6)."""
        hotspot = _in_hotspot(v["lat"], v["lon"])
        spike_type = "sudden_stop" if promoted else "possible_sudden_stop"
        if promoted:
            severity = "high" if (hotspot or is_ngo(mmsi)) else "medium"
        else:
            severity = "medium"
        detail = (
            f"Vessel {v['name'] or mmsi} "
            + ("stopped" if not promoted else f"stopped and held {persistence_s / 60:.0f} min")
            + f" (was {prev_speed:.1f} kn → {v['speed']:.1f} kn)"
            + (f" in {hotspot}" if hotspot else "")
            + ("" if promoted else " — single-sample cue, awaiting persistence")
        )
        self._emit(
            spike_type=spike_type,
            mmsi=mmsi,
            name=v["name"],
            lat=v["lat"],
            lon=v["lon"],
            severity=severity,
            detail=detail,
            ngo_info=get_ngo_info(mmsi),
            metadata={
                "stop_samples": samples,
                "stop_persistence_s": round(persistence_s),
                "stop_displacement_nm": round(displacement_nm, 2),
                "nav_status": nav_status,
                "promoted_from_cue": promoted,
            },
        )

    def _check_clusters(
        self,
        vessels: list[dict[str, Any]],
        prev_positions: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        """Groups of 2+ vessels within CLUSTER_RADIUS_NM.

        Only a group with *fresh* positions that is measurably *converging*
        (mean mutual distance falling scan-over-scan), is under way, and is
        not sitting in a port/anchorage is called a ``rescue_cluster``.
        Everything weaker is a ``possible_rescue_cluster`` -- proximity is
        never named convergence (audit SP-1/SP-2/SP-6, prompt.md PHASE 7A).
        """
        from datetime import UTC, datetime

        prev_positions = prev_positions or {}
        now = datetime.now(UTC)

        def _age_s(v: dict[str, Any]) -> float | None:
            raw = str(v.get("last_seen") or "")
            if not raw:
                return None
            try:
                seen = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            return (now - seen).total_seconds()

        positioned = []
        for v in vessels:
            if not (v.get("lat") and v.get("lon")):
                continue
            age = _age_s(v)
            # An unparseable / missing last_seen is treated as stale -- a
            # rescue cluster needs current positions (CLUSTER_AGE_S).
            if age is None or age > CLUSTER_AGE_S:
                continue
            v = {**v, "_age_s": age}
            positioned.append(v)

        seen_groups: set[frozenset[str]] = set()
        for i, v1 in enumerate(positioned):
            nearby = [
                (v2, _haversine_nm(v1["lat"], v1["lon"], v2["lat"], v2["lon"]))
                for j, v2 in enumerate(positioned)
                if i < j
                and _haversine_nm(v1["lat"], v1["lon"], v2["lat"], v2["lon"]) <= CLUSTER_RADIUS_NM
            ]
            if not nearby:
                continue
            group = [v1] + [v for v, _ in nearby]
            key = frozenset(v["mmsi"] for v in group)
            if key in seen_groups:
                continue
            seen_groups.add(key)

            ngo_vessels = [v for v in group if is_ngo(v["mmsi"])]
            if not ngo_vessels:
                continue  # only flag clusters that include a known NGO/CG vessel

            # ── proximity vs convergence ─────────────────────────────────────
            def _mean_pairwise(points: list[tuple[float, float]]) -> float | None:
                dists = [
                    _haversine_nm(points[a][0], points[a][1], points[b][0], points[b][1])
                    for a in range(len(points))
                    for b in range(a + 1, len(points))
                ]
                return sum(dists) / len(dists) if dists else None

            now_points = [(v["lat"], v["lon"]) for v in group]
            then_points = [prev_positions[v["mmsi"]] for v in group if v["mmsi"] in prev_positions]
            d_now = _mean_pairwise(now_points)
            d_then = _mean_pairwise(then_points) if len(then_points) >= 2 else None
            if d_then is None:
                converging: bool | None = None
                closing_nm = None
            else:
                closing_nm = round(d_then - d_now, 2)
                converging = closing_nm >= CLUSTER_CONVERGENCE_NM

            centroid_lat = sum(p[0] for p in now_points) / len(now_points)
            centroid_lon = sum(p[1] for p in now_points) / len(now_points)
            in_port = _in_port(centroid_lat, centroid_lon)
            moving = any(v.get("speed", 0) > STOP_THRESHOLD_KN for v in group)
            hotspot = _in_hotspot(v1["lat"], v1["lon"])
            distress = _nearby_active_distress(centroid_lat, centroid_lon)
            oldest_age = max(v["_age_s"] for v in group)

            strong = bool(
                converging and moving and not in_port and (hotspot or distress is not None)
            )
            spike_type = "rescue_cluster" if strong else "possible_rescue_cluster"
            if strong:
                severity = "critical" if hotspot else "high"
            else:
                severity = "medium"

            ngo_names = ", ".join(
                (get_ngo_info(v["mmsi"]) or {}).get("name") or v["mmsi"] for v in ngo_vessels
            )
            all_names = ", ".join(v.get("name") or v["mmsi"] for v in group[:4])
            verb = "converging" if converging else (
                "not converging" if converging is False else "convergence unknown"
            )
            detail = (
                f"{len(group)} vessels within {CLUSTER_RADIUS_NM:.0f} nm, {verb}"
                + (f" (closing {closing_nm:+.1f} nm)" if closing_nm is not None else "")
                + f" — NGO: {ngo_names} — vessels: {all_names}"
                + (" — in a port/anchorage" if in_port else "")
                + (f" — near active distress {distress['case_id']}" if distress else "")
            )

            self._emit(
                spike_type=spike_type,
                mmsi=ngo_vessels[0]["mmsi"],
                name=ngo_vessels[0].get("name", ""),
                lat=centroid_lat,
                lon=centroid_lon,
                severity=severity,
                detail=detail,
                ngo_info=get_ngo_info(ngo_vessels[0]["mmsi"]),
                metadata={
                    "cluster_size": len(group),
                    "vessel_names": all_names,
                    "converging": converging,
                    "closing_nm": closing_nm,
                    "positions_max_age_s": round(oldest_age, 0),
                    "in_port_or_anchorage": in_port,
                    "near_active_distress": distress["case_id"] if distress else None,
                },
            )

    # ── Emit helper ───────────────────────────────────────────────────────────

    def _emit(
        self,
        spike_type: str,
        mmsi: str,
        name: str,
        lat: float,
        lon: float,
        severity: str,
        detail: str,
        ngo_info: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        cooldown_key = f"{spike_type}:{mmsi}"
        now = time.monotonic()
        last_emit = self._emitted.get(cooldown_key)
        # A missing key means "never emitted" -- must never collide with a
        # freshly booted process, where time.monotonic() itself starts near
        # zero and a `0` default would make the very first emission look
        # like it happened at t=0 and is still within the cooldown window.
        if last_emit is not None and last_emit + self._emit_cooldown_s > now:
            return
        self._emitted[cooldown_key] = now

        meta: dict[str, Any] = {"spike_type": spike_type}
        if ngo_info:
            meta["org"] = ngo_info.get("org", "")
            meta["vessel_role"] = ngo_info.get("role", "")
        if metadata:
            meta.update(metadata)

        # Cross-check against active distress cases: proximity + rescue-like
        # motion is context for "possible response", never proof of one --
        # same non-causal framing as add_nearby_humanitarian_context.
        if spike_type in _RESCUE_RELEVANT_SPIKES:
            nearby = _nearby_active_distress(lat, lon)
            if nearby is not None:
                meta["possible_response_to"] = nearby
                detail = (
                    f"{detail} — {nearby['distance_nm']} nm from an active distress "
                    f"report ({nearby['title'] or nearby['case_id']}); possible response, "
                    f"not confirmed."
                )
                if severity not in ("critical",):
                    severity = "critical"

        event = IntelEvent(
            type="ais_spike",
            severity=severity,
            lat=lat,
            lon=lon,
            title=f"AIS: {spike_type.replace('_', ' ').title()} — {name or mmsi}",
            text=detail,
            url=f"https://www.marinetraffic.com/en/ais/details/ships/{mmsi}",
            source="AIS Registry",
            linked_mmsi=mmsi,
            metadata=meta,
        )
        added = intel_store.add(event)
        if added:
            logger.info("AIS spike [%s] %s @ %.3f,%.3f", spike_type, name or mmsi, lat, lon)
            normalized = _normalized_sar_activity(event, ngo_info)
            if normalized is not None:
                intel_store.add(normalized, dedup_key=f"sar-activity:{normalized.id}")
            from core.intel.triangulation import evaluate as evaluate_triangulation
            evaluate_triangulation(event)

def _normalized_sar_activity(
    source_event: IntelEvent,
    responder: Optional[dict[str, Any]],
) -> Optional[IntelEvent]:
    """Normalize qualified responder AIS behaviour into a neutral SAR observation.

    One AIS lineage can establish the observed track pattern; it cannot by itself
    establish that a casualty existed or that a rescue occurred.
    """
    if not responder or responder.get("status") != "active":
        return None
    meta = source_event.metadata or {}
    spike_type = str(meta.get("spike_type") or "")
    if spike_type == "ngo_search_pattern":
        activity_kind = "search_pattern_observed"
        label = "search-pattern activity"
        observation = "AIS track shows repeated low-speed course changes consistent with a search pattern."
    elif (
        spike_type == "rescue_cluster"
        and meta.get("converging") is True
        and meta.get("in_port_or_anchorage") is False
    ):
        activity_kind = "responder_convergence_observed"
        label = "multi-vessel convergence"
        observation = "AIS tracks show a known SAR responder converging with nearby vessels offshore."
    else:
        return None

    try:
        observed = datetime.fromisoformat(source_event.timestamp_utc.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        observed = datetime.now(timezone.utc)
    bucket = int(observed.timestamp() // (6 * 3600))
    vessel_name = str(responder.get("name") or source_event.title or "SAR responder")
    return IntelEvent(
        id=f"saractivity:{source_event.linked_mmsi}:{activity_kind}:{bucket}",
        timestamp_utc=source_event.timestamp_utc,
        type="ngo_activity",
        severity="medium",
        lat=source_event.lat,
        lon=source_event.lon,
        title=f"SAR responder {label} — {vessel_name}",
        text=(
            f"{observation} This is a single-lineage AIS observation; "
            "it does not by itself confirm a distress case or rescue."
        ),
        source="SeaCommons AIS analysis",
        linked_mmsi=source_event.linked_mmsi,
        metadata={
            "maritime_domain": "sar",
            "is_distress": False,
            "publication_status": "published",
            "source_policy": "official_api",
            "verification_status": "single_source_observed",
            "coordinate_source": "ais_position",
            "evidence_stage": "derived",
            "source_lineage": "ais_sensor_lineage",
            "independent_source_count": 1,
            "evidence_count": 1,
            "live_valid_for_s": 24 * 3600,
            "activity_kind": activity_kind,
            "observation_type": "sar_responder_activity",
            "operator_type": responder.get("operator_type") or "unknown",
            "org": responder.get("org"),
            "vessel_role": responder.get("role"),
            "vessel_name": vessel_name,
            "normalized_from": source_event.id,
            "possible_response_to": meta.get("possible_response_to"),
            "public_summary": f"{observation} No rescue or casualty is confirmed from AIS alone.",
        },
    )


def backfill_recent_sar_activity(*, max_age_days: int = 1, limit: int = 5000) -> int:
    """Reconcile recent qualified AIS responder cues through the same normalizer."""
    source_events = intel_store.persisted_events(
        types=["ais_spike"], max_age_days=max_age_days, limit=limit
    )
    existing = {
        event.id: event
        for event in intel_store.persisted_events(
            types=["ngo_activity"], max_age_days=max(2, max_age_days), limit=limit
        )
        if event.id.startswith("saractivity:")
    }
    handled: set[str] = set()
    written = 0
    for event in source_events:
        normalized = _normalized_sar_activity(event, get_ngo_info(event.linked_mmsi))
        if normalized is None or normalized.id in handled:
            continue
        handled.add(normalized.id)
        previous = existing.get(normalized.id)
        if previous is not None and str(previous.timestamp_utc or "") >= str(
            normalized.timestamp_utc or ""
        ):
            continue
        if intel_store.add(normalized, dedup_key=f"sar-activity:{normalized.id}"):
            written += 1
    return written
