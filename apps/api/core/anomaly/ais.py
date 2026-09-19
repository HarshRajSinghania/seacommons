# SPDX-License-Identifier: AGPL-3.0-or-later
"""AIS anomaly detector — gap, impossible speed, duplicate MMSI, dark zones, OFAC SDN."""
from __future__ import annotations
import json
import logging
import math
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from core.config import config as _cfg

logger = logging.getLogger(__name__)

_SDN_CACHE = Path.home() / ".suezcanal" / "cache" / "sdn_mmsi.json"

# AIS dark zones (simplified — areas with known AIS reception gaps)
_DARK_ZONES: list[tuple[float, float, float, float]] = [
    (23.5, 37.0, 28.0, 42.0),   # Eastern Med AIS gap
    (10.0, 30.0, 16.0, 36.0),   # Libyan coast gap
]

# Maximum realistic speeds by vessel type (knots)
_MAX_SPEED: dict[str, float] = {
    "cargo": 30, "tanker": 20, "passenger": 35, "fishing": 20,
    "tug": 15, "sailing": 18, "default": 50,
}

# A vessel silence is only a vessel-specific `gap` when the AIS reception
# around it stayed healthy. If the nearby traffic went quiet at the same time
# it is a `coverage_gap` (a reception / source outage), never described as
# intentional dark activity and never escalated per vessel (audit GP-1/GP-5,
# prompt.md PHASE 8).
_GAP_NEIGHBOUR_RADIUS_NM = 40.0
_GAP_FRESH_S = 15 * 60          # a neighbour seen this recently is "still reporting"
_GAP_HISTORY_S = 6 * 3600       # a neighbour seen within this window "was reporting"
_GAP_MIN_NEIGHBOURS = 3         # need this many to judge coverage at all


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return r * 2 * math.asin(math.sqrt(max(0.0, a)))


class AISAnomalyEvent(BaseModel):
    event_id: str
    timestamp_utc: str
    # gap | coverage_gap | impossible_speed | mmsi_duplicate | dark_zone_entry | sdn_match
    anomaly_type: str
    mmsi: str
    vessel_name: str = ""
    position: dict
    confidence: float
    evidence: dict
    source: str = "ais"


class AISAnomalyDetector:
    def __init__(self, mock: bool = False, on_anomaly: Optional[Callable] = None):
        self.mock = mock or os.environ.get("MOCK", "").lower() == "true" or _cfg.MOCK
        self._on_anomaly = on_anomaly
        self._last_seen: dict[str, dict] = {}  # mmsi → {lat, lon, ts, speed, type, name}
        self._positions: dict[str, dict] = {}  # mmsi → latest position
        self._sdn_mmsi: set[str] = set()
        self._emitted: dict[tuple[str, str], float] = {}  # (mmsi, type) → last emit
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Attach to the shared AIS feed and run the silence-sweep thread.

        The old _ws_loop opened its own AISStream socket, which conflicts
        with the primary client (one connection per key on the free tier) --
        that is why this detector was never wired in. It now consumes the
        same PositionReports as everything else via the canonical AIS
        observation bus, preserving each fix's source timestamp.
        """
        self._running = True
        self._load_sdn()
        try:
            from core.vessels import aisstream

            aisstream.register_observation_hook(self._on_observation)
        except Exception as exc:
            logger.warning("AISAnomalyDetector: could not attach to AIS feed: %s", exc)
        self._thread = threading.Thread(
            target=self._silence_sweep_loop, daemon=True, name="ais-anomaly-sweep"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _on_observation(self, observation) -> None:
        """Consume a canonical AIS observation with source and receipt time."""
        if not self._running:
            return
        self.process_position(
            observation.mmsi,
            observation.ship_name,
            observation.lat,
            observation.lon,
            observation.sog or 0.0,
            "",
            observed_at=observation.observed_at,
        )

    def _on_feed_position(
        self, mmsi: str, name: str, lat: float, lon: float,
        sog: float | None, nav_status: int | None = None, *_extra,
    ) -> None:
        """Legacy adapter retained for tests/tools; runtime uses _on_observation."""
        if self._running:
            self.process_position(mmsi, name, lat, lon, sog or 0.0, "")

    def _silence_sweep_loop(self) -> None:
        """Emit a `gap` for a vessel last seen underway in open water that has
        now been silent past the threshold -- the absence of a message, which
        a per-message hook cannot see on its own."""
        from datetime import datetime, timezone

        while self._running:
            time.sleep(90)
            now = time.time()
            # Bounded memory on the small VM: drop tracks and cooldowns that
            # can no longer produce an event.
            self._last_seen = {
                m: s
                for m, s in self._last_seen.items()
                if now - s.get("received_ts", s["ts"]) < 12 * 3600
            }
            self._emitted = {
                k: t for k, t in self._emitted.items() if now - t < self._EMIT_COOLDOWN_S * 2
            }
            for mmsi, seen in list(self._last_seen.items()):
                if seen.get("gap_emitted"):
                    continue
                silent_s = now - seen.get("received_ts", seen["ts"])
                if silent_s < 900 or silent_s > 6 * 3600:
                    continue
                if seen.get("speed", 0) < 1.0 or self._in_dark_zone(seen["lat"], seen["lon"]):
                    continue
                seen["gap_emitted"] = True
                event = self._build_gap_event(mmsi, seen, silent_s, now)
                if event is not None:
                    self._emit(event)

    def _coverage_around(
        self, lat: float, lon: float, exclude_mmsi: str, now: float
    ) -> tuple[int, int]:
        """(neighbours that were reporting within _GAP_HISTORY_S,
        neighbours still reporting within _GAP_FRESH_S) inside
        _GAP_NEIGHBOUR_RADIUS_NM of (lat, lon)."""
        before = after = 0
        for m, s in self._last_seen.items():
            if m == exclude_mmsi or s.get("lat") is None or s.get("lon") is None:
                continue
            if _haversine_nm(lat, lon, s["lat"], s["lon"]) > _GAP_NEIGHBOUR_RADIUS_NM:
                continue
            age = now - s.get("received_ts", s["ts"])
            if age < _GAP_HISTORY_S:
                before += 1
            if age < _GAP_FRESH_S:
                after += 1
        return before, after

    def _build_gap_event(
        self, mmsi: str, seen: dict, silent_s: float, now: float
    ) -> Optional["AISAnomalyEvent"]:
        """A silence is a vessel `gap` only when nearby AIS coverage stayed
        healthy; otherwise it is a `coverage_gap` (a reception outage)."""
        from datetime import datetime, timezone

        lat, lon = seen["lat"], seen["lon"]
        before, after = self._coverage_around(lat, lon, mmsi, now)
        ratio = (after / before) if before else None
        coverage_collapsed = before >= _GAP_MIN_NEIGHBOURS and after == 0

        evidence = {
            "silent_seconds": round(silent_s),
            "sweep": True,
            "nearby_vessels_before": before,
            "nearby_vessels_after": after,
            "local_reporting_ratio": round(ratio, 3) if ratio is not None else None,
        }
        if coverage_collapsed:
            return AISAnomalyEvent(
                event_id=str(uuid.uuid4()),
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                anomaly_type="coverage_gap",
                mmsi=mmsi, vessel_name=seen.get("name", ""),
                position={"lat": lat, "lon": lon},
                confidence=0.4,
                evidence=evidence,
            )
        # Vessel-specific gap: more confident when we can see healthy coverage.
        conf = min(0.85, 0.4 + silent_s / 7200) * (1.0 if after else 0.7)
        return AISAnomalyEvent(
            event_id=str(uuid.uuid4()),
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            anomaly_type="gap",
            mmsi=mmsi, vessel_name=seen.get("name", ""),
            position={"lat": lat, "lon": lon},
            confidence=round(conf, 3),
            evidence=evidence,
        )

    def process_position(
        self,
        mmsi: str,
        name: str,
        lat: float,
        lon: float,
        speed: float,
        vessel_type: str,
        *,
        observed_at=None,
    ) -> None:
        from datetime import datetime, timezone

        received_ts = time.time()
        if observed_at is None:
            observed_ts = received_ts
        elif isinstance(observed_at, datetime):
            observed = observed_at
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            observed_ts = observed.timestamp()
        else:
            try:
                observed_ts = float(observed_at)
            except (TypeError, ValueError):
                observed_ts = received_ts

        prev = self._last_seen.get(mmsi)
        pos = {"lat": lat, "lon": lon}

        # AIS providers can replay or reorder fixes after reconnects. A fix that
        # is not newer in source time must never create a teleport/spoofing cue.
        if prev and observed_ts <= prev["ts"]:
            return

        # Impossible speed between two source-timed fixes (AIS spoofing / MMSI
        # reuse candidate). Silence "gap" is handled from receipt freshness by
        # the sweep loop; movement integrity uses source chronology.
        if prev:
            gap_s = observed_ts - prev["ts"]
            dist_nm = _haversine_nm(prev["lat"], prev["lon"], lat, lon)
            if gap_s > 0 and dist_nm > 0:
                actual_speed_kts = dist_nm / (gap_s / 3600)
                max_spd = _MAX_SPEED.get(vessel_type, _MAX_SPEED["default"])
                if actual_speed_kts > max_spd and actual_speed_kts > 55:
                    self._emit(AISAnomalyEvent(
                        event_id=str(uuid.uuid4()),
                        timestamp_utc=datetime.now(timezone.utc).isoformat(),
                        anomaly_type="impossible_speed",
                        mmsi=mmsi, vessel_name=name,
                        position=pos,
                        confidence=min(0.9, 0.5 + (actual_speed_kts - max_spd) / 100),
                        evidence={
                            "computed_kts": round(actual_speed_kts, 1),
                            "max_allowed": max_spd,
                            "gap_s": round(gap_s),
                            "timing_basis": "ais_source_time",
                        },
                    ))

        # Dark zone entry
        if self._in_dark_zone(lat, lon) and (not prev or not self._in_dark_zone(prev["lat"], prev["lon"])):
            self._emit(AISAnomalyEvent(
                event_id=str(uuid.uuid4()),
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                anomaly_type="dark_zone_entry",
                mmsi=mmsi, vessel_name=name,
                position=pos, confidence=0.65,
                evidence={"zone": "known_ais_blackout_area"},
            ))

        # OFAC SDN list match
        if mmsi in self._sdn_mmsi:
            self._emit(AISAnomalyEvent(
                event_id=str(uuid.uuid4()),
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                anomaly_type="sdn_match",
                mmsi=mmsi, vessel_name=name,
                position=pos, confidence=0.95,
                evidence={"list": "OFAC_SDN"},
            ))

        self._last_seen[mmsi] = {
            "lat": lat,
            "lon": lon,
            "ts": observed_ts,
            "received_ts": received_ts,
            "speed": speed,
            "type": vessel_type,
            "name": name or self._last_seen.get(mmsi, {}).get("name", ""),
        }
        self._positions[mmsi] = pos

    def _in_dark_zone(self, lat: float, lon: float) -> bool:
        return any(lat0 <= lat <= lat1 and lon0 <= lon <= lon1
                   for lat0, lon0, lat1, lon1 in _DARK_ZONES)

    _EMIT_COOLDOWN_S = 3 * 3600

    @staticmethod
    def _grid_cell(lat: float, lon: float) -> str:
        return f"{round(lat)}:{round(lon)}"

    def _emit(self, event: AISAnomalyEvent) -> None:
        # A coverage outage is regional, not per-vessel: collapse every silent
        # vessel in the same ~1° cell onto one cooldown key so a feed-wide
        # reception drop never becomes N vessel events (audit invariant).
        if event.anomaly_type == "coverage_gap":
            key = ("coverage_gap", self._grid_cell(
                event.position.get("lat", 0.0), event.position.get("lon", 0.0)))
        else:
            key = (event.mmsi, event.anomaly_type)
        now = time.time()
        if now - self._emitted.get(key, 0.0) < self._EMIT_COOLDOWN_S:
            return
        self._emitted[key] = now
        logger.warning("AIS anomaly: %s mmsi=%s conf=%.2f", event.anomaly_type, event.mmsi, event.confidence)
        if self._on_anomaly:
            self._on_anomaly(event)
        self._to_intel_event(event)
        try:
            import redis  # type: ignore[import]
            r = redis.from_url(os.environ.get("REDIS_URL", _cfg.REDIS_URL))
            r.publish("ais:anomalies", event.model_dump_json())
        except Exception:
            pass

    @staticmethod
    def _to_intel_event(event: AISAnomalyEvent) -> None:
        """An AIS anomaly is an operator-only analysis signal -- never a
        public distress. Persist it so it shows in the intel feed."""
        try:
            from core.intel.store import IntelEvent, intel_store

            label = event.anomaly_type.replace("_", " ")
            is_coverage = event.anomaly_type == "coverage_gap"
            # A reception outage is regional context, not a per-vessel dark
            # signal -- one event per ~1° cell, and never "grey zone".
            if is_coverage:
                cell = AISAnomalyDetector._grid_cell(
                    event.position.get("lat", 0.0), event.position.get("lon", 0.0))
                ev_id = f"aisanom:coverage:{cell}"
                title = "AIS coverage outage (reception gap in the area)"
                text = (
                    f"AIS reception around {event.position.get('lat'):.2f}, "
                    f"{event.position.get('lon'):.2f} dropped for nearby traffic, not "
                    "just one vessel. This is a source/coverage outage, not intentional "
                    "dark activity."
                )
                domain = "safety"
                severity = "low"
                reason = (
                    "Nearby AIS traffic went silent at the same time -- classified as a "
                    "reception outage, not a vessel-specific gap."
                )
            else:
                ev_id = f"aisanom:{event.mmsi}:{event.anomaly_type}"
                title = f"AIS anomaly: {label}" + (
                    f" — {event.vessel_name}" if event.vessel_name else "")
                text = f"MMSI {event.mmsi}: {label} (confidence {event.confidence:.0%})."
                domain = "sanctions" if event.anomaly_type == "sdn_match" else "grey_zone"
                severity = "high" if event.anomaly_type == "sdn_match" else "medium"
                reason = (
                    f"AIS-derived {label}; confidence {event.confidence:.0%}. "
                    "This is an indicator, not proof of intent."
                )
            from core.mda.offshore_context import build_offshore_context, qualify_offshore_anomaly
            offshore_context = build_offshore_context(
                float(event.position.get("lat") or 0.0),
                float(event.position.get("lon") or 0.0),
            )
            offshore_metadata = {
                "anomaly_confidence": event.confidence,
                "anomaly_evidence": event.evidence,
                "silent_seconds": (event.evidence or {}).get("silent_seconds"),
                "gap_reason": event.evidence if event.anomaly_type in {"gap", "long_gap"} else None,
            }
            offshore_qualification = qualify_offshore_anomaly(
                event.anomaly_type, offshore_metadata, offshore_context
            ) if not is_coverage else {"qualified": False, "reason_codes": [], "rationale": None}
            canonical_live_integrity = (
                bool(offshore_qualification.get("qualified"))
                and event.anomaly_type in {"impossible_speed", "position_jump", "teleport"}
            )
            intel_store.add(
                IntelEvent(
                    id=ev_id,
                    type="ais_anomaly",
                    severity=severity,
                    lat=event.position.get("lat"),
                    lon=event.position.get("lon"),
                    title=title,
                    text=text,
                    url=(
                        ""
                        if is_coverage
                        else f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{event.mmsi}"
                    ),
                    source="ais",
                    linked_mmsi=None if is_coverage else event.mmsi,
                    timestamp_utc=event.timestamp_utc,
                    metadata={
                        "source_policy": "official_api",
                        "transport": "ais_stream",
                        "verification_status": "ais_transponder",
                        "is_distress": False,
                        "publication_status": "published" if canonical_live_integrity else "internal",
                        "analysis_state": "evidence_candidate" if offshore_qualification.get("qualified") else "anomaly",
                        "report_kind": "coverage_outage" if is_coverage else "ais_anomaly",
                        "coordinate_source": "ais_position",
                        "anomaly_type": event.anomaly_type,
                        "maritime_domain": domain,
                        "anomaly_confidence": event.confidence,
                        "anomaly_evidence": event.evidence,
                        "offshore_context": offshore_context,
                        "offshore_anomaly_qualified": bool(offshore_qualification.get("qualified")),
                        "offshore_reason_codes": list(offshore_qualification.get("reason_codes") or ()),
                        "offshore_rationale": offshore_qualification.get("rationale"),
                        "vessel_name": event.vessel_name or None,
                        "detection_reason": reason,
                    },
                ),
                dedup_key=ev_id,
            )
        except Exception:
            logger.debug("AIS anomaly -> intel event failed", exc_info=True)

    def _load_sdn(self) -> None:
        if _SDN_CACHE.exists():
            try:
                self._sdn_mmsi = set(json.loads(_SDN_CACHE.read_text()))
                return
            except Exception:
                pass
        # Download OFAC SDN — parse for vessel MMSI references
        try:
            url = "https://www.treasury.gov/ofac/downloads/sdn.json"
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
            mmsis: set[str] = set()
            for entry in data.get("sdnList", {}).get("sdnEntry", []):
                for prop in entry.get("idList", {}).get("id", []):
                    if "MMSI" in prop.get("idType", "").upper():
                        mmsis.add(str(prop.get("idNumber", "")).strip())
            _SDN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _SDN_CACHE.write_text(json.dumps(list(mmsis)))
            self._sdn_mmsi = mmsis
        except Exception:
            pass


if __name__ == "__main__":
    events: list[AISAnomalyEvent] = []
    det = AISAnomalyDetector(mock=True, on_anomaly=events.append)
    # Inject impossible-speed scenario
    det._last_seen["247012345"] = {"lat": 35.0, "lon": 14.0, "ts": time.time() - 60, "speed": 10, "type": "cargo"}
    det.process_position("247012345", "TestVessel", 36.0, 15.0, 300, "cargo")
    print(f"AISAnomalyDetector self-test OK: {len(events)} events")
    if events:
        print(f"  {events[0].anomaly_type}  evidence={events[0].evidence}")
