# SPDX-License-Identifier: AGPL-3.0-or-later
"""SourceObservation sampling for the live AIS feed (docs/fixes.md M1.2).

AISStream's PositionReport firehose fires on every fix -- many messages
per second across the tracked bbox. Recording a SourceObservation per fix
would flood the table without adding evidence value (a position report
5 seconds after the last one is not new information; core.vessels.
track_store already keeps the throttled trajectory history that actually
needs that density). This subscribes to the same shared hook every other
AIS consumer uses (core.vessels.aisstream.register_position_hook) and
records one only when something actually changed for that vessel:

  - its navigational status changed since the previous received fix
    (observation_type=ais_nav_status);
  - or it is reporting again after a true message silence of at least
    AIS_SOURCE_OBSERVATION_GAP_S since the previous received fix
    (observation_type=ais_gap -- the reappearance itself is the
    observation-worthy event, not every fix in between).

v0 scope: service=maritime/lane=safety is used for every sampled
observation here, not only nav_status in {2, 3, 6} (not_under_command /
restricted_manoeuvrability / aground). A raw navigational-status change is
closer in spirit to vessel operational/safety context than to Maritime
Intelligence or Environmental, the other two available lanes -- but this
is a coarse default, not a claim that every transition is safety-critical.
A later classification pass (once more of docs/fixes.md's evidence model
exists) can assign a more specific lane per nav_status value.

Best-effort and strictly additive: never touches core.vessels.registry
(latest position) or core.vessels.track_store (trajectory history); both
keep functioning exactly as before regardless of this module.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_GAP_S = 30 * 60  # 30 min silence before a reappearance is its own observation
_MAX_TRACKED_MMSI = 20_000  # bounded memory; oldest half pruned past this


class AISSourceObservationSampler:
    """Subscribes to the shared AIS position hook; samples on nav-status
    change or reporting-gap reappearance only, never on every fix."""

    def __init__(self) -> None:
        self._running = False
        # mmsi -> (last_nav_status, last_received_epoch).
        # The timestamp MUST advance on every fix, even when no raw
        # SourceObservation is persisted. Otherwise continuous AIS traffic is
        # falsely reclassified as a 30-minute reporting gap.
        self._last: dict[str, tuple[Optional[int], float]] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        from core.vessels import aisstream

        aisstream.register_position_hook(self.on_position)
        logger.info("AISSourceObservationSampler attached to the AIS feed")

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _gap_threshold_s() -> float:
        from core.config import config

        return float(getattr(config, "AIS_SOURCE_OBSERVATION_GAP_S", _DEFAULT_GAP_S))

    def on_position(
        self,
        mmsi: str,
        name: str,
        lat: float,
        lon: float,
        sog: Optional[float] = None,
        nav_status: Optional[int] = None,
        cog: Optional[float] = None,
        heading: Optional[float] = None,
        received_at: Optional[datetime] = None,
    ) -> None:
        if not mmsi or lat is None or lon is None:
            return
        now = time.time()
        with self._lock:
            prev = self._last.get(mmsi)
            status_changed = prev is not None and prev[0] != nav_status
            gap_reappearance = prev is not None and (now - prev[1]) >= self._gap_threshold_s()
            first_seen = prev is None

            # Always advance last-seen state before deciding whether this fix
            # is worth persisting. This is the critical distinction between
            # message silence and "nothing interesting happened".
            self._last[mmsi] = (nav_status, now)
            should_record = status_changed or gap_reappearance or first_seen
            if len(self._last) > _MAX_TRACKED_MMSI:
                # Same bounded-growth pattern as every other in-memory
                # dedup/state dict in this codebase (GDACS/news/twikit
                # monitors' _seen sets) -- evict the oldest half.
                oldest_first = sorted(self._last.items(), key=lambda item: item[1][1])
                self._last = dict(oldest_first[_MAX_TRACKED_MMSI // 2 :])
            if not should_record:
                return

        if first_seen:
            reason = "first_seen"
        elif gap_reappearance:
            reason = "gap_reappearance"
        else:
            reason = "status_change"
        self._record(mmsi, name, lat, lon, sog, nav_status, received_at, reason)

    def _record(
        self,
        mmsi: str,
        name: str,
        lat: float,
        lon: float,
        sog: Optional[float],
        nav_status: Optional[int],
        received_at: Optional[datetime],
        reason: str,
    ) -> None:
        try:
            from core.db.session import session_scope
            from core.intel.source_observation import record_observation

            observed_at = (received_at or datetime.now(timezone.utc)).isoformat()
            observation_type = "ais_gap" if reason == "gap_reappearance" else "ais_nav_status"
            with session_scope() as db:
                record_observation(
                    db,
                    service="maritime",
                    lane="safety",
                    observation_type=observation_type,
                    source_name="AISStream",
                    source_policy="official_api",
                    # A sampled state change has no natural external
                    # delivery id (unlike a tweet/RSS item) -- an
                    # epoch-nanosecond-suffixed id keeps each sampled
                    # event distinct from the previous one for the same
                    # MMSI without colliding, which is the correct
                    # identity for a derived/sampled fact rather than a
                    # raw redelivered message (AISStream itself never
                    # redelivers a message this hook already saw).
                    source_id=f"{mmsi}:{time.time_ns()}",
                    observed_at=observed_at,
                    raw_payload=(
                        f"mmsi={mmsi} name={name} nav_status={nav_status} "
                        f"sog={sog} reason={reason}"
                    ),
                    lat=lat,
                    lon=lon,
                    provenance={"reason": reason, "nav_status": nav_status},
                )
        except Exception as exc:
            logger.debug(
                "AISSourceObservationSampler: record skipped for %s: %s", mmsi, exc
            )


ais_source_observation_sampler = AISSourceObservationSampler()
