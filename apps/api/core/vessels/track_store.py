# SPDX-License-Identifier: AGPL-3.0-or-later
"""AIS track history — the time-series primitive the dark-vessel detectors run on.

`core/vessels/registry.py` keeps only the *latest* position per MMSI. Gap /
rendezvous (STS) / loiter / spoof-pattern / identity-drift detection all need a
*history*. This module subscribes to the single shared AIS position hook
(`core/vessels/aisstream.register_position_hook`), throttles to at most one row
per MMSI per `VESSEL_TRACK_MIN_INTERVAL_S`, buffers, and bulk-inserts into
`vessel_tracks` on an interval. A daily prune keeps a rolling window.

Fast reads that don't need the DB (last-seen, silence sweep) are served from an
in-memory `_last` dict kept in step with the writes.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config import config

logger = logging.getLogger(__name__)

_FLUSH_INTERVAL_S = 10
_SYNC = os.getenv("SEACOMMONS_TRACK_STORE_SYNC", "").lower() in {"1", "true", "yes"}


@dataclass
class _Last:
    lat: float
    lon: float
    sog: float
    ts: float          # epoch seconds (AIS-reported)
    nav_status: Optional[int]
    name: str


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return r_nm * 2 * math.asin(math.sqrt(max(0.0, a)))


class TrackStore:
    def __init__(self) -> None:
        self._buffer: list[dict[str, Any]] = []
        self._buf_lock = threading.Lock()
        self._last: dict[str, _Last] = {}
        self._last_write_epoch: dict[str, float] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_prune = 0.0

    # ── ingest ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running or not getattr(config, "VESSEL_TRACK_ENABLED", True):
            return
        self._running = True
        try:
            hydrated = self.hydrate_recent_last_seen(max_age_hours=12.0)
            if hydrated:
                logger.info("TrackStore hydrated %d recent vessel states from DB", hydrated)
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore: recent-state hydration failed: %s", exc)
        try:
            from core.vessels import aisstream

            aisstream.register_position_hook(self.on_position)
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore: could not attach to AIS feed: %s", exc)
        if not _SYNC:
            self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="track-store-flush")
            self._thread.start()
        logger.info("TrackStore started (throttle=%ss, retention=%sd)",
                    getattr(config, "VESSEL_TRACK_MIN_INTERVAL_S", 60),
                    getattr(config, "VESSEL_TRACK_RETENTION_DAYS", 60))

    def stop(self) -> None:
        self._running = False

    def on_position(
        self, mmsi: str, name: str, lat: float, lon: float,
        sog: Optional[float] = None, nav_status: Optional[int] = None,
        cog: Optional[float] = None, heading: Optional[float] = None,
        received_at: Optional[datetime] = None,
        source: str = "aisstream", observed_at: Optional[datetime] = None,
    ) -> None:
        if not mmsi or lat is None or lon is None:
            return
        now_epoch = time.time()
        sog_v = float(sog) if sog is not None else 0.0
        prev = self._last.get(mmsi)
        recv = received_at or datetime.now(timezone.utc)
        ts = observed_at or recv

        # Throttle: one row per MMSI per interval, UNLESS the nav status changed
        # or the vessel jumped a long way (both are signal, not noise).
        interval = float(getattr(config, "VESSEL_TRACK_MIN_INTERVAL_S", 60))
        keep = True
        if prev is not None:
            since = now_epoch - self._last_write_epoch.get(mmsi, 0.0)
            status_changed = nav_status is not None and nav_status != prev.nav_status
            jumped = _haversine_nm(prev.lat, prev.lon, lat, lon) > 5.0
            if since < interval and not status_changed and not jumped:
                keep = False

        self._last[mmsi] = _Last(lat, lon, sog_v, now_epoch, nav_status, name or (prev.name if prev else ""))
        if not keep:
            return
        self._last_write_epoch[mmsi] = now_epoch

        row = {
            "mmsi": mmsi, "ts": ts, "received_at": recv,
            "lat": float(lat), "lon": float(lon),
            "sog": sog_v,
            "cog": float(cog) if cog is not None else None,
            "heading": float(heading) if heading is not None else None,
            "nav_status": int(nav_status) if nav_status is not None else None,
            "source": str(source or "aisstream")[:24],
        }
        with self._buf_lock:
            self._buffer.append(row)
        if _SYNC:
            self.flush()

    def on_reconciled_fix(self, fix) -> None:
        self.on_position(
            fix.mmsi, fix.ship_name, fix.lat, fix.lon,
            sog=fix.sog, nav_status=fix.nav_status, cog=fix.cog, heading=fix.heading,
            received_at=fix.received_at, source=fix.selected_upstream,
            observed_at=fix.observed_at,
        )

    # ── flush / prune ────────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(_FLUSH_INTERVAL_S)
            try:
                self.flush()
            except Exception as exc:  # pragma: no cover
                logger.warning("TrackStore flush error: %s", exc)
            if time.time() - self._last_prune > 6 * 3600:
                self._last_prune = time.time()
                try:
                    self.prune()
                except Exception as exc:  # pragma: no cover
                    logger.warning("TrackStore prune error: %s", exc)

    def flush(self) -> int:
        with self._buf_lock:
            if not self._buffer:
                return 0
            rows, self._buffer = self._buffer, []
        try:
            from core.db.models import VesselTrackDB
            from core.db.session import session_scope

            with session_scope() as db:
                db.bulk_insert_mappings(VesselTrackDB, rows)
            return len(rows)
        except Exception as exc:
            logger.warning("TrackStore: bulk insert failed (%d rows dropped): %s", len(rows), exc)
            return 0

    def prune(self, older_than_days: Optional[int] = None) -> int:
        days = older_than_days or int(getattr(config, "VESSEL_TRACK_RETENTION_DAYS", 60))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            from core.db.models import VesselTrackDB
            from core.db.session import session_scope

            with session_scope() as db:
                n = db.query(VesselTrackDB).filter(VesselTrackDB.ts < cutoff).delete(synchronize_session=False)
            if n:
                logger.info("TrackStore: pruned %d track rows older than %dd", n, days)
            return n
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore prune failed: %s", exc)
            return 0

    def hydrate_recent_last_seen(self, *, max_age_hours: float = 12.0) -> int:
        """Restore the latest persisted fix per MMSI into the in-memory gap index.

        Gap detection uses the in-memory last-fix map for fast silence sweeps.
        Rebuilding it from durable track history at startup makes open gaps
        survive API restarts instead of disappearing until a vessel transmits again.
        """
        try:
            from sqlalchemy import func

            from core.db.models import VesselTrackDB
            from core.db.session import session_scope

            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            with session_scope() as db:
                ranked = db.query(
                    VesselTrackDB.mmsi.label("mmsi"),
                    VesselTrackDB.ts.label("ts"),
                    VesselTrackDB.lat.label("lat"),
                    VesselTrackDB.lon.label("lon"),
                    VesselTrackDB.sog.label("sog"),
                    VesselTrackDB.nav_status.label("nav_status"),
                    func.row_number().over(
                        partition_by=VesselTrackDB.mmsi,
                        order_by=VesselTrackDB.ts.desc(),
                    ).label("rank"),
                ).filter(VesselTrackDB.ts >= cutoff).subquery()
                rows = db.query(ranked).filter(ranked.c.rank == 1).all()

            hydrated = 0
            for row in rows:
                if row.ts is None or row.lat is None or row.lon is None:
                    continue
                observed = row.ts
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                mmsi = str(row.mmsi or "")
                if not mmsi:
                    continue
                self._last[mmsi] = _Last(
                    float(row.lat),
                    float(row.lon),
                    float(row.sog or 0.0),
                    observed.timestamp(),
                    int(row.nav_status) if row.nav_status is not None else None,
                    "",
                )
                hydrated += 1
            return hydrated
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore hydration failed: %s", exc)
            return 0

    # ── reads ────────────────────────────────────────────────────────────────

    def last_seen(self, mmsi: str) -> Optional[datetime]:
        last = self._last.get(mmsi)
        return datetime.fromtimestamp(last.ts, tz=timezone.utc) if last else None

    def silent_since(self, min_silent_s: float, *, max_silent_s: float = 12 * 3600,
                     min_speed_kn: float = 1.0) -> list[tuple[str, _Last]]:
        """Vessels last heard *underway* between min and max seconds ago — the
        candidate set for a deliberate-disabling gap. In-memory, no DB."""
        now = time.time()
        out: list[tuple[str, _Last]] = []
        for mmsi, last in list(self._last.items()):
            silent = now - last.ts
            if min_silent_s <= silent <= max_silent_s and last.sog >= min_speed_kn:
                out.append((mmsi, last))
        return out

    def track(self, mmsi: str, *, since: Optional[datetime] = None,
              until: Optional[datetime] = None, limit: int = 5000) -> list[dict[str, Any]]:
        try:
            from core.db.models import VesselTrackDB
            from core.db.session import session_scope

            with session_scope() as db:
                q = db.query(VesselTrackDB).filter(VesselTrackDB.mmsi == mmsi)
                if since is not None:
                    q = q.filter(VesselTrackDB.ts >= since)
                if until is not None:
                    q = q.filter(VesselTrackDB.ts <= until)
                rows = q.order_by(VesselTrackDB.ts.asc()).limit(limit).all()
                return [_row_dict(r) for r in rows]
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore.track failed: %s", exc)
            return []

    def recent_tracks(
        self,
        mmsis: set[str],
        *,
        since: datetime,
        limit_per_mmsi: int = 120,
    ) -> dict[str, list[dict[str, Any]]]:
        """Load bounded recent histories for many vessels in one DB query."""
        clean = sorted({str(m) for m in mmsis if str(m).isdigit() and len(str(m)) == 9})
        if not clean:
            return {}
        try:
            from sqlalchemy import func

            from core.db.models import VesselTrackDB
            from core.db.session import session_scope

            with session_scope() as db:
                ranked = db.query(
                    VesselTrackDB.mmsi.label("mmsi"),
                    VesselTrackDB.ts.label("ts"),
                    VesselTrackDB.lat.label("lat"),
                    VesselTrackDB.lon.label("lon"),
                    VesselTrackDB.sog.label("sog"),
                    VesselTrackDB.cog.label("cog"),
                    VesselTrackDB.heading.label("heading"),
                    VesselTrackDB.nav_status.label("nav_status"),
                    VesselTrackDB.source.label("source"),
                    func.row_number().over(
                        partition_by=VesselTrackDB.mmsi,
                        order_by=VesselTrackDB.ts.desc(),
                    ).label("rank"),
                ).filter(
                    VesselTrackDB.mmsi.in_(clean),
                    VesselTrackDB.ts >= since,
                ).subquery()
                rows = db.query(ranked).filter(
                    ranked.c.rank <= max(2, min(int(limit_per_mmsi), 240))
                ).order_by(ranked.c.mmsi.asc(), ranked.c.ts.asc()).all()
                grouped: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    grouped.setdefault(row.mmsi, []).append({
                        "mmsi": row.mmsi,
                        "ts": row.ts.isoformat() if row.ts else None,
                        "lat": row.lat,
                        "lon": row.lon,
                        "sog": row.sog,
                        "cog": row.cog,
                        "heading": row.heading,
                        "nav_status": row.nav_status,
                        "source": row.source,
                    })
                return grouped
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore.recent_tracks failed: %s", exc)
            return {}

    def positions_between(self, t0: datetime, t1: datetime, *,
                          bbox: Optional[tuple[float, float, float, float]] = None,
                          limit: int = 200_000) -> list[dict[str, Any]]:
        """All positions in a time window (optionally a lon/lat bbox
        min_lon,min_lat,max_lon,max_lat) — the input to a rendezvous / STS scan."""
        try:
            from core.db.models import VesselTrackDB
            from core.db.session import session_scope

            with session_scope() as db:
                q = db.query(VesselTrackDB).filter(
                    VesselTrackDB.ts >= t0, VesselTrackDB.ts <= t1
                )
                if bbox is not None:
                    min_lon, min_lat, max_lon, max_lat = bbox
                    q = q.filter(
                        VesselTrackDB.lon >= min_lon, VesselTrackDB.lon <= max_lon,
                        VesselTrackDB.lat >= min_lat, VesselTrackDB.lat <= max_lat,
                    )
                rows = q.order_by(VesselTrackDB.ts.asc()).limit(limit).all()
                return [_row_dict(r) for r in rows]
        except Exception as exc:  # pragma: no cover
            logger.warning("TrackStore.positions_between failed: %s", exc)
            return []

    def stats(self) -> dict[str, Any]:
        try:
            from core.db.models import VesselTrackDB
            from core.db.session import session_scope
            from sqlalchemy import func

            with session_scope() as db:
                total = db.query(func.count(VesselTrackDB.id)).scalar() or 0
                distinct = db.query(func.count(func.distinct(VesselTrackDB.mmsi))).scalar() or 0
        except Exception:
            total = distinct = 0
        with self._buf_lock:
            pending = len(self._buffer)
        return {"rows": total, "mmsi": distinct, "tracked_live": len(self._last), "buffer": pending}


def _row_dict(r: Any) -> dict[str, Any]:
    return {
        "mmsi": r.mmsi,
        "ts": r.ts.isoformat() if r.ts else None,
        "lat": r.lat, "lon": r.lon, "sog": r.sog, "cog": r.cog,
        "heading": r.heading, "nav_status": r.nav_status, "source": r.source,
    }


track_store = TrackStore()
