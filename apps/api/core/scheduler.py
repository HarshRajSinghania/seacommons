# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seacommons background scheduler.

Runs inside the FastAPI process via APScheduler.  All jobs execute even
when no browser is connected — the systemd service keeps the process alive.

Jobs:
  every 15 min  — check for unprocessed geolocated high/critical intel events → queue drift
  every 15 min  — watch OSINT/AIS source health, alert ops on degraded/offline transitions
  every 30 min  — force-refresh news/RSS sources
  every  1 hr   — IOM Missing Migrants API pull (verified incident coords)
  every  6 hr   — forensic chain builder: link event → drift → forensic packet
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional


def _soon() -> datetime:
    """A first-run time a couple of minutes out, so a slow pull never blocks boot."""
    return datetime.now(timezone.utc) + timedelta(minutes=3)

logger = logging.getLogger(__name__)


def _drift_category_fields(event) -> dict:
    """Canonical visual category for a drift's WS push, from its origin signal."""
    from core.domain.visual_category import visual_category_fields

    meta = getattr(event, "metadata", {}) or {}
    return visual_category_fields(
        source=getattr(event, "source", ""),
        event_type=getattr(event, "type", ""),
        maritime_domain=event.maritime_domain() if hasattr(event, "maritime_domain") else None,
        humanitarian_case_type=meta.get("humanitarian_case_type"),
        metadata=meta,
    )

_scheduler = None
_scheduler_lock = threading.Lock()


# ── Job: drift pending events ─────────────────────────────────────────────────

def _available_ram_mb() -> int:
    """Return available RAM in MB by reading /proc/meminfo (Linux only)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 9999  # unknown → don't block


def _job_drift_pending() -> None:
    """
    Find high/critical intel events with coordinates that have no drift result yet.
    Queues at most 1 drift per run to avoid memory exhaustion on the 1 GB VM.
    Skips if available RAM < 400 MB (drift + CMEMS needs ~600 MB headroom).
    """
    try:
        ram_mb = _available_ram_mb()
        if ram_mb < 400:
            logger.info("Scheduler: skipping drift_pending — only %d MB RAM available", ram_mb)
            return

        from core.intel.store import intel_store
        from core.db.store import list_drift_jobs_for_event

        events = intel_store.events(limit=200)
        pending = []
        for e in events:
            if e.severity not in ("critical", "high"):
                continue
            if e.lat is None or e.lon is None:
                continue
            # Check in-memory metadata first (fast path)
            if e.metadata.get("drift_job_id") is not None:
                continue
            # Check DB for any existing drift linked to this event (survives restarts)
            try:
                existing = list_drift_jobs_for_event(f"intel:{e.id}")
                if existing:
                    # Back-fill metadata so we don't check DB every run
                    e.metadata["drift_job_id"] = existing[0].get("id", "exists")
                    continue
            except Exception:
                pass
            pending.append(e)
            if len(pending) >= 1:  # max 1 per run on 1 GB VM
                break

        if not pending:
            return

        logger.info("Scheduler: queuing %d drift job(s), RAM available: %d MB", len(pending), ram_mb)
        for event in pending:
            _compute_drift_for_event(event)
    except Exception as exc:
        logger.warning("Scheduler drift_pending job failed: %s", exc)


def _compute_drift_for_event(event) -> None:
    """Compute drift for a geolocated intel event and link result in metadata."""
    import uuid
    from core.drift.engine import DriftEngine
    from core.db.store import complete_drift_job, create_drift_job, fail_drift_job
    from core.intel.store import intel_store

    job_id = str(uuid.uuid4())
    time_utc = datetime.now(timezone.utc)
    create_drift_job(
        job_id,
        event_id=f"intel:{event.id}",
        lat=event.lat, lon=event.lon,
        domain="ocean_sar", duration_h=24,
        started_at=time_utc,
    )
    # Mark in metadata so we don't re-queue
    event.metadata["drift_job_id"] = job_id
    event.metadata["drift_status"] = "computing"

    try:
        engine = DriftEngine()
        result = engine.compute(
            lat=event.lat, lon=event.lon,
            time_utc=time_utc, duration_h=24,
            domain="ocean_sar",
            config={"vessel_type": "rubber_boat"},
        )
        complete_drift_job(
            job_id, event_id=f"intel:{event.id}",
            lat=event.lat, lon=event.lon,
            domain="ocean_sar", result=result,
        )
        event.metadata["drift_status"] = "completed"
        event.metadata["drift_job_id"] = job_id
        # Broadcast drift result as a map-ready GeoJSON payload to all WS clients
        intel_store.broadcast_event_update(event.id, {
            "drift_job_id": job_id,
            "drift_status": "completed",
            "drift": {
                "trajectory": result.trajectory,
                "cone_24h": result.cone_24h,
                "impact_point": result.impact_point,
                "origin": {"lat": event.lat, "lon": event.lon},
                "title": event.title[:80],
                "source": event.source,
                # Drift colour inherits the origin signal's category, no severity.
                **_drift_category_fields(event),
            },
        })
        logger.info("Scheduled drift completed: event=%s job=%s", event.id, job_id)

        # Auto-build forensic chain
        _build_forensic_chain(event, job_id, result)

    except Exception as exc:
        fail_drift_job(job_id, event_id=f"intel:{event.id}",
                       lat=event.lat, lon=event.lon,
                       domain="ocean_sar", error_message=str(exc))
        event.metadata["drift_status"] = "failed"
        logger.warning("Scheduled drift failed: event=%s error=%s", event.id, exc)


# ── Job: forensic chain builder ───────────────────────────────────────────────

def _build_forensic_chain(event, job_id: str, result) -> None:
    """
    Create a forensic packet linking an intel event to its drift result.
    Signed and stored — traceable audit trail even without user interaction.
    """
    try:
        from core.forensic.logger import sign_and_broadcast

        sign_and_broadcast(
            job_id,
            {
                "source": "osint_scheduler",
                "intel_event_id": event.id,
                "intel_type": event.type,
                "intel_source": event.source,
                "intel_title": event.title[:200],
                "severity": event.severity,
                "auto_computed": True,
            },
            result.model_dump(),
            position={"lat": event.lat, "lon": event.lon, "alt": 0, "source": "intel"},
            classification="sar_osint",
            confidence=0.75,
        )
        logger.info("Forensic chain built for intel event %s", event.id)
    except Exception as exc:
        logger.warning("Forensic chain build failed for %s: %s", event.id, exc)


# ── Job: refresh news sources ─────────────────────────────────────────────────

def _job_refresh_news() -> None:
    """Force a news/RSS re-scan outside the normal intel engine poll cycle."""
    try:
        from core.intel.engine import intel_engine
        if intel_engine._news:
            intel_engine._news.scan()
            logger.debug("Scheduler: news refresh scan complete")
    except Exception as exc:
        logger.warning("Scheduler news refresh failed: %s", exc)


# ── Job: intel source health alerting ─────────────────────────────────────────

# name -> last status we alerted on, so we notify once per transition, not
# once per 15-minute poll.
_source_alert_state: dict[str, str] = {}


def _job_source_health() -> None:
    """
    Watch OSINT/AIS source health (core.intel.source_registry) and alert ops
    via Telegram when a source transitions into degraded/offline, or recovers.
    Without this, a silently-broken free source (e.g. an HTML-scraping
    monitor whose target site changed markup) is only visible to someone
    manually polling /api/v1/intel/sources — nobody would notice in a 24/7
    unattended deployment.
    """
    try:
        from core.intel.source_registry import source_registry
        from core.notifications import telegram

        for src in source_registry.get_all():
            name = src["name"]
            status = src["status"]
            previous = _source_alert_state.get(name)
            if status == previous:
                continue
            if status in ("degraded", "offline"):
                telegram(
                    f"⚠️ SeaCommons intel source \"{name}\" is {status} "
                    f"({src['consecutive_errors']} consecutive errors). "
                    f"Last error: {src['last_error'] or 'n/a'}"
                )
            elif previous in ("degraded", "offline") and status == "active":
                telegram(f"✅ SeaCommons intel source \"{name}\" recovered (active).")
            _source_alert_state[name] = status
    except Exception as exc:
        logger.warning("Scheduler source_health job failed: %s", exc)


# ── Job: IOM Missing Migrants API ────────────────────────────────────────────

def _job_iom_incidents() -> None:
    """
    Pull verified incidents from IOM Missing Migrants API.
    Free, no auth, updated daily.  Only Mediterranean region.
    https://missingmigrants.iom.int/api
    """
    try:
        import hashlib
        import httpx
        from core.intel.store import IntelEvent, intel_store

        url = "https://missingmigrants.iom.int/api/incidents?region=Mediterranean&limit=50"
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        incidents = data if isinstance(data, list) else data.get("data", [])

        added = 0
        for inc in incidents:
            lat = inc.get("latitude") or inc.get("lat")
            lon = inc.get("longitude") or inc.get("lon")
            title = inc.get("title") or inc.get("incident_type") or "IOM Incident"
            date_str = inc.get("date") or inc.get("incident_date") or ""
            dead = inc.get("dead", 0) or 0
            missing = inc.get("missing", 0) or 0
            total = int(dead or 0) + int(missing or 0)

            severity = "low"
            if total > 50:
                severity = "critical"
            elif total > 10:
                severity = "high"
            elif total > 0:
                severity = "medium"

            uid = hashlib.blake2s(f"iom:{title}:{date_str}".encode(), digest_size=8).hexdigest()
            event = IntelEvent(
                id=uid,
                type="iom_incident",
                severity=severity,
                lat=float(lat) if lat else None,
                lon=float(lon) if lon else None,
                title=f"IOM: {title}",
                text=f"Dead: {dead}  Missing: {missing}  Date: {date_str}",
                url=inc.get("url", "https://missingmigrants.iom.int/"),
                source="IOM Missing Migrants",
                linked_mmsi="",
                timestamp_utc=date_str or datetime.now(timezone.utc).isoformat(),
                metadata={"dead": dead, "missing": missing, "incident_id": inc.get("id")},
            )
            if intel_store.add(event, dedup_key=uid):
                added += 1

        if added:
            logger.info("Scheduler: IOM pulled %d new incidents", added)
    except Exception as exc:
        logger.warning("Scheduler IOM pull failed: %s", exc)


# ── Job: full forensic scan ───────────────────────────────────────────────────

def _job_forensic_scan() -> None:
    """
    Scan all completed drift jobs linked to intel events and ensure
    forensic packets exist for each. Runs every 6h as a catch-up.
    """
    try:
        from core.intel.store import intel_store
        from core.db.store import get_drift

        events = intel_store.events(limit=500)
        for event in events:
            job_id = event.metadata.get("drift_job_id")
            if not job_id or event.metadata.get("forensic_done"):
                continue
            drift = get_drift(job_id)
            if drift and drift.get("status") == "completed":
                try:
                    from core.drift.engine import DriftResult
                    result = DriftResult(**{
                        k: drift[k] for k in DriftResult.model_fields if k in drift
                    })
                    _build_forensic_chain(event, job_id, result)
                    event.metadata["forensic_done"] = True
                except Exception as exc:
                    logger.debug("Forensic scan skip %s: %s", event.id, exc)
    except Exception as exc:
        logger.warning("Scheduler forensic_scan failed: %s", exc)


def _job_receiver_discovery() -> None:
    """Refresh public receiver directories without activating new candidates."""
    try:
        from core.radio.discovery import refresh_discovery

        snapshot = refresh_discovery()
        logger.info(
            "Scheduler: receiver discovery refreshed %s candidate(s)",
            snapshot.get("catalogued", 0),
        )
    except Exception:
        logger.warning("Scheduler receiver discovery refresh failed")


def _job_receiver_catalog_recompute() -> None:
    """Seed curated receivers, probe health and recompute automatic mesh scores."""
    try:
        from core.radio.catalog_store import persist_curated_catalog, probe_and_score_catalog

        persist_curated_catalog()
        summary = probe_and_score_catalog()
        logger.info("Scheduler: receiver catalog recomputed %s", summary)
    except Exception:
        logger.warning("Scheduler receiver catalog recompute failed")


def _job_mda_reference_refresh() -> None:
    """Pull the authoritative open reference layers (EEZ / MPA / pipelines /
    platforms). Best-effort — a no-op offline."""
    try:
        from core.mda.reference import reference

        result = reference.refresh()
        logger.info("MDA reference refresh: %s", result)
    except Exception as exc:
        logger.warning("Scheduler mda_reference_refresh failed: %s", exc)


def _job_mda_daily() -> None:
    """Daily MDA data pulls: sanctions lists, GPS-jamming zones, GFW events,
    VIIRS detections, track-store prune. Each sub-step is best-effort."""
    for label, dotted in (
        ("sanctions", "core.mda.identity:refresh_sanctions"),
        ("jamming", "core.mda.jamming:refresh"),
        ("gfw", "core.intel.gfw_monitor:poll_once"),
        ("viirs", "core.intel.viirs_monitor:poll_once"),
        ("warfare", "core.mda.warfare:poll_once"),
    ):
        try:
            mod_name, fn_name = dotted.split(":")
            import importlib

            fn = getattr(importlib.import_module(mod_name), fn_name, None)
            if fn is not None:
                fn()
        except Exception as exc:
            logger.info("MDA daily %s skipped: %s", label, exc)
    try:
        from core.mda.watch import mda_watch

        report = mda_watch.refresh_darkship_cues(limit=6)
        if report["refreshed"] or report["with_unmatched_sar"]:
            logger.info(
                "MDA daily darkship SAR refresh: scanned=%d refreshed=%d "
                "with_unmatched_sar=%d hypotheses=%d",
                report["scanned"], report["refreshed"],
                report["with_unmatched_sar"], report["hypotheses_evaluated"],
            )
    except Exception as exc:
        logger.info("MDA daily darkship SAR refresh skipped: %s", exc)

    try:
        from core.vessels.track_store import track_store

        track_store.prune()
    except Exception as exc:
        logger.info("MDA daily track prune skipped: %s", exc)


# ── Job: satellite evidence enrichment ───────────────────────────────────────

def _job_satellite_enrichment() -> None:
    """Collect a bounded batch of free satellite evidence for recent incidents."""
    try:
        from core.intel.satellite_enrichment import enrich_recent_events

        report = enrich_recent_events(limit=6)
        if report["enriched"] or report["errors"]:
            logger.info(
                "Scheduler satellite enrichment: scanned=%d enriched=%d persisted=%d errors=%d",
                report["scanned"], report["enriched"], report["persisted"], report["errors"],
            )
    except Exception as exc:
        logger.warning("Scheduler satellite enrichment failed: %s", exc)


# ── Job: Humanitarian Live/Play retirement ───────────────────────────────────

def _job_reconcile_humanitarian_incidents() -> None:
    """Persist active->outcome_unknown after 24h of silence.

    Surface retirement is independent from incident outcome: needs_review is
    left unchanged and simply stops projecting to Live after the 24h window.
    """
    try:
        from core.intel.humanitarian_incident import (
            reconcile_recent_incident_lifecycles,
            reconcile_stale_incidents,
        )

        reconciled = reconcile_recent_incident_lifecycles()
        changed = reconcile_stale_incidents()
        if reconciled:
            logger.info(
                "Scheduler: reconciled %d recent humanitarian incident lifecycle(s)",
                reconciled,
            )
        if changed:
            logger.info("Scheduler: retired %d stale humanitarian incident(s) to Play", changed)
    except Exception as exc:
        logger.warning("Scheduler humanitarian reconcile failed: %s", exc)


# ── Job: bounded Humanitarian incident follow-up ─────────────────────────────

def _job_incident_watch() -> None:
    """Claim and execute a tiny due-watch batch; failures stay local."""
    try:
        from core.intel.incident_watch import claim_due_watches, run_claimed_watch

        now = datetime.now(timezone.utc)
        claimed = claim_due_watches(
            now=now,
            limit=3,
            lease_owner=f"apscheduler:{os.getpid()}",
            lease_seconds=240,
        )
        for watch in claimed:
            result = run_claimed_watch(watch["watch_id"], now=now)
            if result.get("executed") and not result.get("success", True):
                logger.info(
                    "IncidentWatch run failed: watch=%s error=%s",
                    watch["watch_id"], result.get("error_class"),
                )
    except Exception as exc:
        logger.warning("Scheduler incident_watch failed: %s", exc)


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start() -> None:
    """Start APScheduler with all jobs. Safe to call multiple times (idempotent)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:
            logger.warning("APScheduler not installed — background jobs disabled. Run: pip install apscheduler")
            return

        scheduler = BackgroundScheduler(timezone="UTC")

        # Auto-drift disabled — triggered manually via the UI button instead.
        # The 1 GB VM cannot sustain background CMEMS drift jobs without swap exhaustion.
        # scheduler.add_job(_job_drift_pending, ...)

        scheduler.add_job(_job_refresh_news,   IntervalTrigger(minutes=30),
                          id="refresh_news",   replace_existing=True,
                          max_instances=1, misfire_grace_time=300)

        scheduler.add_job(_job_source_health,  IntervalTrigger(minutes=15),
                          id="source_health",  replace_existing=True,
                          max_instances=1, misfire_grace_time=300)

        scheduler.add_job(_job_reconcile_humanitarian_incidents, IntervalTrigger(minutes=15),
                          id="humanitarian_reconcile", replace_existing=True,
                          max_instances=1, misfire_grace_time=300, next_run_time=_soon())

        scheduler.add_job(_job_incident_watch, IntervalTrigger(minutes=5),
                          id="incident_watch", replace_existing=True,
                          max_instances=1, misfire_grace_time=300, next_run_time=_soon())

        scheduler.add_job(_job_satellite_enrichment, IntervalTrigger(minutes=30),
                          id="satellite_enrichment", replace_existing=True,
                          max_instances=1, misfire_grace_time=600, next_run_time=_soon())

        scheduler.add_job(_job_iom_incidents,  IntervalTrigger(hours=1),
                          id="iom_incidents",  replace_existing=True,
                          max_instances=1, misfire_grace_time=600)

        scheduler.add_job(_job_forensic_scan,  IntervalTrigger(hours=6),
                          id="forensic_scan",  replace_existing=True,
                          max_instances=1, misfire_grace_time=3600)

        scheduler.add_job(_job_receiver_discovery, IntervalTrigger(hours=6),
                          id="receiver_discovery", replace_existing=True,
                          max_instances=1, misfire_grace_time=1800, next_run_time=_soon())

        scheduler.add_job(_job_receiver_catalog_recompute, IntervalTrigger(minutes=15),
                          id="receiver_catalog_recompute", replace_existing=True,
                          max_instances=1, misfire_grace_time=600, next_run_time=_soon())

        scheduler.add_job(_job_mda_reference_refresh, IntervalTrigger(days=14),
                          id="mda_reference_refresh", replace_existing=True,
                          max_instances=1, misfire_grace_time=3600,
                          next_run_time=_soon())

        scheduler.add_job(_job_mda_daily, IntervalTrigger(hours=24),
                          id="mda_daily", replace_existing=True,
                          max_instances=1, misfire_grace_time=3600,
                          next_run_time=_soon())

        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "Background scheduler started: refresh_news(30m), source_health(15m), "
            "humanitarian_reconcile(15m), incident_watch(5m), satellite_enrichment(30m), "
            "iom_incidents(1h), forensic_scan(6h), receiver_discovery(6h), receiver_catalog(15m), "
            "mda_reference(14d), mda_daily(24h) "
            "[drift: manual-only]"
        )


def stop() -> None:
    global _scheduler
    with _scheduler_lock:
        if _scheduler:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def status() -> dict:
    with _scheduler_lock:
        if _scheduler is None:
            return {"running": False, "jobs": []}
        jobs = []
        for job in _scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return {"running": True, "jobs": jobs}
