# SPDX-License-Identifier: AGPL-3.0-or-later
"""
In-memory event store for intel events.

Holds up to MAX_EVENTS events in a deque (newest first).
Deduplication via a content-hash set.
WebSocket clients are stored as (websocket, asyncio.loop) tuples so
broadcast() can be called from background threads.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

from core.domain.live_contracts import MaritimeDomain, VerificationStatus

logger = logging.getLogger(__name__)


def _normalised_source(value: str) -> str:
    """Stable identity for harmless source spelling variants."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

# Location-upgrade decisions live in core.intel.location_evidence
# (metadata_quality / location_quality): evidence quality -- review status,
# then source, then tighter uncertainty -- not source rank alone (F-04).

MAX_EVENTS = 600
DEDUP_WINDOW = 2000  # max unique hashes kept in memory

# Source+URL identifies an editorial item (article, post, bulletin), but it is
# not an event identity for machine telemetry. AIS findings for one vessel
# intentionally share its public details URL and must remain separate episodes.
_URL_DEDUP_TYPES = frozenset(
    {
        "distress",
        "twitter",
        "mastodon",
        "bluesky",
        "news",
        "ngo_activity",
        "gdacs",
        "iom_incident",
    }
)

# Machine-derived events intentionally use deterministic IDs so one physical
# episode can be enriched over time without forking a second marker.
_MACHINE_REFRESH_TYPES = frozenset(
    {
        "ais_anomaly",
        "ais_rendezvous",
        "vessel_identity",
        "vessel_incident",
        "dark_candidate",
        "correlated_alert",
        "ais_spike",
        "ngo_activity",
        "navwarning",
    }
)


@dataclass
class IntelEvent:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp_utc: str = field(default="")

    def __post_init__(self):
        if not self.timestamp_utc:
            self.timestamp_utc = datetime.now(timezone.utc).isoformat()
    # Event classification
    type: str = ""        # twitter | news | iom_incident | ais_spike | ngo_activity
    severity: str = ""   # critical | high | medium | low

    # Geography
    lat: Optional[float] = None
    lon: Optional[float] = None

    # Content
    title: str = ""
    text: str = ""
    url: str = ""
    source: str = ""      # e.g. "alarm__phone", "IOM", "MarineTraffic"
    author: str = ""

    # Cross-references
    linked_mmsi: str = ""   # if this event correlates to a known vessel

    # Free-form extras (incident counts, vessel names, etc.)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Operational classification ─────────────────────────────────────────────
    # Distress is the product's reason to exist: distress events are surfaced as
    # the top "operational" tier (pinned, blinking, drift-ready). Everything else
    # is context. Tiers (highest priority first):
    #   operational → a live distress / SAR call demanding action
    #   news        → reporting / incidents / situational updates
    #   signal      → low-salience telemetry (AIS loiter spikes, NGO movements)
    _OPERATIONAL_TYPES = frozenset({"distress", "iom_incident"})
    _NEWS_TYPES = frozenset({"news", "twitter", "mastodon", "manual", "gdacs", "bluesky"})
    _SIGNAL_TYPES = frozenset({"ais_spike", "ngo_activity", "ais_anomaly"})

    def tier(self) -> str:
        if self.type == "distress" or self.metadata.get("is_distress"):
            return "operational"
        if self.type == "iom_incident" and self.severity in ("critical", "high"):
            return "operational"
        if self.type in self._SIGNAL_TYPES:
            return "signal"
        if self.type in self._NEWS_TYPES or self.type == "iom_incident":
            return "news"
        return "news"

    def priority(self) -> int:
        """Lower = more urgent. Used for stable operational sorting."""
        tier_rank = {"operational": 0, "news": 1, "signal": 2}.get(self.tier(), 1)
        sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(self.severity, 2)
        return tier_rank * 10 + sev_rank

    # ── Maritime-domain compartment ───────────────────────────────────────────
    # Which maritime-awareness lane this event belongs to. ``sar`` (migrant and
    # general distress) is the primary lane and the default; the other
    # compartments are operator-only unless allow-listed in
    # PUBLIC_MARITIME_DOMAINS. An explicit metadata["maritime_domain"] always
    # wins; otherwise it is inferred from type / anomaly subtype so legacy
    # events resolve to ``sar``.
    _GREY_ZONE_ANOMALIES = frozenset(
        {
            "ais_rendezvous",
            "circle_spoof",
            "dark_zone_entry",
            "gap",
            "impossible_speed",
            "long_gap",
            "loiter",
            "position_jump",
            "static_spoof",
            "zone_incursion",
            "cable_proximity",
        }
    )
    _SANCTIONS_ANOMALIES = frozenset(
        {"sdn_match", "sanctioned_vessel"}
    )
    _DOMAIN_BY_TYPE = {
        "piracy_incident": "piracy",
        "gfw_event": "sanctions",
        "vessel_incident": "safety",
        "oil_spill": "environmental",
    }

    def is_vessel_mobility_incident(self) -> bool:
        """Recognise current and legacy NUC/disabled/adrift vessel events."""
        kind = str(
            self.metadata.get("ais_nav_status_kind")
            or self.metadata.get("anomaly_type")
            or ""
        ).lower()
        if kind in {"not_under_command", "disabled", "adrift"}:
            return True
        if self.type != "correlated_alert":
            return False
        evidence = " ".join(
            (
                str(self.title or ""),
                str(self.metadata.get("alert_type") or ""),
                json.dumps(self.metadata.get("contributing") or [], default=str),
                json.dumps(self.metadata.get("contributing_sources") or [], default=str),
            )
        ).lower()
        return any(
            marker in evidence
            for marker in (
                "unable to manoeuvre",
                "unable to maneuver",
                "not_under_command",
                "not under command",
                "disabled vessel",
                "vessel adrift",
            )
        )

    def maritime_domain(self) -> str:
        # An explicit, current maritime_domain always wins (docs/fixes.md
        # A-02 / P0.1): current producers set this directly -- in
        # particular vessel_incident_monitor.py writes "safety" for
        # not_under_command/aground/restricted_manoeuvrability (PR #62),
        # and fusion.py's correlated_alert events carry the domain their
        # rule computed. Only a record with NO explicit maritime_domain at
        # all (a genuinely older record, from before either of those wrote
        # one) falls through to the legacy is_vessel_mobility_incident()
        # correction below -- older fusion records labelled every vessel
        # casualty "safety", which was wrong for a NUC/disabled/adrift
        # subject; this reclassifies THOSE to grey_zone at projection time
        # without rewriting the DB. Checking is_vessel_mobility_incident()
        # first, as this used to, silently discarded every explicit value
        # PR #62 writes and reintroduced the exact bug it fixed.
        explicit = self.metadata.get("maritime_domain")
        if explicit:
            return explicit
        if self.is_vessel_mobility_incident():
            return MaritimeDomain.GREY_ZONE.value
        if self.type == "ais_anomaly":
            anomaly_type = self.metadata.get("anomaly_type", "")
            if anomaly_type in self._GREY_ZONE_ANOMALIES:
                return MaritimeDomain.GREY_ZONE.value
            if anomaly_type in self._SANCTIONS_ANOMALIES:
                return MaritimeDomain.SANCTIONS.value
            # An unknown AIS-derived behaviour is maritime-security context,
            # not evidence that the vessel is sanctioned.
            return MaritimeDomain.GREY_ZONE.value
        return self._DOMAIN_BY_TYPE.get(self.type, MaritimeDomain.SAR.value)

    def verification_status(self) -> str:
        """
        Evidence-first labelling (nextstep.txt): never present an asserted public
        report as confirmed fact. Manual operator entries are treated as
        operator-asserted; everything ingested from public sources is unverified.
        """
        explicit = self.metadata.get("verification_status")
        if explicit:
            return explicit
        if self.type == "ais_spike":
            return VerificationStatus.DERIVED.value  # computed from AIS telemetry
        if self.source and self.source.lower() in ("manual", "operator"):
            return VerificationStatus.OPERATOR_ASSERTED.value
        return VerificationStatus.UNVERIFIED_PUBLIC_SOURCE.value

    def to_geojson_feature(self) -> dict[str, Any]:
        # Historical rows pre-date analysis-state metadata. Derive it lazily
        # at projection time so the archive is coherent without rewriting raw
        # observations or requiring a destructive migration.
        from core.intel.analysis_state import annotate_event_analysis
        annotate_event_analysis(self)
        geo = (
            {"type": "Point", "coordinates": [self.lon, self.lat]}
            if self.lat is not None and self.lon is not None
            else None
        )
        return {
            "type": "Feature",
            "geometry": geo,
            "properties": {
                "id": self.id,
                "type": self.type,
                "severity": self.severity,
                "tier": self.tier(),
                "priority": self.priority(),
                "maritime_domain": self.maritime_domain(),
                "verification_status": self.verification_status(),
                "drift_ready": geo is not None and self.tier() == "operational",
                "title": self.title,
                "text": self.text,
                "url": self.url,
                "source": self.source,
                "author": self.author,
                "linked_mmsi": self.linked_mmsi,
                "timestamp_utc": self.timestamp_utc,
                **self.metadata,
            },
        }

    def content_hash(self) -> str:
        """Stable dedup key based on source + core content."""
        raw = f"{self.source}:{self.title}:{self.text[:120]}"
        return hashlib.blake2s(raw.encode(), digest_size=8).hexdigest()

    # ── Canonical classification (docs/fixes.md Phase 2.2 / 2.3) ──────────────
    def canonical_columns(self) -> dict[str, Any]:
        """The explicit IntelEventDB classification columns, derived from the
        same logic the projection uses. Dual-written next to ``meta`` for one
        release so a SQL query can answer operational questions without
        decoding arbitrary JSON.
        """
        meta = self.metadata
        uncertainty = meta.get("location_uncertainty_m")
        try:
            uncertainty = float(uncertainty) if uncertainty is not None else None
        except (TypeError, ValueError):
            uncertainty = None
        return {
            "schema_version": 1,
            "source_timestamp_utc": meta.get("source_timestamp_utc") or self.timestamp_utc,
            "maritime_domain": self.maritime_domain(),
            "operational_tier": self.tier(),
            "humanitarian_case_type": meta.get("humanitarian_case_type"),
            "incident_lifecycle": meta.get("incident_lifecycle"),
            "location_status": meta.get("location_status"),
            "coordinate_review_status": meta.get("coordinate_review_status"),
            "location_uncertainty_m": uncertainty,
        }


def event_feature_with_lifecycle(
    event: IntelEvent,
    *,
    same_source: list[IntelEvent],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Project the canonical lifecycle onto every operational event contract."""
    feature = event.to_geojson_feature()
    if event.tier() != "operational":
        return feature
    from core.intel import lifecycle

    state = lifecycle.distress_lifecycle(
        event,
        now=now or datetime.now(timezone.utc),
        same_source=same_source,
    )
    feature["properties"]["incident_lifecycle"] = state
    feature["properties"]["kind"] = "distress" if state == "active" else state
    return feature


class IntelStore:
    def __init__(self, maxlen: int = MAX_EVENTS) -> None:
        self._events: deque[IntelEvent] = deque(maxlen=maxlen)
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._lock = threading.Lock()
        # (websocket_obj, asyncio_loop) — loop needed for thread-safe sends
        self._ws_clients: set[tuple[Any, asyncio.AbstractEventLoop]] = set()
        # In-process observers notified after every successful add() — the
        # single fan-out point the correlation/fusion engine hooks into
        # (mirrors core.ingestion.router.subscribe for messaging signals).
        self._subscribers: list[Callable[[IntelEvent], None]] = []
        # Serialize durable mutations for this store. Runtime writes remain
        # asynchronous, but a single FIFO worker prevents a slower earlier
        # write (for example location enrichment) from overwriting metadata
        # committed by a later lifecycle update. The worker is lazy so the
        # many short-lived IntelStore instances used by tests do not each
        # create an idle thread.
        self._persist_queue: queue.Queue[
            tuple[Callable[..., None], tuple[Any, ...], threading.Event | None]
        ] = queue.Queue()
        self._persist_worker: threading.Thread | None = None
        self._persist_worker_lock = threading.Lock()

    def _remember_seen_locked(self, keys) -> None:
        """Remember dedup keys in insertion order while bounding memory.

        Must be called with ``self._lock`` held.  The previous implementation
        truncated a ``set`` via ``list(set)``; set iteration is unordered, so
        it could evict fresh keys and retain stale ones nondeterministically.
        """
        for raw_key in keys:
            key = str(raw_key or "")
            if not key or key in self._seen:
                continue
            self._seen.add(key)
            self._seen_order.append(key)
        while len(self._seen_order) > DEDUP_WINDOW:
            oldest = self._seen_order.popleft()
            self._seen.discard(oldest)

    def _enqueue_persist(
        self, target: Callable[..., None], *args: Any, wait: bool = False
    ) -> None:
        """Run DB mutations in FIFO order; optionally wait for this write."""
        # Defensive against a future persistence callback enqueueing a blocking
        # write from the worker itself: execute inline rather than deadlocking.
        if wait and threading.current_thread() is self._persist_worker:
            target(*args)
            return
        completed = threading.Event() if wait else None
        self._persist_queue.put((target, args, completed))
        with self._persist_worker_lock:
            if self._persist_worker is None or not self._persist_worker.is_alive():
                self._persist_worker = threading.Thread(
                    target=self._persist_worker_loop,
                    name="seacommons-intel-persist",
                    daemon=True,
                )
                self._persist_worker.start()
        if completed is not None:
            completed.wait()

    def _persist_worker_loop(self) -> None:
        while True:
            target, args, completed = self._persist_queue.get()
            try:
                target(*args)
            except Exception as exc:  # defensive: one failed write must not stop FIFO
                logger.warning("intel_store persistence worker error: %s", exc)
            finally:
                if completed is not None:
                    completed.set()
                self._persist_queue.task_done()

    # ── Write ─────────────────────────────────────────────────────────────────

    def add(self, event: IntelEvent, dedup_key: str = "") -> bool:
        """
        Add an event.  Returns True if stored, False if duplicate.
        Thread-safe.
        """
        from core.intel.analysis_state import annotate_event_analysis
        annotate_event_analysis(event)
        content_key = event.content_hash()
        keys = {dedup_key or content_key, content_key}
        url_duplicate: Optional[IntelEvent] = None
        id_duplicate: Optional[IntelEvent] = None
        metadata_changed = False
        machine_refresh = False
        with self._lock:
            if event.url and event.type in _URL_DEDUP_TYPES:
                source_key = _normalised_source(event.source)
                url_duplicate = next(
                    (
                        candidate
                        for candidate in self._events
                        if candidate.url == event.url
                        and _normalised_source(candidate.source) == source_key
                    ),
                    None,
                )
            if event.type in _MACHINE_REFRESH_TYPES:
                id_duplicate = next(
                    (candidate for candidate in self._events if candidate.id == event.id),
                    None,
                )
            if url_duplicate is not None:
                # A second collector saw the same source item. Keep the
                # canonical event/id and add only metadata it did not already
                # have; in particular, never downgrade an official
                # source_policy with Twikit's transport policy.
                merged = {**event.metadata, **url_duplicate.metadata}
                metadata_changed = merged != url_duplicate.metadata
                url_duplicate.metadata = merged
                self._remember_seen_locked(keys)
            elif id_duplicate is not None:
                # Deterministic machine IDs are mutable episode identities.
                # Refresh the canonical in-memory object and durable row, but
                # do not treat the refresh as a new event or rebroadcast it.
                merged = {**id_duplicate.metadata, **event.metadata}
                machine_refresh = (
                    merged != id_duplicate.metadata
                    or id_duplicate.timestamp_utc != event.timestamp_utc
                    or id_duplicate.lat != event.lat
                    or id_duplicate.lon != event.lon
                    or id_duplicate.title != event.title
                    or id_duplicate.text != event.text
                    or id_duplicate.severity != event.severity
                )
                id_duplicate.timestamp_utc = event.timestamp_utc
                id_duplicate.type = event.type
                id_duplicate.severity = event.severity
                id_duplicate.lat = event.lat
                id_duplicate.lon = event.lon
                id_duplicate.title = event.title
                id_duplicate.text = event.text
                id_duplicate.url = event.url
                id_duplicate.source = event.source
                id_duplicate.linked_mmsi = event.linked_mmsi
                id_duplicate.metadata = merged
                event.metadata = merged
                self._remember_seen_locked(keys)
            elif any(key in self._seen for key in keys):
                return False
            else:
                self._remember_seen_locked(keys)
                self._events.appendleft(event)

        if url_duplicate is not None:
            if metadata_changed:
                self._enqueue_persist(
                    self._persist_metadata_sync,
                    url_duplicate.id,
                    dict(url_duplicate.metadata),
                    url_duplicate.linked_mmsi,
                )
            return False

        if id_duplicate is not None:
            if machine_refresh:
                self._persist(event)
            return False

        self._fire_broadcast(event)
        self._persist(event)
        self._notify_subscribers(event)
        return True

    # ── Observers ─────────────────────────────────────────────────────────────

    def subscribe(self, fn: Callable[[IntelEvent], None]) -> None:
        """Register a callback invoked (off-thread) after every new event is
        stored. Idempotent: registering the exact same callable twice (an
        accidental double-call of a module's own register(), or -- as
        surfaced by this test suite -- many independent test fixtures each
        calling register() over one shared process-global intel_store) is a
        no-op rather than a second registration -- otherwise every event
        gets processed once per accumulated duplicate, racing multiple
        redundant writers against the same downstream state."""
        with self._lock:
            if fn not in self._subscribers:
                self._subscribers.append(fn)

    def _notify_subscribers(self, event: IntelEvent) -> None:
        with self._lock:
            subs = list(self._subscribers)
        if not subs:
            return

        def _run() -> None:
            for fn in subs:
                try:
                    fn(event)
                except Exception as exc:  # never let an observer break ingestion
                    logger.warning("intel_store subscriber error: %s", exc)

        threading.Thread(target=_run, daemon=True).start()

    def load_from_db(self, limit: int = MAX_EVENTS, max_age_days: int = 30) -> int:
        """
        Reload recent events from DB into the in-memory store on startup.
        Only loads events from the last `max_age_days` days.
        Returns number of events loaded.  Silent on any DB error.
        """
        try:
            from core.db.session import session_scope
            from core.db.models import IntelEventDB
            events_to_add: list[IntelEvent] = []
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            dedup_only: list[IntelEvent] = []
            with session_scope() as db:
                rows = (
                    db.query(IntelEventDB)
                    .filter(IntelEventDB.timestamp_utc >= cutoff)
                    .order_by(IntelEventDB.timestamp_utc.desc())
                    .limit(limit)
                    .all()
                )
                # Build IntelEvent objects inside session so all columns are
                # accessed while the session is open (expire_on_commit=True default)
                for row in reversed(rows):
                    events_to_add.append(IntelEvent(
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
                        metadata=dict(row.meta or {}),
                    ))
                # The `limit` most-recent rows can be only a few hours of AIS
                # churn, dropping a still-tracked Alarm Phone distress incident
                # from this set. When the X monitor's catch-up poll then
                # re-sees that incident's tweet its dedup key is not in
                # `_seen`, so a second marker (fresh id, place-name fallback
                # position) is raised for one boat. Seed the dedup keys for
                # every recent humanitarian-source row regardless of the cap --
                # these do not need a slot in the bounded deque, only a key in
                # `_seen` so re-ingestion is recognised.
                loaded_ids = {row.id for row in rows}
                for row in (
                    db.query(IntelEventDB)
                    .filter(
                        IntelEventDB.timestamp_utc >= cutoff,
                        IntelEventDB.source.in_(["Alarm Phone", "alarm_phone", "alarmphone"]),
                    )
                    .order_by(IntelEventDB.timestamp_utc.desc())
                    .limit(500)
                    .all()
                ):
                    if row.id in loaded_ids:
                        continue
                    dedup_only.append(IntelEvent(
                        id=row.id, timestamp_utc=row.timestamp_utc, type=row.type or "",
                        title=row.title or "", text=row.text or "", url=row.url or "",
                        source=row.source or "", metadata=dict(row.meta or {}),
                    ))
            loaded = 0
            for ev in events_to_add:
                keys = {ev.content_hash()}
                tweet_id = str(ev.metadata.get("tweet_id") or "")
                if tweet_id:
                    keys.add(f"x:{tweet_id}")
                with self._lock:
                    if not any(key in self._seen for key in keys):
                        self._remember_seen_locked(keys)
                        self._events.appendleft(ev)
                        loaded += 1
            with self._lock:
                for ev in dedup_only:
                    self._remember_seen_locked([ev.content_hash()])
                    tweet_id = str(ev.metadata.get("tweet_id") or "")
                    if tweet_id:
                        self._remember_seen_locked([f"x:{tweet_id}"])
            logger.info(
                "intel_store: loaded %d events from DB (+%d dedup-only humanitarian keys)",
                loaded, len(dedup_only),
            )
            return loaded
        except Exception as exc:
            logger.warning("intel_store: DB reload skipped: %s", exc)
            return 0

    def sync_from_db(self, limit: int = MAX_EVENTS, max_age_days: int = 7) -> tuple[int, int]:
        """Pull recent DB rows in, updating events already cached in memory.

        load_from_db() only ever adds — it never touches an event this
        process has already seen, so metadata written by a monitor running
        in a *different* process (e.g. a drift_status flip to "completed")
        would never reach this process's copy. Call this on an interval
        instead when monitors and API are split across processes/VMs.
        Returns (new_count, updated_count).
        """
        try:
            from core.db.session import session_scope
            from core.db.models import IntelEventDB
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            with session_scope() as db:
                rows = (
                    db.query(IntelEventDB)
                    .filter(IntelEventDB.timestamp_utc >= cutoff)
                    .order_by(IntelEventDB.timestamp_utc.desc())
                    .limit(limit)
                    .all()
                )
                row_data = [
                    (row.id, row.timestamp_utc, row.type or "", row.severity or "",
                     row.lat, row.lon, row.title or "", row.text or "", row.url or "",
                     row.source or "", row.linked_mmsi or "", dict(row.meta or {}))
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("intel_store: DB sync skipped: %s", exc)
            return (0, 0)

        new_count = 0
        updated_count = 0
        changed_events: list[IntelEvent] = []
        with self._lock:
            by_id = {event.id: event for event in self._events}
            for (row_id, timestamp_utc, type_, severity, lat, lon, title, text, url,
                 source, linked_mmsi, meta) in row_data:
                existing = by_id.get(row_id)
                if existing is not None:
                    if existing.metadata != meta or existing.lat != lat or existing.lon != lon:
                        existing.lat = lat
                        existing.lon = lon
                        existing.metadata = meta
                        updated_count += 1
                        changed_events.append(existing)
                    continue
                new_event = IntelEvent(
                    id=row_id, timestamp_utc=timestamp_utc, type=type_, severity=severity,
                    lat=lat, lon=lon, title=title, text=text, url=url,
                    source=source, linked_mmsi=linked_mmsi, metadata=meta,
                )
                keys = {new_event.content_hash()}
                tweet_id = str(meta.get("tweet_id") or "")
                if tweet_id:
                    keys.add(f"x:{tweet_id}")
                if any(key in self._seen for key in keys):
                    continue
                self._remember_seen_locked(keys)
                self._events.appendleft(new_event)
                new_count += 1
                changed_events.append(new_event)
        for changed_event in changed_events:
            self._fire_broadcast(changed_event)
        if new_count or updated_count:
            logger.info("intel_store: sync_from_db +%d new, %d updated", new_count, updated_count)
        return (new_count, updated_count)

    def reset_computing_drifts(self) -> int:
        """
        After startup, any in-memory event whose drift_status is 'computing'
        was orphaned by the previous process kill. Reset to 'failed' so the
        UI shows a Retry button instead of a permanent spinner.
        Also persists the change to DB so it survives future restarts.
        Returns number of events fixed.
        """
        to_fix: list[str] = []
        with self._lock:
            for ev in self._events:
                if ev.metadata.get("drift_status") == "computing":
                    ev.metadata["drift_status"] = "failed"
                    to_fix.append(ev.id)
        if to_fix:
            logger.info("intel_store: reset %d orphaned computing drift(s) to failed", len(to_fix))
            self._enqueue_persist(self._persist_drift_status_reset, to_fix)
        return len(to_fix)

    def _persist_drift_status_reset(self, event_ids: list[str]) -> None:
        """Write drift_status=failed into DB meta for each orphaned event."""
        try:
            from core.db.session import session_scope
            from core.db.models import IntelEventDB
            import json as _json
            with session_scope() as db:
                rows = db.query(IntelEventDB).filter(IntelEventDB.id.in_(event_ids)).all()
                for row in rows:
                    meta = dict(row.meta or {})
                    if meta.get("drift_status") == "computing":
                        meta["drift_status"] = "failed"
                        row.meta = meta
                db.flush()
        except Exception as exc:
            logger.debug("intel_store: DB drift_status reset skipped: %s", exc)

    def _persist(self, event: IntelEvent) -> None:
        """Write asynchronously in runtime, synchronously in isolated tests."""
        if os.getenv("SEACOMMONS_INTEL_PERSIST_SYNC", "").lower() in {"1", "true", "yes"}:
            self._persist_sync(event)
            return
        self._enqueue_persist(self._persist_sync, event)

    @staticmethod
    def _live_case_key_for(event: "IntelEvent") -> Optional[str]:
        from core.live.retention import live_case_key

        return live_case_key(event.type, event.metadata or {}, event.linked_mmsi)

    @staticmethod
    def _live_retention_columns(
        event: "IntelEvent", *, prior_entered_at, prior_expires_at,
    ) -> dict[str, Any]:
        """Durable Live retention stamp for a persisted IntelEventDB row.

        Only a qualifying AIS-evidence event gets a window; everything else
        (the vast majority of events) gets all-None columns, unchanged from
        before this contract existed.
        """
        from core.live.retention import compute_retention_window, is_qualifying_ais_evidence

        metadata = event.metadata or {}
        if not is_qualifying_ais_evidence(event.type, metadata):
            return {
                "live_entered_at": None,
                "last_qualified_observation_at": None,
                "live_expires_at": None,
                "live_case_key": None,
            }
        now = datetime.now(timezone.utc)
        entered_at, last_qualified_at, expires_at = compute_retention_window(
            prior_entered_at=prior_entered_at, prior_expires_at=prior_expires_at, now=now,
        )
        return {
            "live_entered_at": entered_at,
            "last_qualified_observation_at": last_qualified_at,
            "live_expires_at": expires_at,
            "live_case_key": IntelStore._live_case_key_for(event),
        }

    @staticmethod
    def _apply_retention_to_metadata(event: "IntelEvent", retained: dict[str, Any]) -> None:
        """Mirror the durable retention stamp onto the in-memory event too.

        Without this, an event still resident in the bounded deque (never
        evicted, no restart) would have no way to tell feed.py it already
        earned a durable window -- only live_retained_events()'s DB read
        would know. Keeping both in sync means the read-side check in
        core.live.feed is uniform regardless of whether the event came from
        the deque or a durable re-hydration.
        """
        if retained.get("live_expires_at") is None:
            return
        event.metadata = {
            **event.metadata,
            "live_entered_at": retained["live_entered_at"].isoformat(),
            "last_qualified_observation_at": retained["last_qualified_observation_at"].isoformat(),
            "live_expires_at": retained["live_expires_at"].isoformat(),
        }

    def _persist_sync(self, event: IntelEvent) -> None:
        try:
            from core.db.session import session_scope
            from core.db.models import IntelEventDB
            with session_scope() as db:
                existing_by_id = db.query(IntelEventDB).filter(
                    IntelEventDB.id == event.id
                ).first()
                if existing_by_id is not None:
                    # Deterministic IDs are updateable machine episodes.
                    existing_by_id.timestamp_utc = event.timestamp_utc
                    existing_by_id.type = event.type
                    existing_by_id.severity = event.severity
                    existing_by_id.lat = event.lat
                    existing_by_id.lon = event.lon
                    existing_by_id.title = event.title[:255]
                    existing_by_id.text = event.text
                    existing_by_id.url = event.url[:511]
                    existing_by_id.source = event.source
                    existing_by_id.linked_mmsi = event.linked_mmsi
                    merged = {**dict(existing_by_id.meta or {}), **event.metadata}
                    existing_by_id.meta = merged
                    event.metadata = merged
                    retained = self._live_retention_columns(
                        event, prior_entered_at=existing_by_id.live_entered_at,
                        prior_expires_at=existing_by_id.live_expires_at,
                    )
                    for key, value in retained.items():
                        setattr(existing_by_id, key, value)
                    self._apply_retention_to_metadata(event, retained)
                    db.flush()
                    return
                if event.url and event.type in _URL_DEDUP_TYPES:
                    # Persistent dedup: in-memory _seen is empty after a restart,
                    # so a feed item that was already ingested (same source+url,
                    # e.g. an RSS article or tweet re-fetched at boot) would
                    # otherwise be inserted again as a fresh row → duplicate
                    # markers on the live edge. Refuse to insert the duplicate.
                    existing = db.query(IntelEventDB).filter(
                        IntelEventDB.source == event.source,
                        IntelEventDB.url == event.url,
                    ).first()
                    if existing is not None:
                        merged = {**event.metadata, **dict(existing.meta or {})}
                        existing.meta = merged
                        if existing.lat is None and event.lat is not None:
                            existing.lat = event.lat
                            existing.lon = event.lon
                        # The event may not have been inside the bounded
                        # in-memory DB preload. Repoint the live object to the
                        # durable canonical ID before reply discovery runs.
                        event.id = str(existing.id)
                        event.metadata = merged
                        db.flush()
                        logger.debug(
                            "Intel DB duplicate merged into canonical event %s",
                            existing.id,
                        )
                        return
                prior_entered_at = prior_expires_at = None
                case_key = self._live_case_key_for(event)
                if case_key is not None:
                    prior_case = (
                        db.query(IntelEventDB)
                        .filter(IntelEventDB.live_case_key == case_key)
                        .order_by(IntelEventDB.created_at.desc())
                        .first()
                    )
                    if prior_case is not None:
                        prior_entered_at = prior_case.live_entered_at
                        prior_expires_at = prior_case.live_expires_at
                retained = self._live_retention_columns(
                    event, prior_entered_at=prior_entered_at, prior_expires_at=prior_expires_at,
                )
                self._apply_retention_to_metadata(event, retained)
                db.add(IntelEventDB(
                    id=event.id,
                    timestamp_utc=event.timestamp_utc,
                    type=event.type,
                    severity=event.severity,
                    lat=event.lat,
                    lon=event.lon,
                    title=event.title[:255],
                    text=event.text,
                    url=event.url[:511],
                    source=event.source,
                    linked_mmsi=event.linked_mmsi,
                    meta=event.metadata,
                    **event.canonical_columns(),
                    **retained,
                ))
        except Exception as exc:
            # WARNING, not DEBUG: this is invisible at default production log
            # level otherwise, and this exact silence has already hidden two
            # real incidents in this codebase (missing intel_events indexes;
            # a swallowed auto-drift request failure). A fresh cause here --
            # e.g. the API deployed before `alembic upgrade head` ran, so
            # intel_events is missing a canonical_columns() column -- must be
            # loud, since it means durable persistence is silently broken.
            logger.warning("Intel DB persist skipped for event %s: %s", event.id, exc)

    def enrich_location(
        self,
        event_id: str,
        *,
        lat: float,
        lon: float,
        metadata: dict[str, Any],
    ) -> bool:
        """Attach a location, upgrading an existing lower-quality estimate."""
        # Every plotted location is a boat at sea. An OCR readout or a
        # drop-pin pixel fit can land a few km inland; nudge onto water before
        # it is stored or broadcast. No-op when already at sea / landmask off.
        try:
            from core.intel.landmask import nearest_sea_point

            lat, lon = nearest_sea_point(float(lat), float(lon))
        except Exception:  # pragma: no cover - never block an enrich on this
            pass
        updated: Optional[IntelEvent] = None
        with self._lock:
            for event in self._events:
                if event.id != event_id:
                    continue
                if event.lat is not None or event.lon is not None:
                    # docs/fixes.md F-04: compare evidence quality, not source
                    # rank alone -- a disputed / lone-engine coordinate can be
                    # stored for review but must never supersede a verified one.
                    from core.intel.location_evidence import metadata_quality

                    if metadata_quality(metadata) <= metadata_quality(event.metadata):
                        return False
                event.lat = lat
                event.lon = lon
                event.metadata.update(metadata)
                # Upgrading to a real point supersedes any prior area result.
                # dict.update() above only adds/overwrites keys the new
                # metadata mentions -- it never removes ones it doesn't, so a
                # stale area_geojson (e.g. from the initial bare-place-match
                # fallback, before OCR found the real position) would keep
                # silently overriding the new point forever: the public
                # projection (core.intel.public_geometry) always prefers
                # area_geojson over lat/lon whenever it's present.
                if (
                    event.metadata.get("coordinate_source") != "region_area"
                    and "area_geojson" not in metadata
                ):
                    event.metadata.pop("area_geojson", None)
                    event.metadata.pop("area_confidence", None)
                    event.metadata.pop("area_weather_narrowed", None)
                updated = event
                break
        if updated is None:
            return False
        self._enqueue_persist(
            self._persist_location_sync, event_id, lat, lon, dict(metadata)
        )
        self._fire_broadcast(updated)
        return True

    def _persist_location_sync(
        self,
        event_id: str,
        lat: float,
        lon: float,
        metadata: dict[str, Any],
    ) -> None:
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope

            with session_scope() as db:
                row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
                if row is None:
                    return
                merged = dict(row.meta or {})
                if row.lat is not None or row.lon is not None:
                    from core.intel.location_evidence import metadata_quality

                    if metadata_quality(metadata) <= metadata_quality(merged):
                        return
                row.lat = lat
                row.lon = lon
                merged.update(metadata)
                # Same as the in-memory path above: an upgrade to a real
                # point must not leave a now-stale area_geojson in the
                # durable row either, or a process restart (which reloads
                # from this row) would resurrect the bug the in-memory fix
                # just cleared.
                if merged.get("coordinate_source") != "region_area" and "area_geojson" not in metadata:
                    merged.pop("area_geojson", None)
                    merged.pop("area_confidence", None)
                    merged.pop("area_weather_narrowed", None)
                row.meta = merged
                # Keep the dual-written classification columns in step with the
                # metadata they mirror (Phase 2.3).
                if "coordinate_review_status" in metadata:
                    row.coordinate_review_status = metadata["coordinate_review_status"]
                if merged.get("location_uncertainty_m") is not None:
                    try:
                        row.location_uncertainty_m = float(merged["location_uncertainty_m"])
                    except (TypeError, ValueError):
                        pass
                db.flush()
        except Exception as exc:
            logger.debug("Intel DB location enrichment skipped: %s", exc)

    def update_metadata(
        self,
        event_id: str,
        *,
        metadata: dict[str, Any],
        linked_mmsi: Optional[str] = None,
    ) -> bool:
        """Merge additional metadata onto an existing event in place.

        Used to record cross-source corroboration (see intel/triangulation.py)
        without creating a duplicate event or overwriting fields set by the
        original ingestion path.
        """
        updated: Optional[IntelEvent] = None
        with self._lock:
            for event in self._events:
                if event.id != event_id:
                    continue
                event.metadata.update(metadata)
                if linked_mmsi:
                    event.linked_mmsi = linked_mmsi
                updated = event
                break
        if updated is None:
            return False
        self._enqueue_persist(
            self._persist_metadata_sync, event_id, dict(updated.metadata), updated.linked_mmsi
        )
        self._fire_broadcast(updated)
        return True

    def update_vessel_episode(
        self,
        event_id: str,
        *,
        lat: float,
        lon: float,
        timestamp_utc: str,
        sog: Optional[float] = None,
        nav_status: Optional[int] = None,
        incident_lifecycle: Optional[str] = None,
    ) -> bool:
        """Move a stable vessel incident forward instead of adding a new dot."""
        normalized = event_id.removeprefix("intel:")
        updated: Optional[IntelEvent] = None
        with self._lock:
            for event in self._events:
                if event.id != normalized:
                    continue
                observation = {
                    "lon": round(float(lon), 6),
                    "lat": round(float(lat), 6),
                    "ts": timestamp_utc,
                    **({"sog": round(float(sog), 2)} if sog is not None else {}),
                    **({"nav_status": int(nav_status)} if nav_status is not None else {}),
                }
                track = [p for p in (event.metadata.get("observed_track") or []) if isinstance(p, dict)]
                if not track or (
                    track[-1].get("lon"), track[-1].get("lat"), track[-1].get("ts")
                ) != (observation["lon"], observation["lat"], observation["ts"]):
                    track.append(observation)
                event.metadata["observed_track"] = track[-120:]
                event.metadata["episode_update_count"] = int(
                    event.metadata.get("episode_update_count") or 1
                ) + 1
                event.metadata.setdefault("first_observed_at", event.timestamp_utc)
                event.metadata["last_observed_at"] = timestamp_utc
                if incident_lifecycle:
                    event.metadata["incident_lifecycle"] = incident_lifecycle
                event.lat = float(lat)
                event.lon = float(lon)
                event.timestamp_utc = timestamp_utc
                updated = event
                break
        if updated is None:
            return False
        self._enqueue_persist(
            self._persist_vessel_episode_sync,
            normalized,
            updated.lat,
            updated.lon,
            updated.timestamp_utc,
            dict(updated.metadata),
        )
        self._fire_broadcast(updated)
        return True

    def _persist_vessel_episode_sync(
        self,
        event_id: str,
        lat: float,
        lon: float,
        timestamp_utc: str,
        metadata: dict[str, Any],
    ) -> None:
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope

            with session_scope() as db:
                row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
                if row is None:
                    return
                row.lat = lat
                row.lon = lon
                row.timestamp_utc = timestamp_utc
                row.meta = metadata
                db.flush()
        except Exception as exc:
            logger.debug("Intel DB vessel episode update skipped: %s", exc)

    def _persist_metadata_sync(
        self,
        event_id: str,
        metadata: dict[str, Any],
        linked_mmsi: str,
    ) -> None:
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope
            with session_scope() as db:
                row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
                if row is None:
                    return
                row.meta = metadata
                if linked_mmsi:
                    row.linked_mmsi = linked_mmsi
                # Dual-write the classification columns that live in metadata
                # (Phase 2.3). Only overwrite when the new metadata carries a
                # value, so an unrelated corroboration update never nulls them.
                for meta_key, column in (
                    ("humanitarian_case_type", "humanitarian_case_type"),
                    ("incident_lifecycle", "incident_lifecycle"),
                    ("location_status", "location_status"),
                    ("coordinate_review_status", "coordinate_review_status"),
                ):
                    if metadata.get(meta_key) is not None:
                        setattr(row, column, metadata[meta_key])
                db.flush()
        except Exception as exc:
            logger.debug("Intel DB metadata update skipped: %s", exc)

    def find_by_tweet_id(self, tweet_id: str) -> Optional[IntelEvent]:
        """Locate an in-memory event by its source tweet id."""
        normalized = str(tweet_id)
        with self._lock:
            return next(
                (
                    event
                    for event in self._events
                    if str(event.metadata.get("tweet_id") or "") == normalized
                ),
                None,
            )

    def find_by_content_hash(self, content_hash: str) -> Optional[IntelEvent]:
        """Locate an in-memory event whose current content_hash matches.

        Used to recover the real stored event behind a dedup-rejected add():
        add() only records content_hash in a flat _seen set, so the caller
        otherwise has no way back to the actual event (a rejected duplicate
        is a fresh, never-stored IntelEvent with its own throwaway id).
        """
        with self._lock:
            return next(
                (event for event in self._events if event.content_hash() == content_hash),
                None,
            )

    def find_by_source_url(self, source: str, url: str) -> Optional[IntelEvent]:
        """Locate the canonical event for a source item already in memory."""
        source_key = _normalised_source(source)
        with self._lock:
            return next(
                (
                    event
                    for event in self._events
                    if event.url == url and _normalised_source(event.source) == source_key
                ),
                None,
            )

    def refresh_source_link(self, event_id: str, *, tweet_id: str, url: str) -> bool:
        """Repoint an existing event at a newer tweet id/url for the same content.

        Tracked accounts occasionally delete a tweet moments after posting and
        repost near-identical text — same content_hash, new tweet id. Content-hash
        dedup then silently drops the repost, leaving the stored event's link
        pointing at a dead status forever. This updates the link in place,
        unconditionally (unlike enrich_location, not gated on coordinate-quality
        ranking), since a live link is strictly better than a dead one regardless
        of position precision.
        """
        updated: Optional[IntelEvent] = None
        with self._lock:
            for event in self._events:
                if event.id != event_id:
                    continue
                event.url = url
                event.metadata["tweet_id"] = tweet_id
                updated = event
                break
        if updated is None:
            return False
        self._enqueue_persist(self._persist_link_sync, event_id, url, tweet_id)
        self._fire_broadcast(updated)
        return True

    def _persist_link_sync(self, event_id: str, url: str, tweet_id: str) -> None:
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope
            with session_scope() as db:
                row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
                if row is None:
                    return
                row.url = url
                merged = dict(row.meta or {})
                merged["tweet_id"] = tweet_id
                row.meta = merged
                db.flush()
        except Exception as exc:
            logger.debug("Intel DB link refresh skipped: %s", exc)

    def append_thread_repost(self, event_id: str, repost: dict[str, Any]) -> bool:
        """Record a repost onto an existing incident's thread.

        The repost is attached to the SAME incident (thread) the source alert
        opened, so a repost/echo can never spawn a new marker. A verified reply
        carrying text may change lifecycle and is broadcast immediately; a
        plain repost only updates bookkeeping and remains silent.

        Delayed replies can arrive after AIS churn evicts the incident from the
        bounded in-memory deque. Rehydrate that one durable event before the
        update so reply/lifecycle correctness never depends on deque residency.
        """
        normalized = event_id.removeprefix("intel:")
        if self.get(normalized) is None:
            durable = self.get_durable(normalized)
            if durable is None:
                return False
            with self._lock:
                if not any(event.id == normalized for event in self._events):
                    self._events.appendleft(durable)

        updated: Optional[IntelEvent] = None
        with self._lock:
            for event in self._events:
                if event.id != event_id:
                    continue
                posts = list(event.metadata.get("thread_reposts") or [])
                if any(
                    str(post.get("tweet_id")) == str(repost.get("tweet_id"))
                    for post in posts
                ):
                    return False
                posts.append(repost)
                event.metadata["thread_reposts"] = posts[-20:]
                event.metadata["repost_count"] = len(posts)
                event.metadata["last_repost_at"] = repost.get("posted_at")
                updated = event
                break
        if updated is None:
            return False
        if repost.get("note"):
            # Verified replies change lifecycle and are rare/high-value. Commit
            # before broadcasting so a restart cannot leave a visible reply
            # attached only to an in-memory event.
            self._enqueue_persist(
                self._persist_metadata_sync,
                event_id,
                dict(updated.metadata),
                updated.linked_mmsi,
                wait=True,
            )
            self._fire_broadcast(updated)
        else:
            self._enqueue_persist(
                self._persist_metadata_sync, event_id, dict(updated.metadata), updated.linked_mmsi
            )
        # docs/updates.md P0.9: a reply/repost/quote/translation-twin/self-
        # reply-resolution never creates a new IntelEvent (event_id above is
        # always the SAME id as the founding event this thread already
        # belongs to) -- but until this call, that also meant the canonical
        # HumanitarianIncidentDB (core.intel.humanitarian_incident, upserted
        # by event.id) never re-synced on a thread update, going stale
        # relative to what core.live.feed's own read-time lifecycle
        # recomputation already reflected. Reusing intel_store's existing
        # subscriber fan-out here is exactly the "Alarm Phone update updates
        # the existing incident" cutover requirement: sync_incident_for_event
        # is an upsert BY event.id, so this can only ever update the incident
        # already keyed to this thread, never create a second one.
        #
        # sync_incident_for_event derives HumanitarianIncidentDB.last_update_at
        # from the passed event's own timestamp_utc -- which append_thread_repost
        # deliberately never mutates on the real stored event (that field means
        # "when the founding post was made", not "when this thread was last
        # active"; changing it here would corrupt every other reader of
        # timestamp_utc, e.g. reported_at/age/sort order). Notify with a
        # throwaway dataclasses.replace() copy carrying the repost's own
        # posted_at instead, so the canonical incident's *activity* timer
        # advances without touching the real event's own timestamp.
        from dataclasses import replace as _dc_replace

        notify_event = updated
        posted_at = repost.get("posted_at")
        if posted_at:
            from core.intel.lifecycle import parse_utc

            posted_ts = parse_utc(str(posted_at))
            current_ts = parse_utc(updated.timestamp_utc)
            if posted_ts is not None and (current_ts is None or posted_ts > current_ts):
                notify_event = _dc_replace(updated, timestamp_utc=str(posted_at))

        self._notify_subscribers(notify_event)
        return True

    def touch_source_observation(self, event_id: str, observed_at: str) -> bool:
        """Record that a source item is still visible without creating a copy."""
        found = False
        with self._lock:
            for event in self._events:
                if event.id != event_id:
                    continue
                event.metadata["last_source_seen_at"] = observed_at
                event.metadata["source_scan_count"] = int(
                    event.metadata.get("source_scan_count") or 1
                ) + 1
                found = True
                break
        self._enqueue_persist(self._persist_source_observation_sync, event_id, observed_at)
        return found

    def _persist_source_observation_sync(self, event_id: str, observed_at: str) -> None:
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope
            with session_scope() as db:
                row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
                if row is None:
                    return
                metadata = dict(row.meta or {})
                metadata["last_source_seen_at"] = observed_at
                metadata["source_scan_count"] = int(metadata.get("source_scan_count") or 1) + 1
                row.meta = metadata
                db.flush()
        except Exception as exc:
            logger.debug("Intel DB source observation skipped: %s", exc)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, event_id: str) -> Optional[IntelEvent]:
        normalized = event_id.removeprefix("intel:")
        with self._lock:
            return next((event for event in self._events if event.id == normalized), None)

    def get_durable(self, event_id: str) -> Optional[IntelEvent]:
        """`get()`, but fall back to the DB row when the event has been
        evicted from the bounded in-memory deque.

        A ~20 h old Alarm Phone distress incident is routinely pushed out of
        the 600-slot deque by AIS churn. Callers that must still reason about
        it (the auto-drift F-01 gate, its report timestamp) got ``None`` and
        silently skipped the check; this keeps them correct.
        """
        found = self.get(event_id)
        if found is not None:
            return found
        normalized = event_id.removeprefix("intel:")
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope

            with session_scope() as db:
                row = (
                    db.query(IntelEventDB)
                    .filter(IntelEventDB.id == normalized)
                    .first()
                )
                if row is None:
                    return None
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
                    metadata=dict(row.meta or {}),
                )
        except Exception as exc:  # pragma: no cover - DB fallback is best-effort
            logger.warning("intel_store.get_durable(%s) DB fallback failed: %s", normalized, exc)
            return None

    def persisted_events(
        self,
        *,
        source: Optional[str] = None,
        source_in: Optional[list[str]] = None,
        types: Optional[list[str]] = None,
        max_age_days: int = 30,
        limit: Optional[int] = 1000,
    ) -> list[IntelEvent]:
        """Read durable events independently from the bounded in-memory deque.

        `source` is an exact match; a collector's own `source=` string is not
        guaranteed to be spelled one way everywhere (e.g. the Alarm Phone
        twikit ingester writes "alarm_phone", an older RSS ingester writes
        "Alarm Phone" -- both real values seen in production). Pass
        `source_in` with every known variant when the caller cares about a
        logical source regardless of which ingester wrote it; `source`
        remains for a caller that genuinely wants one exact string.
        """
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope

            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            with session_scope() as db:
                query = db.query(IntelEventDB).filter(IntelEventDB.timestamp_utc >= cutoff)
                if source:
                    query = query.filter(IntelEventDB.source == source)
                if source_in:
                    query = query.filter(IntelEventDB.source.in_(source_in))
                if types:
                    query = query.filter(IntelEventDB.type.in_(types))
                query = query.order_by(IntelEventDB.timestamp_utc.desc())
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [
                    IntelEvent(
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
                        metadata=dict(row.meta or {}),
                    )
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("intel_store: durable event read skipped: %s", exc)
            return []

    def live_retained_events(self, *, limit: int = 500) -> list[IntelEvent]:
        """Durably re-hydrate every still-in-window qualified observation.

        core.live.retention's whole point: visibility is a function of
        ``live_expires_at`` alone, never of deque residency. Unlike
        persisted_events(), the age filter is live_expires_at > now, not a
        rolling calendar window -- a qualifying event stays eligible for the
        entire retention window regardless of how old its timestamp_utc is.
        """
        try:
            from core.db.models import IntelEventDB
            from core.db.session import session_scope

            # This project's SQLite DateTime columns round-trip naive (see
            # core.intel.lifecycle.parse_utc and friends); a naive-UTC filter
            # value compares correctly against them; an aware one would not
            # reliably in the sqlite3 driver's default adapter.
            now_naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            with session_scope() as db:
                rows = (
                    db.query(IntelEventDB)
                    .filter(IntelEventDB.live_expires_at.isnot(None))
                    .filter(IntelEventDB.live_expires_at > now_naive_utc)
                    .order_by(IntelEventDB.live_expires_at.desc())
                    .limit(limit)
                    .all()
                )
                return [
                    IntelEvent(
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
                        metadata={
                            **dict(row.meta or {}),
                            "live_entered_at": row.live_entered_at.isoformat() if row.live_entered_at else None,
                            "last_qualified_observation_at": (
                                row.last_qualified_observation_at.isoformat()
                                if row.last_qualified_observation_at else None
                            ),
                            "live_expires_at": row.live_expires_at.isoformat() if row.live_expires_at else None,
                        },
                    )
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("intel_store: live-retained event read skipped: %s", exc)
            return []

    def events(
        self,
        severity: Optional[str] = None,
        type_filter: Optional[str] = None,
        limit: int = 200,
        max_age_days: int = 30,
    ) -> list[IntelEvent]:
        with self._lock:
            evts = list(self._events)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        evts = [e for e in evts if (e.timestamp_utc or "") >= cutoff]
        if severity:
            evts = [e for e in evts if e.severity == severity]
        if type_filter:
            evts = [e for e in evts if e.type == type_filter]
        return evts[:limit]

    def geojson(
        self,
        severity: Optional[str] = None,
        type_filter: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        features = [
            e.to_geojson_feature()
            for e in self.events(severity, type_filter, limit)
            if e.lat is not None and e.lon is not None
        ]
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "total": len(features),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            evts = list(self._events)
        by_type: dict[str, int] = {}
        by_sev: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for e in evts:
            by_type[e.type] = by_type.get(e.type, 0) + 1
            by_sev[e.severity] = by_sev.get(e.severity, 0) + 1
            t = e.tier()
            by_tier[t] = by_tier.get(t, 0) + 1
        return {
            "total": len(evts),
            "by_type": by_type,
            "by_severity": by_sev,
            "by_tier": by_tier,
        }

    # ── WebSocket broadcast ───────────────────────────────────────────────────

    def register_ws(self, ws: Any, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._ws_clients.add((ws, loop))

    def unregister_ws(self, ws: Any) -> None:
        with self._lock:
            self._ws_clients = {(w, l) for w, l in self._ws_clients if w is not ws}

    def broadcast_event_update(self, event_id: str, update: dict) -> None:
        """Push a lightweight update packet to all WS clients (e.g. drift completed)."""
        payload = json.dumps({"type": "event_update", "id": event_id, **update})
        with self._lock:
            clients = list(self._ws_clients)
        for ws, loop in clients:
            try:
                asyncio.run_coroutine_threadsafe(_ws_send(ws, payload), loop)
            except Exception:
                pass

    def _fire_broadcast(self, event: IntelEvent) -> None:
        """
        Non-blocking: schedule coroutine in each registered loop.
        Called from background threads after a successful add().
        """
        with self._lock:
            same_source = [candidate for candidate in self._events if candidate.source == event.source]
        payload = json.dumps(event_feature_with_lifecycle(event, same_source=same_source))
        dead: list[Any] = []
        with self._lock:
            clients = list(self._ws_clients)
        for ws, loop in clients:
            try:
                asyncio.run_coroutine_threadsafe(
                    _ws_send(ws, payload), loop
                )
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister_ws(ws)


async def _ws_send(ws: Any, payload: str) -> None:
    try:
        await ws.send_text(payload)
    except Exception:
        pass


# ── Module-level singleton ────────────────────────────────────────────────────
intel_store = IntelStore()
