# SPDX-License-Identifier: AGPL-3.0-or-later
"""Periodic dark-vessel / grey-zone scans over the AIS track store.

One background loop (default every MDA_SCAN_INTERVAL_S) runs the scans that need
a *history* rather than a single message:

  * rendezvous / ship-to-ship (STS)  -> `ais_rendezvous` IntelEvent
  * loitering near critical infrastructure (cable / pipeline / platform)
    -> `ais_anomaly` (anomaly_type = infra_proximity), domain grey_zone
  * deliberate AIS gap (jamming-aware)  -> `ais_anomaly` (anomaly_type = gap)

Per-message integrity checks (spoof patterns, identity screening) stay in
`core/anomaly/ais.py`. Everything emitted here flows through `intel_store` into
the existing fusion engine, which raises the correlated alert + case.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config import config
from core.geo import haversine_km
from core.intel.store import IntelEvent, intel_store

logger = logging.getLogger(__name__)

_MED_BLACK_SEA = (-8.0, 28.0, 45.0, 48.0)  # min_lon, min_lat, max_lon, max_lat

# docs/fixes.md M14.1: minimum number of other nearby vessels reporting
# *before* a gap starts before we treat their before/after ratio as real
# evidence of a coverage outage. Below this we have no corroborating data
# either way, so a gap is judged on its own (matches the pre-M14.1
# behaviour for an isolated vessel with no neighbours in range at all)
# rather than defaulting to "no witnesses" == "outage".
_MIN_COVERAGE_WITNESSES = 2


def _nm(km: float) -> float:
    return km / 1.852


def _latest_behavioural_baseline(mmsi: str):
    from core.mda.behavioural_baseline import latest_baseline

    return latest_baseline(mmsi)


def _recent_track_for_behaviour(mmsi: str) -> list[dict[str, Any]]:
    from core.vessels.track_store import track_store

    since = datetime.now(timezone.utc) - timedelta(hours=6)
    return track_store.track(mmsi, since=since, limit=240)


def _assess_behaviour(track: list[dict[str, Any]], baseline):
    from core.mda.behaviour_assessment import assess_behaviour

    return assess_behaviour(track, baseline)


def _behaviour_context_for(mmsi: str) -> dict[str, Any]:
    try:
        assessment = _assess_behaviour(
            _recent_track_for_behaviour(mmsi), _latest_behavioural_baseline(mmsi)
        )
        return {
            "status": assessment.status,
            "reason_codes": list(assessment.reason_codes),
            "baseline_id": assessment.baseline_id,
            "method_version": assessment.method_version,
            "dimensions": assessment.dimensions,
            "caveats": list(assessment.caveats),
        }
    except Exception as exc:
        logger.debug("behaviour context unavailable for %s: %s", mmsi, exc)
        return {
            "status": "unavailable", "reason_codes": [],
            "baseline_id": None, "method_version": None,
            "dimensions": {}, "caveats": ["behavioural context unavailable"],
        }


def _nearby_gap_witness_counts(
    track_store: Any, mmsi: str, lat: float, lon: float,
    gap_start: datetime, now: datetime,
    *, window_min: float = 60.0, radius_nm: float = 25.0,
) -> tuple[int, int]:
    """Distinct other vessels reporting near (lat, lon) in the window just
    before the gap started vs during the gap itself -- the
    nearby_vessels_reporting_before/after core.mda.gap_reason.build_gap_reason
    needs to tell a vessel-specific gap from a shared reception outage."""
    from core.mda.coverage import _bbox_for_radius

    bbox = _bbox_for_radius(lat, lon, radius_nm)
    before_rows = track_store.positions_between(
        gap_start - timedelta(minutes=window_min), gap_start, bbox=bbox)
    after_rows = track_store.positions_between(gap_start, now, bbox=bbox)
    before = {r["mmsi"] for r in before_rows if r.get("mmsi") and r["mmsi"] != mmsi}
    after = {r["mmsi"] for r in after_rows if r.get("mmsi") and r["mmsi"] != mmsi}
    return len(before), len(after)


class MdaWatch:
    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # (mmsi_a, mmsi_b) -> {first_seen, last_seen, mid, count} for sustained-rendezvous
        self._pairs: dict[tuple[str, str], dict[str, Any]] = {}
        self._emitted: dict[str, float] = {}  # dedup key -> unix time

    def start(self) -> None:
        if self._running or not getattr(config, "MDA_WATCH_ENABLED", True):
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mda-watch")
        self._thread.start()
        logger.info("MdaWatch started (interval=%ss)", getattr(config, "MDA_SCAN_INTERVAL_S", 300))

    def stop(self) -> None:
        self._running = False

    # ── loop ─────────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        time.sleep(60)  # let the track store fill after boot
        while self._running:
            try:
                self.scan()
            except Exception as exc:  # pragma: no cover
                logger.warning("MdaWatch scan error: %s", exc)
            time.sleep(int(getattr(config, "MDA_SCAN_INTERVAL_S", 300)))

    def scan(self) -> dict[str, int]:
        """Run the canonical MDA stages independently.

        One malformed detector result must not prevent later stages, especially
        hypothesis evaluation, from seeing all other fresh evidence in the same
        cycle. Failures remain explicit in logs/metrics and contribute zero to
        that stage's count; no fallback or parallel analysis path is created.
        """
        from core.observability import record_mda_scan_stage

        stages = (
            ("rendezvous", self.scan_rendezvous),
            ("infra_loiter", self.scan_infra_loiter),
            ("gap", self.scan_gaps),
            ("identity", self.scan_identity),
            ("sanctioned_port_call", self.scan_sanctioned_port_calls),
            ("mmsi_duplicate", self.scan_mmsi_duplicate),
            ("spoofing", self.scan_spoofing),
            # Hypothesis evaluation deliberately remains last so it sees every
            # detector result that succeeded in this cycle.
            ("hypotheses", self.scan_hypotheses),
        )
        counts: dict[str, int] = {}
        for stage, runner in stages:
            try:
                counts[stage] = int(runner())
                record_mda_scan_stage(stage=stage, outcome="success")
            except Exception as exc:
                counts[stage] = 0
                record_mda_scan_stage(stage=stage, outcome="error")
                logger.exception("MdaWatch stage %s failed: %s", stage, exc)

        # prune emit-dedup + stale pairs even when one stage failed
        now = time.time()
        self._emitted = {k: t for k, t in self._emitted.items() if now - t < 24 * 3600}
        self._pairs = {k: v for k, v in self._pairs.items() if now - v["last_seen"] < 2 * 3600}
        return counts

    # ── investigation hypotheses ────────────────────────────────────────────

    @staticmethod
    def _durable_hypothesis_family_events(*, max_age_days: int = 2, limit_per_family: int = 750):
        """Bounded, fair durable sample for hypothesis evaluation.

        A single recency-sorted AIS-anomaly query lets chatty spoof/circle
        detectors crowd dark-gap evidence out of the hypothesis scan. Read the
        same IntelEvent table in small semantic quotas instead. This is not a
        second pipeline; it is only fair candidate selection for the existing
        canonical hypothesis engine.
        """
        from datetime import datetime, timedelta, timezone
        from core.db.models import IntelEventDB
        from core.db.session import session_scope
        from core.intel.store import IntelEvent

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        families = (
            ("ais_anomaly", ("gap", "long_gap")),
            ("ais_anomaly", ("position_jump", "circle_spoof", "static_spoof")),
            ("ais_rendezvous", ("ais_rendezvous", "rendezvous", "sts")),
            ("vessel_identity", ("mmsi_duplicate", "identity_anomaly", "sdn_match", "sanctioned_vessel")),
            ("dark_candidate", ("dark_candidate",)),
        )
        by_id = {}
        with session_scope() as db:
            from core.db.models import VesselTrackDB
            from core.intel.lifecycle import parse_utc

            anomaly = IntelEventDB.meta["anomaly_type"].as_string()
            for event_type, anomaly_types in families:
                query = db.query(IntelEventDB).filter(
                    IntelEventDB.timestamp_utc >= cutoff,
                    IntelEventDB.type == event_type,
                    anomaly.in_(anomaly_types),
                )
                if event_type == "ais_anomaly" and "gap" in anomaly_types:
                    silent = IntelEventDB.meta["silent_seconds"].as_float()
                    query = query.order_by(silent.desc().nullslast(), IntelEventDB.timestamp_utc.desc())
                else:
                    query = query.order_by(IntelEventDB.timestamp_utc.desc())
                rows = query.limit(limit_per_family).all()

                now = datetime.now(timezone.utc)
                for row in rows:
                    metadata = dict(row.meta or {})
                    if event_type == "ais_anomaly" and metadata.get("anomaly_type") in {"gap", "long_gap"}:
                        stored_silent = float(metadata.get("silent_seconds") or 0.0)
                        emitted_at = parse_utc(row.timestamp_utc)
                        gap_reason = metadata.get("gap_reason") or {}
                        worth_lookup = (
                            isinstance(gap_reason, dict)
                            and gap_reason.get("hypothesis") == "vessel_gap"
                            and float(gap_reason.get("confidence") or 0.0) >= 0.7
                            and int(gap_reason.get("nearby_vessels_reporting_before") or 0) >= 5
                            and int(gap_reason.get("nearby_vessels_reporting_after") or 0) >= 5
                            and float(metadata.get("jamming_score") or 0.0) < 0.3
                        )
                        latest = None
                        if worth_lookup and row.linked_mmsi:
                            latest = (
                                db.query(VesselTrackDB.ts)
                                .filter(VesselTrackDB.mmsi == str(row.linked_mmsi))
                                .order_by(VesselTrackDB.ts.desc())
                                .limit(1)
                                .scalar()
                            )
                        if emitted_at is not None and latest is not None and stored_silent > 0:
                            original_last = emitted_at - timedelta(seconds=stored_silent)
                            latest_utc = latest if latest.tzinfo is not None else latest.replace(tzinfo=timezone.utc)
                            latest_utc = latest_utc.astimezone(timezone.utc)
                            still_open = latest_utc <= original_last + timedelta(minutes=5)
                            metadata["gap_still_open"] = bool(still_open)
                            metadata["current_silent_seconds"] = (
                                max(0.0, (now - latest_utc).total_seconds())
                                if still_open else 0.0
                            )
                        else:
                            metadata["gap_still_open"] = False
                            metadata["current_silent_seconds"] = 0.0
                    by_id[row.id] = IntelEvent(
                        id=row.id, timestamp_utc=row.timestamp_utc, type=row.type or "",
                        severity=row.severity or "", lat=row.lat, lon=row.lon,
                        title=row.title or "", text=row.text or "", url=row.url or "",
                        source=row.source or "", linked_mmsi=row.linked_mmsi or "",
                        metadata=metadata,
                    )
        return list(by_id.values())

    @staticmethod
    def _retrospective_darkship_candidates(
        *,
        limit: int = 6,
        min_age_hours: float = 72.0,
        max_age_days: int = 10,
        recheck_hours: float = 24.0,
    ) -> list[dict[str, Any]]:
        """Select a bounded set of old, high-quality offshore AIS gaps.

        This is only candidate selection for the existing hypothesis engine.
        It does not create a second event stream and never publishes anything.
        """
        from core.db.models import IntelEventDB, VesselTrackDB
        from core.db.session import session_scope
        from core.intel.lifecycle import parse_utc
        from core.mda.offshore_context import (
            build_offshore_context,
            qualify_offshore_anomaly,
        )

        now = datetime.now(timezone.utc)
        oldest = (now - timedelta(days=max_age_days)).isoformat()
        newest = (now - timedelta(hours=min_age_hours)).isoformat()
        candidates: list[dict[str, Any]] = []
        with session_scope() as db:
            anomaly = IntelEventDB.meta["anomaly_type"].as_string()
            rows = (
                db.query(IntelEventDB)
                .filter(
                    IntelEventDB.type == "ais_anomaly",
                    anomaly.in_(("gap", "long_gap")),
                    IntelEventDB.timestamp_utc >= oldest,
                    IntelEventDB.timestamp_utc <= newest,
                )
                .order_by(IntelEventDB.timestamp_utc.desc())
                .limit(max(1000, limit * 200))
                .all()
            )
            for row in rows:
                if row.lat is None or row.lon is None or not row.linked_mmsi:
                    continue
                metadata = dict(row.meta or {})
                cue = metadata.get("darkship_cue") or {}
                if (
                    isinstance(cue, dict)
                    and cue.get("association_status") == "unmatched_candidate"
                    and cue.get("gfw_unmatched_in_area")
                ):
                    continue

                last_checked = parse_utc(
                    str(metadata.get("darkship_cue_refreshed_at") or "")
                )
                if (
                    last_checked is not None
                    and (now - last_checked).total_seconds() < recheck_hours * 3600
                ):
                    continue

                gap_reason = metadata.get("gap_reason") or {}
                silent_seconds = float(metadata.get("silent_seconds") or 0.0)
                if (
                    not isinstance(gap_reason, dict)
                    or gap_reason.get("hypothesis") != "vessel_gap"
                    or float(gap_reason.get("confidence") or 0.0) < 0.7
                    or int(gap_reason.get("nearby_vessels_reporting_before") or 0) < 5
                    or int(gap_reason.get("nearby_vessels_reporting_after") or 0) < 5
                    or not (4 * 3600 <= silent_seconds <= 12 * 3600)
                    or float(metadata.get("jamming_score") or 0.0) >= 0.3
                ):
                    continue

                context = build_offshore_context(float(row.lat), float(row.lon))
                qualification = qualify_offshore_anomaly(
                    str(metadata.get("anomaly_type") or "gap"),
                    metadata,
                    context,
                )
                if not qualification.get("qualified"):
                    continue

                emitted_at = parse_utc(row.timestamp_utc)
                if emitted_at is None:
                    continue
                gap_start = emitted_at - timedelta(seconds=silent_seconds)
                pre = (
                    db.query(VesselTrackDB)
                    .filter(
                        VesselTrackDB.mmsi == str(row.linked_mmsi),
                        VesselTrackDB.ts >= gap_start - timedelta(hours=2),
                        VesselTrackDB.ts <= gap_start + timedelta(minutes=5),
                    )
                    .order_by(VesselTrackDB.ts.desc())
                    .first()
                )
                if pre is None:
                    continue
                speed_kn = float(pre.sog or metadata.get("pre_gap_speed_kn") or 0.0)
                if speed_kn < 2.0:
                    continue
                course_deg = (
                    float(pre.cog)
                    if pre.cog is not None
                    else float(pre.heading)
                    if pre.heading is not None
                    else None
                )
                candidates.append({
                    "event_id": row.id,
                    "mmsi": str(row.linked_mmsi),
                    "lat": float(pre.lat),
                    "lon": float(pre.lon),
                    "speed_kn": speed_kn,
                    "course_deg": course_deg,
                    "gap_start": gap_start,
                    "search_hours": silent_seconds / 3600.0,
                    "last_checked": last_checked,
                    "offshore_context": context,
                })

        candidates.sort(key=lambda item: (
            item["last_checked"] is not None,
            item["last_checked"] or item["gap_start"],
        ))
        return candidates[:max(0, int(limit))]

    @staticmethod
    def _persist_darkship_cue_refresh(
        candidate: dict[str, Any],
        cue: dict[str, Any],
        *,
        checked_at: datetime,
    ) -> Optional[IntelEvent]:
        """Update the same deterministic AIS-gap row without rebroadcasting it."""
        from core.db.models import IntelEventDB
        from core.db.session import session_scope

        with session_scope() as db:
            row = db.get(IntelEventDB, candidate["event_id"])
            if row is None:
                return None
            metadata = dict(row.meta or {})
            metadata["darkship_cue"] = cue
            metadata["darkship_cue_refreshed_at"] = checked_at.isoformat()
            metadata["darkship_cue_refresh_source"] = "gfw_4wings"
            metadata["darkship_cue_refresh_attempts"] = (
                int(metadata.get("darkship_cue_refresh_attempts") or 0) + 1
            )
            if cue.get("gfw_unmatched_in_area"):
                metadata["analysis_state"] = "evidence_candidate"
            row.meta = metadata
            db.flush()
            return IntelEvent(
                id=row.id,
                timestamp_utc=row.timestamp_utc,
                type=row.type or "",
                severity=row.severity or "",
                lat=row.lat,
                lon=row.lon,
                title=row.title or "",
                text=row.text or "",
                url=row.url or "",
                source=row.source or "",
                linked_mmsi=row.linked_mmsi or "",
                metadata=metadata,
            )

    def refresh_darkship_cues(
        self,
        *,
        limit: int = 6,
        min_age_hours: float = 72.0,
        max_age_days: int = 10,
        recheck_hours: float = 24.0,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Revisit delayed GFW SAR evidence for existing offshore AIS gaps."""
        from core.mda.darkship_cue import build as build_darkship_cue

        candidates = self._retrospective_darkship_candidates(
            limit=limit,
            min_age_hours=min_age_hours,
            max_age_days=max_age_days,
            recheck_hours=recheck_hours,
        )
        report: dict[str, Any] = {
            "scanned": len(candidates),
            "refreshed": 0,
            "with_unmatched_sar": 0,
            "hypotheses_evaluated": 0,
            "details": [],
        }
        refreshed_events: list[IntelEvent] = []
        for candidate in candidates:
            cue = build_darkship_cue(
                lat=candidate["lat"],
                lon=candidate["lon"],
                course_deg=candidate["course_deg"],
                speed_kn=candidate["speed_kn"],
                gap_start=candidate["gap_start"],
                max_search_hours=candidate["search_hours"],
                include_s1=False,
            )
            unmatched_count = len(cue.get("gfw_unmatched_in_area") or ())
            report["details"].append({
                "event_id": candidate["event_id"],
                "association_status": cue.get("association_status"),
                "unmatched_sar": unmatched_count,
                "search_window_hours": cue.get("search_window_hours"),
            })
            if unmatched_count:
                report["with_unmatched_sar"] += 1
            if dry_run:
                continue
            updated = self._persist_darkship_cue_refresh(
                candidate,
                cue,
                checked_at=datetime.now(timezone.utc),
            )
            if updated is not None:
                refreshed_events.append(updated)
                report["refreshed"] += 1

        if refreshed_events and not dry_run:
            report["hypotheses_evaluated"] = self.scan_hypotheses(
                extra_events=refreshed_events
            )
        return report

    def scan_hypotheses(
        self, *, extra_events: Optional[list[IntelEvent]] = None,
    ) -> int:
        from core.intel.hypothesis_engine import (
            event_to_episode_input_feature,
            evaluate_episode,
        )
        from core.intel.store import intel_store
        from core.live.vessel_episodes import coalesce_security_vessel_episodes

        # The bounded in-memory deque is dominated by high-volume MDA/AIS
        # churn. Independent corroborators such as GFW can therefore vanish
        # before the hypothesis pass sees them. Merge a bounded durable window
        # with memory so evidence from different lineages can meet without
        # turning the scan into an unbounded historical replay.
        by_id = {
            event.id: event
            for event in self._durable_hypothesis_family_events(max_age_days=2, limit_per_family=750)
        }
        for event in intel_store.persisted_events(
            source_in=["GFW", "VIIRS VBD"], max_age_days=7, limit=1000,
        ):
            by_id[event.id] = event
        for event in intel_store.events(limit=1200, max_age_days=7):
            by_id[event.id] = event
        for event in extra_events or ():
            by_id[event.id] = event

        features = []
        for event in by_id.values():
            feature = event_to_episode_input_feature(event)
            if feature is not None:
                features.append(feature)
        episodes = coalesce_security_vessel_episodes(features)
        return sum(1 for episode in episodes if evaluate_episode(episode) is not None)

    # ── rendezvous / STS ─────────────────────────────────────────────────────

    def scan_rendezvous(self) -> int:
        from core.mda.reference import reference
        from core.vessels.track_store import track_store

        window_min = float(getattr(config, "MDA_RENDEZVOUS_WINDOW_MIN", 30))
        max_sep_m = float(getattr(config, "MDA_RENDEZVOUS_MAX_SEP_M", 600))
        max_sog = float(getattr(config, "MDA_RENDEZVOUS_MAX_SOG_KN", 2.0))
        min_dur_min = float(getattr(config, "MDA_RENDEZVOUS_MIN_DURATION_MIN", 30))

        now = datetime.now(timezone.utc)
        rows = track_store.positions_between(now - timedelta(minutes=window_min), now,
                                             bbox=_MED_BLACK_SEA)
        # latest slow position per MMSI
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            sog = r.get("sog") or 0.0
            if sog > max_sog + 1.0:
                continue
            prev = latest.get(r["mmsi"])
            if prev is None or r["ts"] > prev["ts"]:
                latest[r["mmsi"]] = r
        slow = [r for r in latest.values() if (r.get("sog") or 0.0) <= max_sog]
        slow.sort(key=lambda r: r["lon"])   # cheap sweep on longitude

        emitted = 0
        seen_now: set[tuple[str, str]] = set()
        for i, a in enumerate(slow):
            for b in slow[i + 1:]:
                if (b["lon"] - a["lon"]) * 96_000 > max_sep_m * 3:  # ~deg->m at 45N, early out
                    break
                sep_m = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) * 1000
                if sep_m > max_sep_m:
                    continue
                mid_lat, mid_lon = (a["lat"] + b["lat"]) / 2, (a["lon"] + b["lon"]) / 2
                if reference.in_port_or_anchorage(mid_lat, mid_lon):
                    continue
                _port, port_km = reference.nearest_port_km(mid_lat, mid_lon)
                if port_km < 5.0:
                    continue
                # Anchored/moored neighbours are an anchorage observation,
                # not evidence of a ship-to-ship transfer.
                if a.get("nav_status") in {1, 5} or b.get("nav_status") in {1, 5}:
                    continue
                key = tuple(sorted((a["mmsi"], b["mmsi"])))
                seen_now.add(key)
                now_epoch = time.time()
                continuity_grace_s = max(
                    900.0,
                    float(getattr(config, "MDA_SCAN_INTERVAL_S", 300)) * 2.5,
                )
                pair = self._pairs.get(key)
                if pair is None or now_epoch - float(pair.get("last_seen") or 0.0) > continuity_grace_s:
                    pair = {"first_seen": now_epoch, "count": 0}
                    self._pairs[key] = pair
                pair["last_seen"] = now_epoch
                pair["count"] += 1
                pair["mid"] = (mid_lat, mid_lon)
                dur_min = (time.time() - pair["first_seen"]) / 60.0
                if dur_min >= min_dur_min and not self._recently_emitted(f"sts:{key[0]}:{key[1]}", 6 * 3600):
                    self._emit_rendezvous(key, mid_lat, mid_lon, dur_min)
                    emitted += 1
        return emitted

    def _emit_rendezvous(self, key: tuple[str, str], lat: float, lon: float, dur_min: float) -> None:
        from core.mda.reference import reference
        from core.vessels.registry import registry

        info = []
        tanker = False
        for mmsi in key:
            v = registry._cache.get(mmsi, {}) if hasattr(registry, "_cache") else {}
            st = v.get("ship_type")
            if isinstance(st, int) and 80 <= st <= 89:
                tanker = True
            info.append({"mmsi": mmsi, "name": v.get("ship_name") or mmsi,
                         "ship_type": st, "flag": v.get("flag")})
        zone = reference.in_sts_zone(lat, lon)
        dark = self._either_had_gap(key)
        from core.mda.offshore_context import build_offshore_context, qualify_offshore_anomaly
        offshore_context = build_offshore_context(lat, lon)
        rendezvous_meta = {
            "duration_min": round(dur_min, 1),
            "sts_zone": zone,
            "tanker": tanker,
            "dark": dark,
        }
        offshore_qualification = qualify_offshore_anomaly("ais_rendezvous", rendezvous_meta, offshore_context)
        severity = "high" if (tanker or zone or dark or offshore_qualification["qualified"]) else "medium"
        title = "STS rendezvous"
        if tanker:
            title = "Tanker STS rendezvous"
        if dark:
            title = "Dark " + title[0].lower() + title[1:]
        intel_store.add(IntelEvent(
            id=f"sts:{key[0]}:{key[1]}",
            type="ais_rendezvous",
            severity=severity,
            lat=round(lat, 5), lon=round(lon, 5),
            title=f"{title} — {info[0]['name']} / {info[1]['name']}",
            text=(f"MMSI {key[0]} and {key[1]} co-located within a few hundred metres, "
                  f"both near-stationary, for ~{int(dur_min)} min offshore"
                  + (f" in the {zone} STS zone" if zone else "") + "."),
            source="mda",
            linked_mmsi=key[0],
            metadata={
                "anomaly_type": "ais_rendezvous",
                # docs/fixes.md M0.3: a raw rendezvous observation is not a
                # sanctions event -- two vessels in sustained proximity is
                # the entire evidence at this point, regardless of tanker/
                # dark-party/zone flags (those raise severity, not the
                # legal-allegation-shaped domain). Was "sanctions"
                # unconditionally. core.intel.fusion._rule_dark_sts (the
                # correlation layer) already computes its own, properly
                # evidence-gated domain/case_type when it later corroborates
                # this with an independent sanctions/identity signal --
                # this raw observation must not pre-empt that with its own
                # unconditional allegation-shaped tag.
                "maritime_domain": "grey_zone",
                "service": "maritime",
                "lane": "intelligence",
                "observation_type": "rendezvous",
                "is_distress": False,
                "publication_status": "published" if offshore_qualification["qualified"] else "internal",
                "analysis_state": "evidence_candidate" if offshore_qualification["qualified"] else "anomaly",
                "source_policy": "official_api",
                "verification_status": "ais_transponder",
                "coordinate_source": "ais_position",
                "vessels": info, "duration_min": round(dur_min, 1),
                "sts_zone": zone, "tanker": tanker, "dark": dark,
                "offshore_context": offshore_context,
                "offshore_anomaly_qualified": offshore_qualification["qualified"],
                "offshore_reason_codes": offshore_qualification["reason_codes"],
                "offshore_rationale": offshore_qualification["rationale"],
            },
        ), dedup_key=f"sts:{key[0]}:{key[1]}:{int(time.time() // 21600)}")
        logger.warning("MDA: STS rendezvous %s <-> %s (%dmin, tanker=%s dark=%s zone=%s)",
                       key[0], key[1], int(dur_min), tanker, dark, zone)

    def _either_had_gap(self, key: tuple[str, str]) -> bool:
        for ev in intel_store.events(limit=400):
            if (ev.type == "ais_anomaly" and ev.linked_mmsi in key
                    and (ev.metadata.get("anomaly_type") in {"gap", "long_gap"})):
                return True
        return False

    # ── infrastructure loitering ─────────────────────────────────────────────

    def scan_infra_loiter(self) -> int:
        from core.mda.reference import reference
        from core.vessels.track_store import track_store

        buf_km = float(getattr(config, "MDA_INFRA_BUFFER_KM", 3.0))
        max_sog = float(getattr(config, "MDA_INFRA_LOITER_MAX_SOG_KN", 3.0))
        min_dur_min = float(getattr(config, "MDA_INFRA_LOITER_MIN_MIN", 45))

        now = datetime.now(timezone.utc)
        rows = track_store.positions_between(now - timedelta(minutes=min_dur_min * 1.5), now,
                                             bbox=_MED_BLACK_SEA)
        by_mmsi: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_mmsi.setdefault(r["mmsi"], []).append(r)

        from core.vessels.registry import registry
        cache = getattr(registry, "_cache", {}) or {}
        emitted = 0
        for mmsi, track in by_mmsi.items():
            track.sort(key=lambda r: r["ts"])
            slow = [r for r in track if (r.get("sog") or 0.0) <= max_sog]
            if len(slow) < 4:
                continue
            span_min = (_parse(slow[-1]["ts"]) - _parse(slow[0]["ts"])).total_seconds() / 60.0
            if span_min < min_dur_min:
                continue
            # must actually dwell, not just transit slowly through the buffer
            lats = [r["lat"] for r in slow]
            lons = [r["lon"] for r in slow]
            dwell_km = haversine_km(min(lats), min(lons), max(lats), max(lons))
            if dwell_km > 5.0:
                continue
            v = cache.get(mmsi, {})
            st = v.get("ship_type")
            if isinstance(st, int) and 30 <= st <= 32:   # fishing vessels work slowly everywhere
                continue
            if st == 52:   # tugs work slowly near port infrastructure by design
                continue
            mid = slow[len(slow) // 2]
            hit = reference.nearest_infrastructure(mid["lat"], mid["lon"], max_km=buf_km)
            if hit is None or hit.kind not in ("cable", "pipeline", "sts_zone"):
                continue
            # A vessel idling in a bunkering / STS anchorage is not itself
            # unusual -- that is what the zone is for. It becomes worth
            # surfacing when the vessel loitering there is a confirmed
            # sanctions match: a classic evasion pattern (refuel/transfer at
            # a grey-zone hub instead of a port call), cross-referenced here
            # even without a second vessel for scan_rendezvous to pair it
            # with. cable/pipeline proximity stays unconditional -- that is
            # infrastructure-safety context regardless of who the vessel is.
            sanctioned = False
            if hit.kind == "sts_zone":
                from core.mda.identity import screen
                result = screen(mmsi=mmsi, imo=v.get("imo"),
                                name=v.get("ship_name") or "", flag=v.get("flag") or "")
                sanctioned = bool(result.get("sanctions"))
                if not sanctioned:
                    continue
            if self._recently_emitted(f"infra:{mmsi}:{hit.name}", 24 * 3600):
                continue
            intel_store.add(IntelEvent(
                id=f"infraloiter:{mmsi}",
                type="ais_anomaly",
                severity="high",
                lat=round(mid["lat"], 5), lon=round(mid["lon"], 5),
                title=(f"Sanctioned vessel loitering in {hit.name} — {v.get('ship_name') or mmsi}"
                       if sanctioned else f"Loitering near {hit.name} — {v.get('ship_name') or mmsi}"),
                text=(f"MMSI {mmsi} at <{max_sog:.0f} kn for ~{int(span_min)} min within "
                      f"{hit.distance_km:.1f} km of {hit.name} ({hit.kind})."
                      + (" Vessel is a confirmed sanctions match." if sanctioned else "")),
                url=f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
                source="SeaCommons AIS analysis", linked_mmsi=mmsi,
                metadata={
                    "anomaly_type": (
                        "sanctions_bunkering_loiter" if sanctioned
                        else "cable_proximity" if hit.kind == "cable" else "loiter"
                    ),
                    "maritime_domain": "sanctions" if sanctioned else "grey_zone",
                    "is_distress": False,
                    "publication_status": "internal", "source_policy": "official_api",
                    "verification_status": "ais_transponder", "coordinate_source": "ais_position",
                    "infrastructure": {"kind": hit.kind, "name": hit.name, "distance_km": hit.distance_km},
                    "loiter_minutes": round(span_min, 1),
                    "sanctions_matched": sanctioned,
                    "behaviour_context": _behaviour_context_for(mmsi),
                    "detection_reason": (
                        f"AIS dwell: {len(slow)} slow fixes over {int(span_min)} minutes, "
                        f"within {hit.distance_km:.1f} km of {hit.name}; proximity is "
                        "anomaly context, not evidence of interference."
                    ),
                },
            ), dedup_key=f"infraloiter:{mmsi}:{int(time.time() // 43200)}")
            logger.warning("MDA: %s loitering near %s (%s, %.1fkm, %dmin)",
                           mmsi, hit.name, hit.kind, hit.distance_km, int(span_min))
            emitted += 1
        return emitted

    # ── deliberate AIS gap (jamming-aware) ───────────────────────────────────

    def scan_gaps(self) -> int:
        from core.intel import confidence as confidence_mod
        from core.mda.coverage import compute_coverage_baseline
        from core.mda.gap_reason import build_gap_reason
        from core.mda.jamming import jamming
        from core.mda.reference import reference
        from core.vessels.registry import registry
        from core.vessels.track_store import track_store

        min_gap = float(getattr(config, "MDA_GAP_MIN_S", 3600))
        candidates = track_store.silent_since(min_silent_s=min_gap, min_speed_kn=2.0)
        cache = getattr(registry, "_cache", {}) or {}
        emitted = 0
        now_dt = datetime.now(timezone.utc)
        for mmsi, last in candidates:
            if self._recently_emitted(f"gap:{mmsi}", 6 * 3600):
                continue
            # docs/fixes.md M14.1: vessel type is context only from here on --
            # it is carried into the emitted event's metadata but never gates
            # whether a gap is reported. What decides that is
            # core.mda.gap_reason.build_gap_reason (docs/fixes.md M4.3),
            # which classifies this vessel's silence against how many OTHER
            # nearby vessels kept reporting through the same window: a
            # common/port-wide outage silences its neighbours too and is
            # rejected as "coverage_gap"; a vessel actually going dark while
            # its neighbours keep reporting normally is a "vessel_gap" and
            # gets reported regardless of ship type.
            v = cache.get(mmsi, {})
            ship_type = v.get("ship_type")
            silent_s = time.time() - last.ts
            gap_start = datetime.fromtimestamp(last.ts, tz=timezone.utc)
            nearby_before, nearby_after = _nearby_gap_witness_counts(
                track_store, mmsi, last.lat, last.lon, gap_start, now_dt)
            gap_reason = None
            if nearby_before >= _MIN_COVERAGE_WITNESSES:
                coverage = compute_coverage_baseline(mmsi, last.lat, last.lon, at=gap_start)
                gap_reason = build_gap_reason(
                    gap_duration_s=silent_s,
                    nearby_vessels_reporting_before=nearby_before,
                    nearby_vessels_reporting_after=nearby_after,
                    coverage=coverage,
                    pre_gap_speed=last.sog,
                    pre_gap_course=_last_course(track_store, mmsi),
                )
                if gap_reason.hypothesis == "coverage_gap":
                    continue
            provider_coverage = None
            fusion_mode = str(getattr(config, "AIS_FUSION_MODE", "legacy") or "legacy").lower()
            if getattr(config, "AIS_FUSION_ENABLED", False) or fusion_mode == "fused":
                from core.vessels.ais_coverage import coverage_state
                provider_coverage = coverage_state.assess(
                    nearby_traffic_seen=nearby_after > 0, now=now_dt
                )
                if not provider_coverage.gap_eligible:
                    continue
            jam = jamming.in_jamming_zone(last.lat, last.lon)
            port_or_anchorage = reference.in_port_or_anchorage(last.lat, last.lon)
            pre_gap_course = _last_course(track_store, mmsi)
            cue = None
            # Cross-sensor collection follows the local investigation gate.
            # Do not spend network/SAR queries on every one-hour AIS silence.
            gap_cross_cue_ready = (
                gap_reason is not None
                and gap_reason.hypothesis == "vessel_gap"
                and gap_reason.confidence >= 0.7
                and nearby_before >= 5 and nearby_after >= 5
                and 4 * 3600 <= silent_s <= 12 * 3600
                and last.sog >= 2.0
                and jam < 0.3
                and not port_or_anchorage
            )
            if gap_cross_cue_ready:
                try:
                    from core.mda.darkship_cue import build as _cue
                    course = pre_gap_course
                    cue = _cue(lat=last.lat, lon=last.lon, course_deg=course,
                               speed_kn=last.sog,
                               gap_start=datetime.fromtimestamp(last.ts, tz=timezone.utc))
                except Exception as exc:  # pragma: no cover
                    logger.debug("darkship_cue failed: %s", exc)
            confidence = round(max(0.2, min(0.9, 0.4 + (time.time() - last.ts) / 14400) - 0.5 * jam), 3)
            severity = "high" if confidence >= 0.7 else "medium"
            # Shadow-mode confidence model (docs/prompt.md phase 9/11): a
            # second, traceable score computed alongside the inline formula
            # above. Stored, not cut over -- severity/publication behaviour
            # here is still driven entirely by `confidence`/`severity` above,
            # unchanged. Lets the two be compared before anything switches.
            gap_rule = "ais_gap_long" if silent_s > 6 * 3600 else "ais_gap"
            confidence_v2 = confidence_mod.combine(
                gap_rule,
                rule_strength=confidence_mod.rule_strength(gap_rule),
                source_reliability=confidence_mod.source_reliability("official_api"),
                observation_freshness=confidence_mod.observation_freshness(silent_s),
                coverage_quality=confidence_mod.coverage_quality(jam),
                location_precision=confidence_mod.location_precision_score("ais_position"),
            )
            behaviour_context = _behaviour_context_for(mmsi)
            from core.mda.offshore_context import build_offshore_context, qualify_offshore_anomaly
            anomaly_type = "long_gap" if (time.time() - last.ts) > 6 * 3600 else "gap"
            gap_meta = {
                "silent_seconds": int(time.time() - last.ts),
                "jamming_score": jam,
                "gap_reason": (
                    {
                        "hypothesis": gap_reason.hypothesis,
                        "confidence": gap_reason.confidence,
                        "coverage_ratio": gap_reason.coverage_ratio,
                        "nearby_vessels_reporting_before": nearby_before,
                        "nearby_vessels_reporting_after": nearby_after,
                    }
                    if gap_reason is not None else None
                ),
                "behaviour_context": behaviour_context,
            }
            offshore_context = build_offshore_context(last.lat, last.lon)
            offshore_qualification = qualify_offshore_anomaly(anomaly_type, gap_meta, offshore_context)
            intel_store.add(IntelEvent(
                id=f"aisgap:{mmsi}",
                type="ais_anomaly",
                severity=severity,
                lat=round(last.lat, 5), lon=round(last.lon, 5),
                title=f"AIS gap — {last.name or mmsi}",
                text=(f"MMSI {mmsi} last heard underway ({last.sog:.0f} kn) "
                      f"{int((time.time() - last.ts) / 60)} min ago, then silent."
                      + (" Inside an active GNSS-jamming zone (likely reception loss)." if jam > 0.3 else "")),
                source="mda", linked_mmsi=mmsi,
                metadata={
                    "anomaly_type": anomaly_type,
                    # A reporting gap is a traffic anomaly.  It becomes
                    # sanctions context only when identity screening finds a
                    # real list match on this vessel.
                    "maritime_domain": "grey_zone",
                    "is_distress": False,
                    "publication_status": "published" if offshore_qualification["qualified"] else "internal",
                    "analysis_state": "evidence_candidate" if offshore_qualification["qualified"] else "anomaly",
                    "source_policy": "official_api", "verification_status": "ais_transponder",
                    "coordinate_source": "ais_position",
                    "silent_seconds": int(time.time() - last.ts),
                    "jamming_score": jam, "anomaly_confidence": confidence,
                    "offshore_context": offshore_context,
                    "offshore_anomaly_qualified": offshore_qualification["qualified"],
                    "offshore_reason_codes": offshore_qualification["reason_codes"],
                    "offshore_rationale": offshore_qualification["rationale"],
                    "port_or_anchorage": port_or_anchorage,
                    "pre_gap_speed_kn": last.sog,
                    "pre_gap_course_deg": pre_gap_course,
                    "confidence_v2": confidence_v2.as_metadata(),
                    # docs/fixes.md M14.1: vessel class is context only here,
                    # never a detection gate.
                    "vessel_type_context": ship_type,
                    "provider_coverage": (
                        {
                            "status": provider_coverage.status,
                            "confidence": provider_coverage.confidence,
                            "reason_codes": list(provider_coverage.reason_codes),
                            "active_upstreams": sorted(provider_coverage.active_upstreams),
                            "degraded_upstreams": sorted(provider_coverage.degraded_upstreams),
                        }
                        if provider_coverage is not None else None
                    ),
                    "gap_reason": (
                        {
                            "hypothesis": gap_reason.hypothesis,
                            "confidence": gap_reason.confidence,
                            "coverage_ratio": gap_reason.coverage_ratio,
                            "nearby_vessels_reporting_before": nearby_before,
                            "nearby_vessels_reporting_after": nearby_after,
                        }
                        if gap_reason is not None else None
                    ),
                    "darkship_cue": cue,
                    "behaviour_context": _behaviour_context_for(mmsi),
                },
            ), dedup_key=f"aisgap:{mmsi}:{int(time.time() // 21600)}")
            emitted += 1
        return emitted

    # ── identity screening ──────────────────────────────────────────────────

    @staticmethod
    def _identity_fingerprint(result: dict[str, Any]) -> list:
        sanctions = sorted(
            (s.get("list", ""), s.get("program", "")) for s in (result.get("sanctions") or [])
        )
        return [sorted(result.get("risk_flags") or []), sanctions]

    def _identity_status_changed(self, mmsi: str, result: dict[str, Any]) -> bool:
        """True on first sighting of this MMSI's flagged status, or if it
        changed since the last emitted vessel_identity event for it (new
        sanctions program, escalation from a weak flag to sanctions_hit,
        etc). id=f"vesselid:{mmsi}" is deterministic -- one row per vessel,
        looked up directly rather than re-scanning the event stream."""
        fingerprint = self._identity_fingerprint(result)
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope

            with session_scope() as db:
                row = db.query(IntelEventDB).filter(
                    IntelEventDB.id == f"vesselid:{mmsi}"
                ).first()
                if row is None:
                    return True
                previous = (row.meta or {}).get("identity_fingerprint")
                return previous != fingerprint
        except Exception as exc:  # pragma: no cover - fail open, same as before this change
            logger.debug("identity fingerprint lookup failed for %s: %s", mmsi, exc)
            return True

    def scan_identity(self) -> int:
        from core.mda.identity import screen
        from core.vessels.registry import registry
        from core.vessels.track_store import track_store

        cache = getattr(registry, "_cache", {}) or {}
        now = datetime.now(timezone.utc)
        rows = track_store.positions_between(
            now - timedelta(hours=6), now, bbox=_MED_BLACK_SEA, limit=100_000)
        # track_store._last is an in-memory, per-process cache of live AIS
        # messages — empty right after a restart even though these rows (from
        # the DB) prove the vessel has a recent position. Take the latest fix
        # per MMSI from the rows we already fetched instead of a second,
        # unreliable lookup — every match previously shipped with
        # lat/lon = None and simply couldn't be plotted.
        last_pos: dict[str, tuple[float, float]] = {}
        for r in rows:  # ordered ts ascending -- later rows overwrite, so this ends up latest-wins
            last_pos[r["mmsi"]] = (r["lat"], r["lon"])
        emitted = 0
        for mmsi in list(last_pos)[:2000]:
            v = cache.get(mmsi, {})
            result = screen(mmsi=mmsi, imo=v.get("imo"), name=v.get("ship_name") or "",
                            flag=v.get("flag") or "")
            serious = {"sanctions_hit"} & set(result["risk_flags"])
            weak = {f for f in result["risk_flags"] if f.startswith("mmsi_")} | \
                   ({"imo_checksum_fail"} & set(result["risk_flags"]))
            if not serious and len(weak) < 1:
                continue
            if self._recently_emitted(f"ident:{mmsi}", 24 * 3600):
                continue
            # Sanctions/identity status is a persistent property of the
            # vessel, not a recurring event -- re-alerting every 24h for as
            # long as a known-sanctioned vessel keeps transiting the Med is
            # exactly the "map noise" docs/prompt.md warns about. Only emit
            # when this is either the first sighting or the flagged status
            # actually changed (new list, new program, escalation from a
            # weak flag to a real sanctions hit).
            if not self._identity_status_changed(mmsi, result):
                continue
            lat, lon = last_pos[mmsi]
            intel_store.add(IntelEvent(
                id=f"vesselid:{mmsi}",
                type="vessel_identity",
                severity="high" if serious else "medium",
                lat=lat, lon=lon,
                title=(f"Sanctioned vessel: {v.get('ship_name') or mmsi}" if serious
                       else f"Identity anomaly: {v.get('ship_name') or mmsi}"),
                text=(f"MMSI {mmsi} — flags: {', '.join(result['risk_flags'])}."
                      + (f" Sanctions: {result['sanctions'][0]['list']} "
                         f"({result['sanctions'][0].get('program', '')})." if serious else "")),
                source="mda", linked_mmsi=mmsi,
                metadata={
                    "anomaly_type": "sdn_match" if serious else "identity_anomaly",
                    "maritime_domain": "sanctions", "is_distress": False,
                    "publication_status": "internal", "source_policy": "official_api",
                    "verification_status": "derived", "coordinate_source": "ais_position",
                    "identity": result,
                    "identity_fingerprint": self._identity_fingerprint(result),
                },
            ), dedup_key=f"vesselid:{mmsi}:{int(time.time() // 86400)}")
            emitted += 1
        return emitted

    def scan_sanctioned_port_calls(self) -> int:
        """Emit a factual Maritime compliance signal only when an exact
        IMO/MMSI sanctions-list match meets a qualified AIS-derived port call.
        Name-only matches and fast geofence transits remain internal.
        """
        import hashlib

        from core.api.routes.mda import _derive_recent_port_calls
        from core.mda.identity import screen
        from core.mda.reference import reference
        from core.vessels.registry import registry
        from core.vessels.track_store import track_store

        now = datetime.now(timezone.utc)
        rows = track_store.positions_between(
            now - timedelta(hours=48), now, bbox=_MED_BLACK_SEA, limit=150_000
        )
        by_mmsi: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            mmsi = str(row.get("mmsi") or "")
            if mmsi:
                by_mmsi.setdefault(mmsi, []).append(row)

        cache = getattr(registry, "_cache", {}) or {}
        emitted = 0
        for mmsi, track in by_mmsi.items():
            vessel = cache.get(mmsi, {}) or {}
            identity = screen(
                mmsi=mmsi,
                imo=vessel.get("imo"),
                name=vessel.get("ship_name") or "",
                flag=vessel.get("flag") or "",
            )
            strong_hits = [
                hit for hit in (identity.get("sanctions") or [])
                if set(hit.get("matched_on") or ()) & {"imo", "mmsi"}
            ]
            if not strong_hits:
                continue
            track = sorted(track, key=lambda point: str(point.get("ts") or ""))
            calls = _derive_recent_port_calls(track, limit=2)
            if not calls:
                continue
            call = calls[0]
            arrived = str(call.get("arrived_at") or "")
            port = str(call.get("port") or "").strip()
            last_seen = str(call.get("last_seen_at") or "")
            if not arrived or not port or int(call.get("ais_fixes") or 0) < 2:
                continue
            from core.intel.lifecycle import parse_utc

            arrived_dt = parse_utc(arrived)
            activity_dt = parse_utc(str(call.get("departed_at") or last_seen or arrived))
            if arrived_dt is None or activity_dt is None:
                continue
            if now - activity_dt > timedelta(hours=24):
                continue
            stable = hashlib.blake2s(
                f"{mmsi}|{port}|{arrived_dt.date().isoformat()}".encode(),
                digest_size=8,
            ).hexdigest()
            dedup = f"sanction-port:{stable}"
            if self._recently_emitted(dedup, 48 * 3600):
                continue

            # Keep the signal at an observed AIS fix inside this port,
            # never at a later post-departure fix and never at a port centroid.
            port_points = []
            for candidate in track:
                try:
                    candidate_port = reference.in_port_or_anchorage(
                        float(candidate["lat"]), float(candidate["lon"])
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if candidate_port == port:
                    port_points.append(candidate)
            if not port_points:
                continue
            point = port_points[-1]
            hit = strong_hits[0]
            vessel_name = vessel.get("ship_name") or mmsi
            intel_store.add(
                IntelEvent(
                    id=f"sanport:{stable}",
                    type="vessel_identity",
                    severity="high",
                    lat=float(point["lat"]),
                    lon=float(point["lon"]),
                    title=f"Sanctioned vessel port call — {vessel_name} · {port}",
                    text=(
                        f"AIS-derived qualified port call at {port}. Vessel identity "
                        f"matches {hit.get('list') or 'sanctions list'} on "
                        f"{', '.join(hit.get('matched_on') or ())}. This is an observed "
                        "identity + movement fact, not an allegation of sanctions evasion."
                    ),
                    source="SeaCommons MDA",
                    linked_mmsi=mmsi,
                    timestamp_utc=activity_dt.isoformat(),
                    metadata={
                        "anomaly_type": "sanctioned_port_call",
                        "episode_family": "port_call_episode",
                        "maritime_domain": "sanctions",
                        "is_distress": False,
                        "publication_status": "published",
                        "source_policy": "official_api",
                        "verification_status": "multi_source_corroborated",
                        "coordinate_source": "ais_position",
                        "sanctions_matched": True,
                        "sanctions": strong_hits,
                        "port_call": call,
                        "identity": identity,
                        "contributing_sources": [
                            "ais", str(hit.get("list") or "sanctions_list")
                        ],
                        "contributing_independence_groups": [
                            "ais_sensor_lineage",
                            f"sanctions_list:{hit.get('list') or 'unknown'}",
                        ],
                        "alternative_explanations": [],
                        "detection_reason": (
                            "Exact IMO/MMSI sanctions-list match plus qualified AIS "
                            "port stay; fast geofence transit and name-only matching "
                            "are excluded."
                        ),
                    },
                ),
                dedup_key=dedup,
            )
            emitted += 1
        return emitted

    def scan_mmsi_duplicate(self) -> int:
        """Same MMSI transmitting from two places far apart in the same window —
        the classic clone / borrowed-identity signature."""
        from core.vessels.track_store import track_store

        now = datetime.now(timezone.utc)
        rows = track_store.positions_between(now - timedelta(minutes=40), now, bbox=_MED_BLACK_SEA)
        by_mmsi: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_mmsi.setdefault(r["mmsi"], []).append(r)
        emitted = 0
        for mmsi, pts in by_mmsi.items():
            if len(pts) < 4:
                continue
            far = 0
            base = pts[0]
            for p in pts[1:]:
                if haversine_km(base["lat"], base["lon"], p["lat"], p["lon"]) > 100:
                    far += 1
            if far < 2:
                continue
            if self._recently_emitted(f"dup:{mmsi}", 12 * 3600):
                continue
            intel_store.add(IntelEvent(
                id=f"mmsidup:{mmsi}",
                type="vessel_identity",
                severity="high",
                lat=round(base["lat"], 5), lon=round(base["lon"], 5),
                title=f"Duplicate MMSI {mmsi} — two positions >100 km apart",
                text=(f"MMSI {mmsi} broadcast from two widely separated positions within "
                      f"40 min — clone / borrowed identity or a spoofed track."),
                source="mda", linked_mmsi=mmsi,
                metadata={
                    "anomaly_type": "mmsi_duplicate", "maritime_domain": "sanctions",
                    "is_distress": False, "publication_status": "internal",
                    "source_policy": "official_api", "verification_status": "derived",
                    "coordinate_source": "ais_position",
                },
            ), dedup_key=f"mmsidup:{mmsi}:{int(time.time() // 43200)}")
            emitted += 1
        return emitted

    # ── spoofing patterns ───────────────────────────────────────────────────

    def scan_spoofing(self) -> int:
        from core.intel import confidence as confidence_mod
        from core.intel.ais_integrity_replay import classify_impossible_speed
        from core.mda.jamming import jamming
        from core.mda.reference import reference
        from core.vessels.registry import registry
        from core.vessels.track_store import track_store

        now = datetime.now(timezone.utc)
        rows = track_store.positions_between(now - timedelta(minutes=90), now, bbox=_MED_BLACK_SEA)
        by_mmsi: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_mmsi.setdefault(r["mmsi"], []).append(r)
        cache = getattr(registry, "_cache", {}) or {}
        emitted = 0
        for mmsi, pts in by_mmsi.items():
            if len(pts) < 6:
                continue
            pts.sort(key=lambda r: r["ts"])
            pts = self._clean_spoof_points(pts)
            if len(pts) < 6:
                continue
            reason, extra = self._spoof_signature(pts)
            if reason is None:
                continue

            # A single impossible leg is not enough to publish a spoofing
            # episode. AIS providers can emit one-frame coordinate glitches
            # that jump hundreds of miles and immediately return. Require the
            # following fixes to remain clustered around the new location.
            teleport_pattern_pre = None
            if reason == "teleport":
                teleport_pattern_pre = self._teleport_pattern(pts)
                if teleport_pattern_pre != "sustained_relocation":
                    continue

            if reason in ("frozen", "circular"):
                # A pleasure/sailing craft (ship_type 36/37) swinging on its
                # anchor near a marina produces exactly this signature --
                # near-static or a small drift circle. Real, not spoofed.
                # docs/fixes.md M14.1 scoped the vessel-class-exclusion
                # removal to scan_gaps() (the exit gate it was tested
                # against); these frozen/circular exemptions stay for now --
                # removing them needs a coverage-based replacement signal of
                # their own, tracked as separate follow-up work, not a
                # same-PR removal that would just reintroduce the exact
                # false positives (Genoa tug traffic, marina-anchored
                # yachts) these were added for.
                v = cache.get(mmsi, {})
                ship_type = v.get("ship_type")
                if isinstance(ship_type, int) and ship_type in (36, 37):
                    from core.mda.identity import screen
                    result = screen(
                        mmsi=mmsi, imo=v.get("imo"),
                        name=v.get("ship_name") or "", flag=v.get("flag") or "",
                    )
                    if not result.get("sanctions"):
                        continue
                # AIS ship_type 40-49 = high-speed craft (including many
                # passenger ferries); 60-69 = passenger ships. Repeated terminal
                # approaches / turnarounds can draw a clean ring in sampled AIS.
                # Ship class is a false-positive control for this low-specificity
                # signature only; it never suppresses impossible-speed teleports.
                elif (
                    isinstance(ship_type, int)
                    and (40 <= ship_type <= 49 or 60 <= ship_type <= 69)
                ):
                    continue
                # AIS ship_type 30-32 = fishing vessel. A trawler working a
                # ground draws exactly the "circular" signature -- repeated
                # tight loops/passes are how trawling works, not spoofing.
                elif isinstance(ship_type, int) and 30 <= ship_type <= 32:
                    from core.mda.identity import screen
                    result = screen(
                        mmsi=mmsi, imo=v.get("imo"),
                        name=v.get("ship_name") or "", flag=v.get("flag") or "",
                    )
                    if not result.get("sanctions"):
                        continue
                # AIS ship_type 52 = tug. Repeated short manoeuvres assisting
                # ships in/out of port, then idling near the breakwater
                # between jobs, draws the same near-static/tight-ring
                # signature -- observed live in Genoa traffic.
                elif ship_type == 52:
                    from core.mda.identity import screen
                    result = screen(
                        mmsi=mmsi, imo=v.get("imo"),
                        name=v.get("ship_name") or "", flag=v.get("flag") or "",
                    )
                    if not result.get("sanctions"):
                        continue
            if self._recently_emitted(f"spoof:{mmsi}", 6 * 3600):
                continue
            mid = pts[len(pts) // 2]
            jam = jamming.in_jamming_zone(mid["lat"], mid["lon"])
            atype = "position_jump" if reason == "teleport" else (
                "circular_pattern" if reason == "circular" else "static_position_inconsistency")
            # docs/fixes.md M14.1: cross-check the teleport signature against
            # core.intel.ais_integrity_replay.classify_impossible_speed --
            # informational only (this detector's own gating above is
            # unchanged), vessel type is passed through but never used to
            # gate the classification either (see that module's docstring).
            integrity_classification = None
            teleport_pattern = None
            coincident_teleport_peers: tuple[str, ...] = ()
            teleport_near_port = None
            if reason == "teleport":
                teleport_pattern = teleport_pattern_pre
                trigger = self._teleport_trigger(pts)
                coincident_teleport_peers = self._coincident_teleport_peers(by_mmsi, mmsi)
                if trigger is not None:
                    teleport_near_port = reference.in_port_or_anchorage(*trigger["to"])
                kn, dt_s = self._teleport_metrics(pts)
                if kn is not None:
                    v = cache.get(mmsi, {})
                    label, conf = classify_impossible_speed(
                        implied_speed_kn=kn,
                        vessel_type=str(v.get("ship_type") or "unknown"),
                        time_delta_s=dt_s,
                    )
                    integrity_classification = {"label": label, "confidence": conf}
            # Shadow-mode confidence model (docs/prompt.md phase 9/11) --
            # this detector had no confidence value at all before, only
            # severity from jamming alone. Stored alongside severity, not
            # driving it yet.
            spoof_rule = {"teleport": "spoof_teleport", "circular": "spoof_circular",
                          "frozen": "spoof_frozen"}.get(reason, "spoof_frozen")
            duration_s = max(0.0, (_parse(pts[-1]["ts"]) - _parse(pts[0]["ts"])).total_seconds())
            confidence_v2 = confidence_mod.combine(
                spoof_rule,
                rule_strength=confidence_mod.rule_strength(spoof_rule),
                source_reliability=confidence_mod.source_reliability("derived"),
                persistence=confidence_mod.persistence(len(pts), duration_s),
                coverage_quality=confidence_mod.coverage_quality(jam),
                location_precision=confidence_mod.location_precision_score("ais_position"),
            )
            label = {
                "teleport": "AIS impossible-movement candidate",
                "circular": "AIS circular track pattern",
                "frozen": "AIS static-position inconsistency",
            }.get(reason, "AIS position-integrity cue")
            intel_store.add(IntelEvent(
                id=f"spoof:{mmsi}:{reason}",
                type="ais_anomaly",
                severity="high" if reason == "teleport" and jam < 0.4 else "medium",
                lat=round(mid["lat"], 5), lon=round(mid["lon"], 5),
                title=f"{label} — {mmsi}",
                text=(
                    f"MMSI {mmsi}: derived AIS integrity cue ({extra}). "
                    "This single AIS lineage does not by itself establish spoofing."
                    + (" GNSS interference is reported in the area." if jam > 0.3 else "")
                ),
                source="mda", linked_mmsi=mmsi,
                metadata={
                    "anomaly_type": atype, "maritime_domain": "grey_zone",
                    "is_distress": False, "publication_status": "internal",
                    "source_policy": "official_api", "verification_status": "derived",
                    "coordinate_source": "ais_position", "spoof_reason": reason,
                    "evidence_stage": "derived", "source_lineage": "ais_sensor_lineage",
                    "independent_source_count": 1,
                    "detection_claim": "position_integrity_cue",
                    "jamming_score": jam, "detail": extra,
                    "confidence_v2": confidence_v2.as_metadata(),
                    "ais_integrity_classification": integrity_classification,
                    "teleport_pattern": teleport_pattern,
                    "coincident_teleport_peers": list(coincident_teleport_peers),
                    "teleport_near_port": teleport_near_port,
                },
            ), dedup_key=f"spoof:{mmsi}:{reason}:{int(time.time() // 21600)}")
            emitted += 1
        return emitted

    @staticmethod
    def _teleport_trigger(pts: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        for index, (a, b) in enumerate(zip(pts, pts[1:])):
            dt = (_parse(b["ts"]) - _parse(a["ts"])).total_seconds()
            if dt <= 0:
                continue
            distance_km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            implied_kn = distance_km / 1.852 / (dt / 3600)
            if implied_kn > 60 and distance_km > 15:
                return {
                    "index": index,
                    "at": _parse(b["ts"]),
                    "from": (float(a["lat"]), float(a["lon"])),
                    "to": (float(b["lat"]), float(b["lon"])),
                    "distance_km": distance_km,
                    "implied_speed_kn": implied_kn,
                }
        return None

    @staticmethod
    def _coincident_teleport_peers(
        by_mmsi: dict[str, list[dict[str, Any]]], current_mmsi: str,
        *, max_time_s: float = 300.0, max_distance_km: float = 30.0,
    ) -> tuple[str, ...]:
        current = MdaWatch._teleport_trigger(by_mmsi.get(current_mmsi, []))
        if current is None:
            return ()
        peers: list[str] = []
        for mmsi, pts in by_mmsi.items():
            if mmsi == current_mmsi:
                continue
            if MdaWatch._teleport_pattern(pts) != "sustained_relocation":
                continue
            other = MdaWatch._teleport_trigger(pts)
            if other is None:
                continue
            if abs((other["at"] - current["at"]).total_seconds()) > max_time_s:
                continue
            if haversine_km(*current["to"], *other["to"]) <= max_distance_km:
                peers.append(mmsi)
        return tuple(sorted(set(peers)))

    @staticmethod
    def _teleport_pattern(pts: list[dict[str, Any]]) -> str:
        """Classify the first impossible jump by what happens immediately after.

        A fix that jumps away and returns close to the origin is a transient
        outlier. A jump followed by at least two nearby fixes at the new
        location is a sustained relocation worth opening as an investigation.
        Everything else remains unresolved evidence.
        """
        for index, (a, b) in enumerate(zip(pts, pts[1:])):
            dt = (_parse(b["ts"]) - _parse(a["ts"])).total_seconds()
            if dt <= 0:
                continue
            distance_km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            implied_kn = distance_km / 1.852 / (dt / 3600)
            if implied_kn <= 60 or distance_km <= 15:
                continue
            following = pts[index + 2:index + 5]
            if any(haversine_km(a["lat"], a["lon"], p["lat"], p["lon"]) <= 5 for p in following):
                return "transient_outlier"
            new_cluster = sum(
                1 for p in following
                if haversine_km(b["lat"], b["lon"], p["lat"], p["lon"]) <= 15
            )
            if new_cluster >= 2:
                return "sustained_relocation"
            return "unresolved_jump"
        return "not_applicable"

    @staticmethod
    def _teleport_metrics(pts: list[dict[str, Any]]) -> tuple[Optional[float], float]:
        """The implied speed (kn) and elapsed time (s) of the fix pair that
        triggered a 'teleport' _spoof_signature() verdict -- same pair, same
        threshold, so this always finds one when reason == 'teleport'."""
        for a, b in zip(pts, pts[1:]):
            dt = (_parse(b["ts"]) - _parse(a["ts"])).total_seconds()
            if dt <= 0:
                continue
            kn = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) / 1.852 / (dt / 3600)
            if kn > 60 and haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) > 15:
                return kn, dt
        return None, 0.0

    @staticmethod
    def _clean_spoof_points(pts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop obvious coordinate sentinels before kinematic inference.

        Exact longitude 0 is geographically valid, so it is removed only when
        the same track is otherwise clustered well away from Greenwich and the
        zero fix keeps essentially the same latitude. This catches receiver /
        decoder default-value artifacts without masking genuine meridian
        crossings.
        """
        nonzero_lons = [float(p["lon"]) for p in pts if abs(float(p.get("lon") or 0.0)) > 1e-6]
        if len(nonzero_lons) < 3:
            return pts
        ordered = sorted(nonzero_lons)
        median_lon = ordered[len(ordered) // 2]
        if abs(median_lon) <= 1.0:
            return pts
        nonzero_lats = [float(p["lat"]) for p in pts if abs(float(p.get("lon") or 0.0)) > 1e-6]
        median_lat = sorted(nonzero_lats)[len(nonzero_lats) // 2]
        return [
            p for p in pts
            if not (
                abs(float(p.get("lon") or 0.0)) <= 1e-6
                and abs(float(p.get("lat") or 0.0) - median_lat) <= 0.25
            )
        ]

    @staticmethod
    def _spoof_signature(pts: list[dict[str, Any]]) -> tuple[Optional[str], str]:
        # teleport: a single step implies an impossible speed
        for a, b in zip(pts, pts[1:]):
            dt = (_parse(b["ts"]) - _parse(a["ts"])).total_seconds()
            if dt <= 0:
                continue
            kn = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) / 1.852 / (dt / 3600)
            if kn > 60 and haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) > 15:
                return "teleport", f"{kn:.0f} kn between two fixes"
        # frozen: >80% of fixes identical position while SOG > 1
        first = (round(pts[0]["lat"], 4), round(pts[0]["lon"], 4))
        same = sum(1 for p in pts if (round(p["lat"], 4), round(p["lon"], 4)) == first)
        moving = sum(1 for p in pts if (p.get("sog") or 0) > 1.0)
        if same / len(pts) > 0.8 and moving > len(pts) * 0.5:
            return "frozen", f"{same}/{len(pts)} fixes identical while SOG>1"
        # circular: least-squares circle fit, small residual, plausible radius
        r_m, resid_ratio = _circle_fit([(p["lat"], p["lon"]) for p in pts])
        if r_m and 40 <= r_m <= 3000 and resid_ratio < 0.12:
            return "circular", f"ring radius ~{r_m:.0f} m"
        return None, ""

    # ── helpers ──────────────────────────────────────────────────────────────

    def _recently_emitted(self, key: str, cooldown_s: float) -> bool:
        now = time.time()
        if now - self._emitted.get(key, 0.0) < cooldown_s:
            return True
        self._emitted[key] = now
        return False


def _last_course(track_store: Any, mmsi: str) -> Optional[float]:
    from datetime import datetime as _dt
    pts = track_store.track(mmsi, since=_dt.now(timezone.utc) - timedelta(hours=3), limit=20)
    for p in reversed(pts):
        if p.get("cog") is not None:
            return float(p["cog"])
    if len(pts) >= 2:
        a, b = pts[-2], pts[-1]
        from core.geo import bearing_deg
        return bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])
    return None


def _circle_fit(latlon: list[tuple[float, float]]) -> tuple[Optional[float], float]:
    """Kasa least-squares circle fit in a local metre frame. Returns
    (radius_m, mean_residual / radius); radius None when degenerate.

    Model: x^2 + y^2 = a*x + b*y + c  ->  centre (a/2, b/2), r = sqrt(c + a^2/4 + b^2/4).
    """
    if len(latlon) < 5:
        return None, 1.0
    try:
        import numpy as np
    except Exception:  # pragma: no cover
        return None, 1.0
    lat0 = sum(p[0] for p in latlon) / len(latlon)
    lon0 = sum(p[1] for p in latlon) / len(latlon)
    mlat = 111_320.0
    mlon = 111_320.0 * math.cos(math.radians(lat0))
    x = np.array([(p[1] - lon0) * mlon for p in latlon])
    y = np.array([(p[0] - lat0) * mlat for p in latlon])
    A = np.column_stack([x, y, np.ones_like(x)])
    z = x * x + y * y
    try:
        a, b, c = np.linalg.lstsq(A, z, rcond=None)[0]
    except Exception:  # pragma: no cover
        return None, 1.0
    cx, cy = a / 2.0, b / 2.0
    inside = c + cx * cx + cy * cy
    if inside <= 0:
        return None, 1.0
    r = math.sqrt(inside)
    resid = float(np.mean(np.abs(np.hypot(x - cx, y - cy) - r)))
    return r, resid / r if r > 0 else 1.0


def _parse(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


mda_watch = MdaWatch()
