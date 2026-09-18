"""
VesselRegistry — persistent SQLite vessel database with in-memory cache.

Architecture:
  - SQLite stores one row per MMSI (upsert on every AIS message)
  - In-memory dict mirrors the DB for instant reads (no disk I/O on GET)
  - DB writes happen in a background thread (fire-and-forget, non-blocking)
  - GeoJSON is rebuilt lazily only when the cache is dirty
  - Incremental updates via ?since=<ISO timestamp> keep the frontend fast
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_GEOJSON_CACHE_TTL_S = 10.0  # serve cached GeoJSON for up to 10 s even when dirty

_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vessels.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS vessels (
    mmsi          TEXT PRIMARY KEY,
    ship_name     TEXT,
    imo           TEXT,
    ship_type     INTEGER,
    flag          TEXT,
    ais_class     TEXT DEFAULT 'A',
    destination   TEXT,
    last_lat      REAL,
    last_lon      REAL,
    last_course   REAL,
    last_speed    REAL,
    last_heading  REAL,
    nav_status    INTEGER,
    last_seen     TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON vessels(last_seen);
"""

# Columns added after the original schema shipped. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so _init_db checks PRAGMA table_info first.
_ADDED_COLUMNS: dict[str, str] = {
    "nav_status": "INTEGER",
}

_UPSERT_SQL = """
INSERT INTO vessels (
    mmsi, ship_name, imo, ship_type, flag, ais_class,
    destination, last_lat, last_lon, last_course,
    last_speed, last_heading, nav_status, last_seen, updated_at
) VALUES (
    :mmsi, :ship_name, :imo, :ship_type, :flag, :ais_class,
    :destination, :last_lat, :last_lon, :last_course,
    :last_speed, :last_heading, :nav_status, :last_seen, :updated_at
)
ON CONFLICT(mmsi) DO UPDATE SET
    ship_name   = COALESCE(excluded.ship_name,   vessels.ship_name),
    imo         = COALESCE(excluded.imo,         vessels.imo),
    ship_type   = COALESCE(excluded.ship_type,   vessels.ship_type),
    flag        = COALESCE(excluded.flag,        vessels.flag),
    ais_class   = COALESCE(excluded.ais_class,   vessels.ais_class),
    destination = COALESCE(excluded.destination, vessels.destination),
    last_lat    = COALESCE(excluded.last_lat,    vessels.last_lat),
    last_lon    = COALESCE(excluded.last_lon,    vessels.last_lon),
    last_course = COALESCE(excluded.last_course, vessels.last_course),
    last_speed  = COALESCE(excluded.last_speed,  vessels.last_speed),
    last_heading= COALESCE(excluded.last_heading,vessels.last_heading),
    nav_status  = COALESCE(excluded.nav_status,  vessels.nav_status),
    last_seen   = COALESCE(excluded.last_seen,   vessels.last_seen),
    updated_at  = excluded.updated_at;
"""




def _normalized_ship_name(value: Any, mmsi: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text or text == mmsi:
        return f"MMSI {mmsi}"
    return text.upper()


def _vessel_visual_contract(v: dict[str, Any]) -> dict[str, Any]:
    raw_speed = v.get("last_speed")
    try:
        speed = float(raw_speed) if raw_speed is not None else None
    except (TypeError, ValueError):
        speed = None
    nav_status = v.get("nav_status")
    try:
        stationary_status = int(nav_status) in {1, 5}
    except (TypeError, ValueError):
        stationary_status = False
    motion_state = "unknown" if speed is None and not stationary_status else ("stationary" if stationary_status or (speed is not None and speed <= 0.5) else "moving")
    heading = v.get("last_heading")
    try:
        heading_value = float(heading) if heading is not None and 0 <= float(heading) < 360 else None
    except (TypeError, ValueError):
        heading_value = None
    if heading_value is None:
        course = v.get("last_course")
        try:
            heading_value = float(course) if course is not None and 0 <= float(course) < 360 else None
        except (TypeError, ValueError):
            heading_value = None
    moving = motion_state == "moving"
    return {
        "display_name": _normalized_ship_name(v.get("ship_name"), str(v["mmsi"])),
        "entity_kind": "vessel",
        "motion_state": motion_state,
        "visual_shape": "triangle" if moving else "circle",
        "visual_color": "#38bdf8" if moving else "#60a5fa",
        "layer_membership": ["maritime", "ais", f"ais_{motion_state}"],
        "show_heading": bool(moving and heading_value is not None),
        "heading_deg": heading_value,
        "report_type": "vessel",
        "report_id": str(v["mmsi"]),
    }

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class VesselRegistry:
    """Thread-safe vessel registry backed by SQLite with hot in-memory cache."""

    def __init__(self, db_path: Path = _DB_PATH):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self._source_context: dict[str, dict[str, list[str]]] = {}
        self._dirty = False
        self._geojson_cache: dict | None = None
        self._geojson_json: bytes | None = None  # pre-serialized, skips FastAPI encoder
        self._geojson_cache_ts: float = 0.0
        self._write_queue: list[dict] = []
        self._write_lock = threading.Lock()
        self._init_db()
        self._load_cache()

    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        con.executescript(_CREATE_SQL)
        have = {r[1] for r in con.execute("PRAGMA table_info(vessels)")}
        for col, decl in _ADDED_COLUMNS.items():
            if col not in have:
                con.execute(f"ALTER TABLE vessels ADD COLUMN {col} {decl}")
        con.commit()
        con.close()

    def _load_cache(self) -> None:
        """Load entire DB into memory at startup."""
        con = sqlite3.connect(self._db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM vessels").fetchall()
        con.close()
        with self._lock:
            self._cache = {r["mmsi"]: _row_to_dict(r) for r in rows}
            self._dirty = True  # force geojson rebuild on first call

    def upsert(
        self,
        mmsi: str,
        *,
        ship_name: str | None = None,
        imo: str | None = None,
        ship_type: int | None = None,
        flag: str | None = None,
        ais_class: str = "A",
        destination: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        course: float | None = None,
        speed: float | None = None,
        heading: float | None = None,
        nav_status: int | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ts = (last_seen or datetime.now(timezone.utc)).isoformat()

        row: dict[str, Any] = {
            "mmsi": mmsi,
            "ship_name": ship_name,
            "imo": str(imo) if imo else None,
            "ship_type": ship_type,
            "flag": flag,
            "ais_class": ais_class,
            "destination": destination,
            "last_lat": lat,
            "last_lon": lon,
            "last_course": course,
            "last_speed": speed,
            "last_heading": heading,
            "nav_status": nav_status,
            "last_seen": ts,
            "updated_at": now,
        }

        with self._lock:
            existing = self._cache.get(mmsi, {})
            merged: dict[str, Any] = {
                k: (row[k] if row[k] is not None else existing.get(k))
                for k in row
            }
            self._cache[mmsi] = merged
            self._dirty = True

        threading.Thread(target=self._db_write, args=(merged,), daemon=True).start()

    def upsert_reconciled(self, fix) -> None:
        with self._lock:
            self._source_context[fix.mmsi] = {
                "sources": sorted(fix.transport_providers),
                "upstream_sources": sorted(fix.upstream_sources),
                "stations": sorted(fix.station_ids),
            }
        self.upsert(
            fix.mmsi, ship_name=fix.ship_name or None, lat=fix.lat, lon=fix.lon,
            course=fix.cog, speed=fix.sog, heading=fix.heading,
            nav_status=fix.nav_status, last_seen=fix.observed_at,
        )

    def _db_write(self, data: dict) -> None:
        try:
            con = sqlite3.connect(self._db_path, timeout=5)
            con.execute(_UPSERT_SQL, data)
            con.commit()
            con.close()
        except Exception:
            pass  # best-effort; cache is source of truth

    def get_geojson(self, since: str | None = None) -> dict:
        """
        Return GeoJSON FeatureCollection.
        If `since` is an ISO timestamp, only return vessels updated after that time.
        """
        with self._lock:
            if not since:
                now_ts = time.monotonic()
                if (
                    self._geojson_cache is not None
                    and (not self._dirty or (now_ts - self._geojson_cache_ts) < _GEOJSON_CACHE_TTL_S)
                ):
                    return self._geojson_cache
            # Snapshot values under lock — O(1) copy of references, not dicts
            all_values = list(self._cache.values())

        # Filter outside the lock — fromisoformat on 9k rows takes ~20ms
        if since:
            try:
                cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
                vessels = [
                    v for v in all_values
                    if v.get("last_seen") and
                       datetime.fromisoformat(v["last_seen"]) >= cutoff
                ]
            except ValueError:
                vessels = all_values
        else:
            # Full load: only vessels active in the last 2 hours.
            # The DB accumulates months of data (9k+) but SAR only needs
            # vessels currently underway. Stale vessels are excluded here
            # but will re-appear in incremental ?since= polls when active again.
            active_cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            vessels = [v for v in all_values if (v.get("last_seen") or "") >= active_cutoff]

        features = []
        for v in vessels:
            if v.get("last_lat") is None or v.get("last_lon") is None:
                continue
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [v["last_lon"], v["last_lat"]],
                },
                "properties": {
                    "vessel_id": v["mmsi"],
                    "mmsi": v["mmsi"],
                    "ship_name": v.get("ship_name") or v["mmsi"],
                    "imo": v.get("imo"),
                    "ship_type": v.get("ship_type"),
                    "ais_class": v.get("ais_class", "A"),
                    "destination": v.get("destination") or "",
                    "course": v.get("last_course"),
                    "speed": v.get("last_speed"),
                    "heading": v.get("last_heading"),
                    "nav_status": v.get("nav_status"),
                    "last_seen": v.get("last_seen"),
                    "sources": self._source_context.get(v["mmsi"], {}).get("sources", ["aisstream"]),
                    "upstream_sources": self._source_context.get(v["mmsi"], {}).get("upstream_sources", ["aisstream"]),
                    "stations": self._source_context.get(v["mmsi"], {}).get("stations", []),
                    **_vessel_visual_contract(v),
                },
            })

        result = {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "total": len(features),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        if not since:
            serialized = json.dumps(result).encode()
            with self._lock:
                self._geojson_cache = result
                self._geojson_json = serialized
                self._geojson_cache_ts = time.monotonic()
                self._dirty = False

        return result

    def get_geojson_json(self) -> bytes | None:
        """Return pre-serialized GeoJSON bytes if the cache is still valid, else None."""
        with self._lock:
            if self._geojson_json is None:
                return None
            now_ts = time.monotonic()
            if not self._dirty or (now_ts - self._geojson_cache_ts) < _GEOJSON_CACHE_TTL_S:
                return self._geojson_json
        return None

    def get_last_known_geojson(self, mmsis: set[str] | frozenset[str] | None = None) -> dict:
        """Return persisted last-known vessel positions without a freshness cutoff.

        This is intentionally separate from get_geojson(), whose full snapshot
        represents current traffic and drops fixes older than two hours. Stable
        tracked fleets such as civil SAR need the last observed position even
        when AIS is temporarily unavailable, provided the timestamp remains
        explicit so callers never present a stale fix as live.
        """
        wanted = {str(value) for value in mmsis} if mmsis is not None else None
        with self._lock:
            vessels = [
                dict(value)
                for key, value in self._cache.items()
                if wanted is None or str(key) in wanted
            ]

        features = []
        for v in vessels:
            if v.get("last_lat") is None or v.get("last_lon") is None:
                continue
            mmsi = str(v["mmsi"])
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [v["last_lon"], v["last_lat"]],
                },
                "properties": {
                    "vessel_id": mmsi,
                    "mmsi": mmsi,
                    "ship_name": v.get("ship_name") or mmsi,
                    "imo": v.get("imo"),
                    "ship_type": v.get("ship_type"),
                    "ais_class": v.get("ais_class", "A"),
                    "destination": v.get("destination") or "",
                    "course": v.get("last_course"),
                    "speed": v.get("last_speed"),
                    "heading": v.get("last_heading"),
                    "nav_status": v.get("nav_status"),
                    "last_seen": v.get("last_seen"),
                    "position_timestamp_utc": v.get("last_seen"),
                    "sources": self._source_context.get(mmsi, {}).get("sources", ["aisstream"]),
                    "upstream_sources": self._source_context.get(mmsi, {}).get(
                        "upstream_sources", ["aisstream"]
                    ),
                    "stations": self._source_context.get(mmsi, {}).get("stations", []),
                    **_vessel_visual_contract(v),
                },
            })

        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "total": len(features),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "position_policy": "last_known",
            },
        }

    def stats(self) -> dict:
        # Snapshot values list outside the lock scan to minimise hold time.
        with self._lock:
            values = list(self._cache.values())
        total = len(values)
        positioned = sum(1 for v in values if v.get("last_lat") is not None)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        active = sum(1 for v in values if (v.get("last_seen") or "") >= cutoff)
        return {"total_known": total, "positioned": positioned, "active_30m": active}


# Module-level singleton — imported by AISStream client and API routes
registry = VesselRegistry()
