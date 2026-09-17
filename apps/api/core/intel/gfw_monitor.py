# SPDX-License-Identifier: AGPL-3.0-or-later
"""Global Fishing Watch event ingest — corroboration for our own AIS analysis.

GFW publishes (free, non-commercial) derived events for the whole Med + Black
Sea: encounters (STS proxy), loitering, AIS-disabling "gap" events, and
Sentinel-1 SAR vessel detections (matched / unmatched to AIS). We consume those
rather than rebuild them; each becomes an IntelEvent that can seed or corroborate
a fusion alert.

Needs `GFW_API_TOKEN` (config / secrets). Best-effort — a no-op without it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import config
from core.intel.store import IntelEvent, intel_store

logger = logging.getLogger(__name__)

_BASE = "https://gateway.api.globalfishingwatch.org/v3"
_BBOX = [-8.0, 28.0, 45.0, 48.0]   # min_lon, min_lat, max_lon, max_lat
_MAP = {
    # GFW activity is independent behavioural evidence, never a sanctions
    # allegation by itself. Sanctions context is added only after an explicit
    # identity/list match elsewhere in the evidence graph.
    "encounter": ("ais_rendezvous", "grey_zone", "high"),
    "loitering": ("loiter", "grey_zone", "medium"),
    "gap": ("long_gap", "grey_zone", "medium"),
    "port_visit": (None, None, None),
}

_DATASETS = {
    "encounter": "public-global-encounters-events:latest",
    "loitering": "public-global-loitering-events:latest",
    "gap": "public-global-gaps-events:latest",
}
def _dataset_for(gfw_type: str) -> str:
    return _DATASETS[gfw_type]


def _classification_for(gfw_type: str) -> tuple[str, str, str]:
    return _MAP[gfw_type]


def _event_request_body(gfw_type: str, since: str, until: str) -> dict[str, Any]:
    return {
        "datasets": [_dataset_for(gfw_type)],
        "startDate": since,
        "endDate": until,
        "geometry": {"type": "Polygon", "coordinates": [[
            [_BBOX[0], _BBOX[1]], [_BBOX[2], _BBOX[1]],
            [_BBOX[2], _BBOX[3]], [_BBOX[0], _BBOX[3]], [_BBOX[0], _BBOX[1]],
        ]]},
    }


def _valid_mmsi(value: Any) -> str:
    text = str(value or "").strip()
    return text if len(text) == 9 and text.isdigit() else ""


def _event_mmsis(entry: dict[str, Any]) -> tuple[str, ...]:
    candidates: list[Any] = []
    vessel = entry.get("vessel") or {}
    if isinstance(vessel, dict):
        candidates.extend((vessel.get("ssvid"), vessel.get("mmsi")))
    for item in entry.get("vessels") or []:
        if not isinstance(item, dict):
            continue
        ship = item.get("ship") or {}
        candidates.extend((item.get("ssvid"), item.get("mmsi")))
        if isinstance(ship, dict):
            candidates.extend((ship.get("ssvid"), ship.get("mmsi")))
    return tuple(dict.fromkeys(m for raw in candidates if (m := _valid_mmsi(raw))))


def _record_source_observation(
    eid: str, entry: dict, *, lat: float, lon: float, mmsis: tuple[str, ...] = (),
) -> None:
    """docs/updates.md P0.2: a durable, lossless SourceObservation for
    every GFW event this monitor receives -- before the existing
    intel_store.add() write path below. Best-effort and strictly
    additive: never raises into poll_once(), never alters what gets
    published."""
    try:
        from core.db.session import session_scope
        from core.intel.source_observation import record_observation

        with session_scope() as db:
            record_observation(
                db,
                service="maritime", lane="intelligence", observation_type="ais_derived_event",
                source_name="GFW", source_policy="official_api", source_id=eid,
                observed_at=str(entry.get("start") or ""),
                raw_payload=str(entry), lat=lat, lon=lon,
                subject_refs=[f"mmsi:{m}" for m in mmsis],
                provenance={"event_type": str(entry.get("type") or "")},
            )
    except Exception as exc:
        logger.debug("gfw_monitor: source_observation record skipped for %s: %s", eid, exc)


def poll_once() -> int:
    token = getattr(config, "GFW_API_TOKEN", "") or ""
    if not token:
        return 0
    try:
        import httpx
    except Exception:  # pragma: no cover
        return 0

    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    until = datetime.now(timezone.utc).date().isoformat()
    headers = {"Authorization": f"Bearer {token}"}
    n = 0
    for gfw_type in ("encounter", "loitering", "gap"):
        anom, domain, sev = _classification_for(gfw_type)
        try:
            r = httpx.post(
                f"{_BASE}/events",
                headers=headers,
                json=_event_request_body(gfw_type, since, until),
                params={"limit": 200, "offset": 0}, timeout=60)
            r.raise_for_status()
            entries = r.json().get("entries", [])
        except Exception as exc:
            logger.info("GFW %s poll skipped: %s", gfw_type, exc)
            continue
        for e in entries:
            pos = e.get("position") or {}
            lat, lon = pos.get("lat"), pos.get("lon")
            if lat is None or lon is None:
                continue
            eid = f"gfw:{gfw_type}:{e.get('id')}"
            mmsis = _event_mmsis(e)
            vessels = list(mmsis)
            _record_source_observation(
                eid, e, lat=float(lat), lon=float(lon), mmsis=mmsis,
            )
            added = intel_store.add(IntelEvent(
                id=eid,
                type="ais_rendezvous" if gfw_type == "encounter" else "ais_anomaly",
                severity=sev, lat=float(lat), lon=float(lon),
                title=f"GFW {gfw_type} — {', '.join(str(v) for v in vessels if v) or 'vessel'}",
                text=(f"Global Fishing Watch {gfw_type} event, "
                      f"{e.get('start')} to {e.get('end')}."),
                source="GFW", timestamp_utc=str(e.get("start") or ""),
                linked_mmsi=mmsis[0] if len(mmsis) == 1 else "",
                metadata={"anomaly_type": anom, "maritime_domain": domain,
                          "is_distress": False, "publication_status": "internal",
                          "source_policy": "official_api", "coordinate_source": "gfw",
                          "gfw_type": gfw_type, "vessels": vessels,
                          "tanker": any("tanker" in str(v.get("ship", {}).get("type", "")).lower()
                                        for v in (e.get("vessels") or []))},
            ), dedup_key=eid)
            n += 1 if added else 0
    if n:
        logger.info("GFW: %d events ingested", n)
    return n
