from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any
from urllib.request import Request, urlopen

NETWORK = "aiscatcher_community"
PROFILE_URL = "https://www.aiscatcher.org/station/{station_id}"
SNAPSHOT_MINUTES = 15
SNAPSHOT_MAX_AGE_MINUTES = 20
STATION_RX_FRESH_MINUTES = 10
MIN_ONLINE_24H = 0.75
MIN_MESSAGES_1H = 50
MIN_VESSELS_1H = 3

# Central-Mediterranean community stations useful as coverage witnesses.
# These pages are public station-health profiles. We deliberately do not call
# AIS-catcher's /api endpoints or ingest its per-vessel feed.
STATION_IDS = ("3372", "1553", "2146", "2931", "3697")


@dataclass(frozen=True)
class ParsedStation:
    station_id: str
    label: str
    lat: float
    lon: float
    status: str
    last_received_at: datetime | None
    online_24h: float
    max_reception_km: float | None
    messages_1h: int
    vessels_1h: int


def _text(raw: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def _grab(raw: str, pattern: str) -> str | None:
    match = re.search(pattern, raw, re.I | re.S)
    return _text(match.group(1)) if match else None


def _number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("UTC", "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
def parse_station_profile(station_id: str, raw: str) -> ParsedStation:
    label = _grab(raw, r"<h1[^>]*>(.*?)</h1>") or f"Station {station_id}"
    status = (_grab(raw, r'id="station-state"[^>]*>(.*?)</span>') or "unknown").lower()
    last_received = _parse_utc(_grab(raw, r'id="station-last"[^>]*>(.*?)</dd>'))
    online_pct = _number(_grab(raw, r'id="station-online"[^>]*>(.*?)</dd>')) or 0.0
    max_nmi = _number(_grab(raw, r"Furthest[^<]*</dt><dd[^>]*>(.*?)</dd>"))
    messages = int(_number(_grab(raw, r'id="station-messages"[^>]*>(.*?)</dd>')) or 0)
    vessels = int(_number(_grab(raw, r'id="station-vessels"[^>]*>(.*?)</dd>')) or 0)
    loc = re.search(r"livemap\?lat=([-0-9.]+)&lon=([-0-9.]+)", raw)
    if loc is None:
        raise ValueError(f"AIS station {station_id} has no public coordinates")
    return ParsedStation(
        station_id=str(station_id),
        label=label[:128],
        lat=float(loc.group(1)),
        lon=float(loc.group(2)),
        status=status,
        last_received_at=last_received,
        online_24h=max(0.0, min(1.0, online_pct / 100.0)),
        max_reception_km=(max_nmi * 1.852 if max_nmi is not None else None),
        messages_1h=messages,
        vessels_1h=vessels,
    )


def _fetch_profile(station_id: str) -> str:
    request = Request(
        PROFILE_URL.format(station_id=station_id),
        headers={"User-Agent": "SeaCommons/1.0 coverage-witness"},
    )
    with urlopen(request, timeout=8) as response:
        return response.read(500_000).decode("utf-8", errors="replace")
def refresh_public_coverage_snapshots(
    *,
    fetch_profile=_fetch_profile,
    station_ids: tuple[str, ...] = STATION_IDS,
) -> dict[str, int]:
    """Persist bounded public health snapshots for selected AIS stations."""
    from core.db.models import AISCoverageSnapshotDB
    from core.db.session import session_scope

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bucket = now.replace(
        minute=(now.minute // SNAPSHOT_MINUTES) * SNAPSHOT_MINUTES,
        second=0,
        microsecond=0,
    )
    counts = {"saved": 0, "failed": 0}
    with session_scope() as db:
        for station_id in station_ids:
            try:
                parsed = parse_station_profile(station_id, fetch_profile(station_id))
            except Exception:
                counts["failed"] += 1
                continue
            snapshot_id = f"{NETWORK}:{station_id}:{bucket.isoformat()}"
            row: Any = db.query(AISCoverageSnapshotDB).filter_by(snapshot_id=snapshot_id).one_or_none()
            if row is None:
                row = AISCoverageSnapshotDB(snapshot_id=snapshot_id)
                db.add(row)
                counts["saved"] += 1
            row.network = NETWORK
            row.station_id = station_id
            row.station_label = parsed.label
            row.source_url = PROFILE_URL.format(station_id=station_id)
            row.lat = parsed.lat
            row.lon = parsed.lon
            row.station_status = parsed.status
            row.station_last_received_at = parsed.last_received_at
            row.online_24h = parsed.online_24h
            row.max_reception_km = parsed.max_reception_km
            row.messages_1h = parsed.messages_1h
            row.vessels_1h = parsed.vessels_1h
            row.observed_at = now
    return counts


def _distance_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    dlat, dlon = radians(b_lat - a_lat), radians(b_lon - a_lon)
    value = sin(dlat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(value))
def coverage_witnesses(
    lat: float,
    lon: float,
    *,
    at: datetime | None = None,
    max_results: int = 4,
) -> list[dict[str, object]]:
    """Return conservative station-health witnesses for an AIS observation.

    Coverage witnesses strengthen the AIS coverage premise only. They MUST NOT
    be counted as independent corroborating evidence for the vessel behaviour.
    """
    from core.db.models import AISCoverageSnapshotDB
    from core.db.session import session_scope

    target = (at or datetime.now(timezone.utc)).replace(tzinfo=None)
    oldest = target - timedelta(minutes=SNAPSHOT_MAX_AGE_MINUTES)
    newest = target + timedelta(minutes=SNAPSHOT_MAX_AGE_MINUTES)

    with session_scope() as db:
        db_rows = (
            db.query(AISCoverageSnapshotDB)
            .filter(
                AISCoverageSnapshotDB.observed_at >= oldest,
                AISCoverageSnapshotDB.observed_at <= newest,
            )
            .order_by(AISCoverageSnapshotDB.observed_at.desc())
            .all()
        )
        # Materialize while the ORM session is alive. session_scope commits on
        # exit, which expires ORM instances; carrying rows past this boundary
        # would make a read-only coverage check fail with DetachedInstanceError.
        rows: list[dict[str, Any]] = [
            {
                "station_id": str(row.station_id),
                "station_label": str(row.station_label),
                "lat": float(row.lat),
                "lon": float(row.lon),
                "station_status": str(row.station_status or ""),
                "station_last_received_at": row.station_last_received_at,
                "online_24h": float(row.online_24h or 0.0),
                "max_reception_km": float(row.max_reception_km or 0.0),
                "messages_1h": int(row.messages_1h or 0),
                "vessels_1h": int(row.vessels_1h or 0),
                "observed_at": row.observed_at,
            }
            for row in db_rows
        ]

    # One closest-in-time snapshot per station. Prefer proximity to the
    # event time, not simply the newest row, so replay cannot borrow a later
    # coverage state when an earlier snapshot is a better temporal witness.
    closest: dict[str, dict[str, Any]] = {}
    for row in rows:
        station_id = str(row["station_id"])
        prior = closest.get(station_id)
        if prior is None:
            closest[station_id] = row
            continue
        prior_delta = abs((target - prior["observed_at"]).total_seconds())
        row_delta = abs((target - row["observed_at"]).total_seconds())
        if row_delta < prior_delta:
            closest[station_id] = row

    result: list[dict[str, object]] = []
    for row in closest.values():
        if str(row["station_status"]).lower() != "active":
            continue
        if float(row["online_24h"]) < MIN_ONLINE_24H:
            continue
        if int(row["messages_1h"]) < MIN_MESSAGES_1H or int(row["vessels_1h"]) < MIN_VESSELS_1H:
            continue
        last_received_at = row["station_last_received_at"]
        if last_received_at is None:
            continue
        if abs((target - last_received_at).total_seconds()) > STATION_RX_FRESH_MINUTES * 60:
            continue
        radius = float(row["max_reception_km"])
        if radius <= 0:
            continue
        distance = _distance_km(lat, lon, float(row["lat"]), float(row["lon"]))
        if distance > radius:
            continue
        result.append(
            {
                "network": NETWORK,
                "station_id": str(row["station_id"]),
                "station_label": str(row["station_label"]),
                "distance_km": round(distance, 1),
                "observed_max_reception_km": round(radius, 1),
                "online_24h": round(float(row["online_24h"]), 3),
                "messages_1h": int(row["messages_1h"]),
                "vessels_1h": int(row["vessels_1h"]),
                "snapshot_at": row["observed_at"].isoformat() + "Z",
                "coverage_role": "same_lineage_coverage_witness",
            }
        )
    result.sort(key=lambda item: float(item["distance_km"]))
    return result[:max_results]
