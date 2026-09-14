# SPDX-License-Identifier: AGPL-3.0-or-later
"""Low-memory live publisher from SeaCommons intel state to the edge.

The operational mode is deliberately live-first:
- no historical backfill is required;
- recent rows are rescanned so location/status enrichments are published;
- each material state version gets a deterministic event ID;
- a SQLite outbox survives short network failures;
- delivered versions are remembered locally to avoid repeated sends.

Run with:
    python -m core.live_edge_publisher
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from core.domain.live_contracts import (
    FEDERATED_EVENT_SCHEMA,
    VerificationStatus,
    Visibility,
    validate_federated_event_input,
)

# Eligibility for the public edge feed (type, publication policy, maritime
# compartment, context-noise filtering, GDACS relevance, news corroboration)
# is decided by the one canonical function core.live.projection.
# _public_intel_feature -- the exact rule the VM's mode=humanitarian feed
# applies. The edge deliberately keeps no second copy (docs/fixes.md F-06).
from core.observability import (
    record_publisher_cycle,
    record_publisher_delivery,
    record_publisher_heartbeat,
)

if TYPE_CHECKING:
    from core.intel.store import IntelEvent

logger = logging.getLogger("seacommons.live_edge_publisher")


@dataclass(frozen=True)
class PublisherSettings:
    edge_url: str
    ingest_secret: str
    node_id: str
    outbox_path: Path
    poll_seconds: float = 1.0
    batch_size: int = 25
    scan_limit: int = 200
    # Deliberately longer than the 24h public Live window: the publisher must
    # keep rescanning retired rows long enough to deliver an explicit removal
    # after an edge/VM outage. Scan retention is not public visibility.
    live_window_minutes: int = 8 * 24 * 60
    request_timeout_seconds: float = 8.0
    max_attempts: int = 20
    heartbeat_seconds: float = 60.0
    # Prometheus scrape endpoint for this standalone process. 0 disables it
    # (default), so nothing changes for an existing deployment.
    metrics_port: int = 0

    @classmethod
    def from_env(cls) -> "PublisherSettings":
        return cls(
            edge_url=os.getenv("LIVE_EDGE_INGEST_URL", "").rstrip("/"),
            ingest_secret=os.getenv("LIVE_EDGE_INGEST_SECRET", ""),
            node_id=os.getenv("SEACOMMONS_NODE_ID", os.uname().nodename),
            outbox_path=Path(os.getenv("LIVE_EDGE_OUTBOX_PATH", "./shared/live-edge-outbox.db")),
            poll_seconds=max(0.5, float(os.getenv("LIVE_EDGE_POLL_SECONDS", "1"))),
            batch_size=max(1, int(os.getenv("LIVE_EDGE_BATCH_SIZE", "25"))),
            scan_limit=max(10, int(os.getenv("LIVE_EDGE_SCAN_LIMIT", "200"))),
            live_window_minutes=max(5, int(os.getenv("LIVE_EDGE_WINDOW_MINUTES", str(8 * 24 * 60)))),
            request_timeout_seconds=float(os.getenv("LIVE_EDGE_TIMEOUT_SECONDS", "8")),
            max_attempts=max(1, int(os.getenv("LIVE_EDGE_MAX_ATTEMPTS", "20"))),
            heartbeat_seconds=max(30.0, float(os.getenv("LIVE_EDGE_HEARTBEAT_SECONDS", "60"))),
            metrics_port=max(0, int(os.getenv("LIVE_EDGE_METRICS_PORT", "0"))),
        )

    def validate(self) -> None:
        if not self.edge_url:
            raise RuntimeError("LIVE_EDGE_INGEST_URL is required")
        if not self.ingest_secret:
            raise RuntimeError("LIVE_EDGE_INGEST_SECRET is required")


class Outbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending (
                event_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS delivered (
                event_id TEXT PRIMARY KEY,
                delivered_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pending_ready_idx
                ON pending(next_attempt_at, created_at);
            CREATE INDEX IF NOT EXISTS delivered_at_idx
                ON delivered(delivered_at);
            """
        )
        self.connection.commit()

    def enqueue(self, event_id: str, payload: dict[str, Any]) -> bool:
        if self.connection.execute(
            "SELECT 1 FROM delivered WHERE event_id=?", (event_id,)
        ).fetchone():
            return False
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO pending(event_id,payload,created_at) VALUES(?,?,?)",
            (event_id, json.dumps(payload, separators=(",", ":"), sort_keys=True), now_iso()),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def was_ever_delivered(self, incident_id: str) -> bool:
        """Whether any version of this incident was ever actually sent.

        Used to decide whether a row that no longer qualifies for the
        public feed needs an explicit removal pushed -- most excluded rows
        (AIS spikes, private reports) were never published in the first
        place and don't need one; only a row that flips from published to
        excluded (e.g. a duplicate later marked private) does.
        """
        return self.connection.execute(
            "SELECT 1 FROM delivered WHERE event_id LIKE ? LIMIT 1",
            (f"{incident_id}:%",),
        ).fetchone() is not None

    def ready(self, limit: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM pending WHERE next_attempt_at <= ? "
                "ORDER BY created_at ASC LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        )

    def acknowledge(self, event_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM pending WHERE event_id=?", (event_id,))
            self.connection.execute(
                "INSERT OR REPLACE INTO delivered(event_id,delivered_at) VALUES(?,?)",
                (event_id, now_iso()),
            )

    def fail(self, event_id: str, attempts: int, error: str, max_attempts: int) -> None:
        capped_attempts = min(attempts, max_attempts)
        delay = min(300.0, 2 ** min(capped_attempts, 8))
        self.connection.execute(
            "UPDATE pending SET attempts=?, next_attempt_at=?, last_error=? WHERE event_id=?",
            (attempts, time.time() + delay, error[:500], event_id),
        )
        self.connection.commit()

    def prune(self, retention_hours: int = 48) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
        self.connection.execute("DELETE FROM delivered WHERE delivered_at < ?", (cutoff,))
        self.connection.commit()

    def counts(self) -> dict[str, int]:
        total = self.connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        failed = self.connection.execute("SELECT COUNT(*) FROM pending WHERE attempts > 0").fetchone()[0]
        return {"pending": int(total), "retrying": int(failed)}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_id(incident_id: str, payload: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "type": payload.get("type"),
            "geometry": payload.get("geometry"),
            "confidence": payload.get("confidence"),
            "properties": payload.get("properties"),
            "source_url": payload.get("source_url"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    version = hashlib.sha256(material.encode()).hexdigest()[:20]
    return f"{incident_id}:{version}"


def _event_from_row(row: Any) -> "IntelEvent":
    from core.intel.store import IntelEvent

    return IntelEvent(
        id=str(getattr(row, "id")),
        timestamp_utc=str(getattr(row, "timestamp_utc", "") or ""),
        type=str(getattr(row, "type", "") or ""),
        severity=str(getattr(row, "severity", "") or ""),
        lat=getattr(row, "lat", None),
        lon=getattr(row, "lon", None),
        title=str(getattr(row, "title", "") or ""),
        text=str(getattr(row, "text", "") or ""),
        url=str(getattr(row, "url", "") or ""),
        source=str(getattr(row, "source", "") or ""),
        linked_mmsi=str(getattr(row, "linked_mmsi", "") or ""),
        metadata=dict(getattr(row, "meta", None) or {}),
    )


def public_event_from_row(
    row: Any,
    node_id: str,
    *,
    now: datetime,
    same_source: list[Any],
) -> dict[str, Any] | None:
    """Build an edge payload with the SAME lifecycle semantics as the VM's
    public /api/v1/live/signals feed (core/live/feed.py) — both paths
    import core.intel.lifecycle so a marker can never look "resolved" on one
    and "still active" on the other.
    """
    from core.intel import lifecycle

    event = _event_from_row(row)
    metadata = event.metadata
    event_type = event.type

    # docs/fixes.md F-06: humanitarian eligibility is ONE canonical decision.
    # The edge no longer implements its own source policy (previously an
    # Alarm-Phone-only gate) -- it delegates to the exact function the VM's
    # /api/v1/live/signals?mode=humanitarian feed uses, so "what counts as
    # Humanitarian Live" can never depend on whether the browser is served
    # from the edge or the VM.
    from core.intel.public_policy import SECURITY_MARITIME_DOMAINS, domains_for_mode
    from core.live.projection import _public_intel_feature

    resolved_domain = event.maritime_domain()
    if resolved_domain in SECURITY_MARITIME_DOMAINS or resolved_domain == "safety":
        # The public edge is the humanitarian compartment only; the VM buckets
        # these into `security`/`safety` and excludes them from
        # mode=humanitarian (docs/fixes.md P0.1 -- Safety became its own VM
        # mode, DEFAULT_PUBLIC_MARITIME_DOMAINS now includes it, so it must
        # be excluded explicitly here too or it would leak into the edge's
        # humanitarian-only feed and break VM/edge parity).
        return None
    vm_feature = _public_intel_feature(
        event, allowed_domains=domains_for_mode("humanitarian")
    )
    if vm_feature is None:
        return None
    # kind == "distress" iff event.tier() == "operational" -- the same signal
    # the VM feed's distress lifecycle / window handling keys off.
    is_distress = vm_feature["properties"].get("kind") == "distress"

    from core.intel.public_geometry import public_geometry_and_precision

    incident_id = event.id
    geometry, location_precision = public_geometry_and_precision(event)

    confidence = metadata.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None and not 0 <= confidence <= 1:
        confidence = None

    # docs/updates.md P0.10: canonical HumanitarianIncident state is the
    # public authority when one exists -- same helper core.live.feed uses.
    from core.intel.humanitarian_incident import resolve_public_incident_state

    incident_state = (
        resolve_public_incident_state(event, now=now, same_source=same_source)
        if is_distress else None
    )
    incident_lifecycle = incident_state["lifecycle"] if incident_state else None
    incident_status = incident_state["incident_status"] if incident_state else None
    # Live is a 24-hour operational surface. Resolved/outcome-unknown incidents
    # are explicit removals even when their founding observation is younger.
    expired = is_distress and (
        not lifecycle.is_within_live_window(event, now=now)
        or incident_status in {"resolved", "outcome_unknown"}
    )
    # Same redaction as the VM's public feed (core/live/projection.py,
    # _public_intel_feature): only the tracked account's own reply fields —
    # never the event's private-caller-sourced `text` (already blanked
    # above) or anything else from thread_reposts.
    thread_reposts = metadata.get("thread_reposts")
    repost_count = metadata.get("repost_count")
    from core.domain.visual_category import visual_category_fields

    category = visual_category_fields(
        source=event.source,
        event_type=event.type,
        maritime_domain=event.maritime_domain(),
        humanitarian_case_type=metadata.get("humanitarian_case_type"),
        metadata=metadata,
    )
    properties = {
        # Bump when retention/lifecycle transport semantics change. It is part
        # of the material version hash, forcing every in-window row through
        # the durable edge again after a deploy (including tombstones).
        "live_contract_version": 2,
        "incident_id": incident_id,
        "severity": event.severity,
        "visual_category": category["visual_category"],
        "visual_color": category["visual_color"],
        "category_label": category["category_label"],
        "maritime_domain": event.maritime_domain(),
        "humanitarian_case_type": metadata.get("humanitarian_case_type"),
        "location_status": metadata.get("location_status"),
        "title": event.title,
        # Same public contract as the VM feed: raw source/caller text stays
        # private; verified public self-reply notes are projected separately.
        "text": "",
        "verification_status": metadata.get(
            "verification_status", VerificationStatus.UNVERIFIED_PUBLIC_SOURCE.value
        ),
        "coordinate_source": metadata.get("coordinate_source"),
        "radius_m": metadata.get("location_uncertainty_m"),
        "incident_lifecycle": incident_lifecycle,
        "incident_status": incident_status,
        "reported_at": incident_state["reported_at"] if incident_state else None,
        "last_update_at": incident_state["last_update_at"] if incident_state else None,
        "state_changed_at": incident_state["state_changed_at"] if incident_state else None,
        "resolved_at": incident_state["resolved_at"] if incident_state else None,
        "expired": expired,
        "persons": metadata.get("persons"),
        "linked_mmsi": event.linked_mmsi,
        "last_source_seen_at": metadata.get("last_source_seen_at"),
        "repost_count": repost_count,
        "thread_reposts": [
            {"tweet_id": r.get("tweet_id"), "posted_at": r.get("posted_at"),
             "url": r.get("url"), "kind": r.get("kind"), "note": r.get("note")}
            for r in thread_reposts
        ] if thread_reposts else None,
        "location_precision": location_precision,
        "area_weather_narrowed": metadata.get("area_weather_narrowed"),
    }
    properties = {key: value for key, value in properties.items() if value not in (None, "")}

    payload: dict[str, Any] = {
        "schema": FEDERATED_EVENT_SCHEMA,
        "type": "incident_removed" if expired else (
            "distress_observation" if is_distress else event_type
        ),
        "source": event.source or "unknown",
        "node": node_id,
        "observed_at": event.timestamp_utc or now_iso(),
        "visibility": Visibility.PUBLIC.value,
        "confidence": confidence,
        "geometry": geometry,
        "properties": properties,
        "source_url": event.url or None,
    }
    payload["id"] = _version_id(incident_id, payload)
    return validate_federated_event_input(payload)


def removed_payload(incident_id: str, node_id: str, *, source: str = "unknown") -> dict[str, Any]:
    """A synthetic incident_removed payload for a row still inside the scan
    window but no longer eligible for the public feed (e.g. flipped to
    private after being published) -- without this the edge keeps serving
    whatever version it last received forever, since it never re-scans
    anything itself; collect() is the only thing that ever revisits a row.
    """
    payload: dict[str, Any] = {
        "schema": FEDERATED_EVENT_SCHEMA,
        "type": "incident_removed",
        "source": source,
        "node": node_id,
        "observed_at": now_iso(),
        "visibility": Visibility.PUBLIC.value,
        "confidence": None,
        "geometry": None,
        "properties": {"incident_id": incident_id, "live_contract_version": 2},
        "source_url": None,
    }
    payload["id"] = _version_id(incident_id, payload)
    return validate_federated_event_input(payload)


def signature(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


class LiveEdgePublisher:
    def __init__(self, settings: PublisherSettings) -> None:
        settings.validate()
        self.settings = settings
        self.outbox = Outbox(settings.outbox_path)
        self.running = True
        self.client = httpx.Client(timeout=settings.request_timeout_seconds)
        self.cycles = 0
        self.last_heartbeat_at = 0.0

    def stop(self, *_: Any) -> None:
        self.running = False

    def collect(self) -> int:
        """Rescan only the live window and enqueue unseen material versions.

        Two queries, not one: a high-volume, non-distress source (AIS spike
        detection can post dozens of rows per cycle) would otherwise crowd
        genuine distress reports out of a single "most recent N" scan. The
        second query is exempt from that noise so a real report is never
        starved out of the window by spike volume.
        """
        from core.db.models import IntelEventDB
        from core.db.session import session_scope
        from core.intel import lifecycle

        cutoff = (datetime.now(timezone.utc) - timedelta(
            minutes=self.settings.live_window_minutes
        )).isoformat()
        now = datetime.now(timezone.utc)
        added = 0
        with session_scope() as db:
            general = (
                db.query(IntelEventDB)
                .filter(IntelEventDB.timestamp_utc >= cutoff)
                .order_by(IntelEventDB.timestamp_utc.desc())
                .limit(self.settings.scan_limit)
                .all()
            )
            distress_bearing = (
                db.query(IntelEventDB)
                .filter(IntelEventDB.timestamp_utc >= cutoff)
                .filter(IntelEventDB.type != "ais_spike")
                .order_by(IntelEventDB.timestamp_utc.desc())
                .limit(self.settings.scan_limit)
                .all()
            )
            by_id = {row.id: row for row in general}
            by_id.update({row.id: row for row in distress_bearing})
            rows = list(by_id.values())
            events = [_event_from_row(row) for row in rows]
            by_source: dict[str, list[Any]] = {}
            for event in events:
                by_source.setdefault(event.source, []).append(event)
            for row in reversed(rows):
                same_source = by_source.get(str(getattr(row, "source", "") or ""), [])
                payload = public_event_from_row(row, self.settings.node_id, now=now, same_source=same_source)
                if payload is not None:
                    if self.outbox.enqueue(payload["id"], payload):
                        added += 1
                    continue
                # Row is still within the scan window but no longer public
                # (e.g. a duplicate later marked private) -- if the edge was
                # ever actually sent a version of it, it needs an explicit
                # removal, or it keeps showing the last version forever.
                if self.outbox.was_ever_delivered(row.id):
                    removal = removed_payload(row.id, self.settings.node_id, source=str(row.source or "unknown"))
                    if self.outbox.enqueue(removal["id"], removal):
                        added += 1
        return added

    def deliver(self) -> int:
        delivered = 0
        for row in self.outbox.ready(self.settings.batch_size):
            body = str(row["payload"])
            try:
                response = self.client.post(
                    self.settings.edge_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-SeaCommons-Signature": signature(self.settings.ingest_secret, body),
                        "User-Agent": f"SeaCommons-Node/{self.settings.node_id}",
                    },
                )
                if response.status_code in {200, 202}:
                    self.outbox.acknowledge(str(row["event_id"]))
                    delivered += 1
                    record_publisher_delivery(delivered=True)
                    continue
                raise RuntimeError(f"edge HTTP {response.status_code}: {response.text[:200]}")
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                self.outbox.fail(
                    str(row["event_id"]), attempts, str(exc), self.settings.max_attempts
                )
                record_publisher_delivery(delivered=False)
                logger.warning("Live edge delivery failed for %s: %s", row["event_id"], exc)
        return delivered

    def heartbeat(self, status: str = "active", *, force: bool = False) -> bool:
        """Report collector health independently from the arrival of incidents."""
        now_monotonic = time.monotonic()
        if not force and now_monotonic - self.last_heartbeat_at < self.settings.heartbeat_seconds:
            return False
        self.last_heartbeat_at = now_monotonic
        payload = {
            "source": "live-edge-publisher",
            "node": self.settings.node_id,
            "observed_at": now_iso(),
            "status": status if status in {"active", "degraded", "offline"} else "degraded",
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        heartbeat_url = f"{self.settings.edge_url.rsplit('/', 1)[0]}/heartbeat"
        try:
            response = self.client.post(
                heartbeat_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-SeaCommons-Signature": signature(self.settings.ingest_secret, body),
                    "User-Agent": f"SeaCommons-Node/{self.settings.node_id}",
                },
            )
            if response.status_code in {200, 202}:
                record_publisher_heartbeat(ok=True)
                return True
            raise RuntimeError(f"edge HTTP {response.status_code}: {response.text[:200]}")
        except Exception as exc:
            record_publisher_heartbeat(ok=False)
            logger.warning("Live edge heartbeat failed: %s", exc)
            return False

    def run(self) -> None:
        logger.info(
            "Live-first edge publisher started node=%s poll=%.2fs window=%dm",
            self.settings.node_id,
            self.settings.poll_seconds,
            self.settings.live_window_minutes,
        )
        while self.running:
            started = time.monotonic()
            cycle_status = "active"
            added = 0
            delivered = 0
            try:
                added = self.collect()
                delivered = self.deliver()
                self.cycles += 1
                if self.cycles % 3600 == 0:
                    self.outbox.prune()
                if added or delivered:
                    logger.info(
                        "Live edge cycle: collected=%d delivered=%d outbox=%s",
                        added,
                        delivered,
                        self.outbox.counts(),
                    )
            except Exception:
                cycle_status = "degraded"
                logger.exception("Live edge publisher cycle failed")
            try:
                record_publisher_cycle(
                    outcome="ok" if cycle_status == "active" else "degraded",
                    collected=added,
                    outbox_counts=self.outbox.counts(),
                )
            except Exception:  # metrics must never break the delivery loop
                logger.debug("publisher metric update failed", exc_info=True)
            self.heartbeat(cycle_status)
            remaining = self.settings.poll_seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = PublisherSettings.from_env()
    if settings.metrics_port:
        from prometheus_client import start_http_server

        start_http_server(settings.metrics_port)
        logger.info("Publisher metrics on :%d/metrics", settings.metrics_port)
    publisher = LiveEdgePublisher(settings)
    signal.signal(signal.SIGTERM, publisher.stop)
    signal.signal(signal.SIGINT, publisher.stop)
    publisher.run()


if __name__ == "__main__":
    main()
