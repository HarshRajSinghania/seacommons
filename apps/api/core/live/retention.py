# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable Live retention contract.

A qualified observation that enters Live must stay visible for at least 24h
from its *last* qualified observation -- independent of the bounded
in-memory deque, detector re-firing, process restart, or the case's
operational (resolved/explained) state. Presence-on-Live and operational
state are separate concepts; this module governs only the former.

Pure, DB-free logic lives here so the window arithmetic is unit-testable
without a database. core.intel.store._persist_sync and
core.intel.hypothesis_store.save_hypothesis are the two write-side callers
that stamp the three durable columns this module computes; core.live.feed
is the read-side caller that treats live_expires_at as the sole
authoritative removal signal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

LIVE_RETENTION_WINDOW = timedelta(hours=24)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """SQLite round-trips a DateTime column back as naive (this codebase's
    ambient convention -- see core.intel.lifecycle.parse_utc and friends).
    Every value this module compares may have come straight from such a
    column, so normalise once here rather than at every call site."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

# core.live.feed._SAR_ACTIVITY_LIVE_TTL predates this contract and stays as
# its own narrower TTL for derived SAR-responder-movement cues; it is not
# part of the qualified-AIS-evidence durability fix.
_QUALIFYING_AIS_TYPES = frozenset({"ais_anomaly", "ais_rendezvous"})


def is_qualifying_ais_evidence(event_type: str, metadata: dict) -> bool:
    """Mirror core.live.projection's offshore-evidence gate (the same test
    that decides whether a raw AIS event is shown as Live "maritime
    evidence"). Only events that already pass this gate need a durable
    retention floor -- everything else stays governed by existing rules."""
    if event_type not in _QUALIFYING_AIS_TYPES:
        return False
    if not (
        bool(metadata.get("offshore_anomaly_qualified"))
        and metadata.get("analysis_state") == "evidence_candidate"
    ):
        return False
    anomaly_type = str(metadata.get("anomaly_type") or "").strip().lower()
    if event_type == "ais_anomaly" and anomaly_type in {"gap", "long_gap", "ais_gap"}:
        from core.live.eligibility import ais_gap_has_public_support

        return ais_gap_has_public_support(metadata)
    return True


def live_case_key(event_type: str, metadata: dict, linked_mmsi: str) -> str | None:
    """Group repeated qualifying observations of the same subject/anomaly
    family so a later one extends, rather than restarts, the window."""
    mmsi = str(linked_mmsi or metadata.get("mmsi") or "").strip()
    if not mmsi:
        return None
    family = str(metadata.get("anomaly_type") or metadata.get("episode_family") or event_type)
    return f"{event_type}:{family}:{mmsi}"


def compute_retention_window(
    *,
    prior_entered_at: datetime | None,
    prior_expires_at: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime, datetime]:
    """Returns (live_entered_at, last_qualified_observation_at, live_expires_at).

    A still-live prior window (prior_expires_at >= now) carries its original
    entered_at forward -- the case has been continuously visible, not
    re-entering Live. Anything else (first qualification, or a window that
    had already lapsed) starts a fresh entered_at at ``now``.
    """
    prior_entered_at = _as_aware_utc(prior_entered_at)
    prior_expires_at = _as_aware_utc(prior_expires_at)
    now = _as_aware_utc(now) or now
    if prior_entered_at is not None and prior_expires_at is not None and prior_expires_at >= now:
        entered_at = prior_entered_at
    else:
        entered_at = now
    return entered_at, now, now + LIVE_RETENTION_WINDOW


def is_live_retained(live_expires_at: datetime | None, *, now: datetime) -> bool:
    """None means the row predates this contract (or was never a qualifying
    observation) -- callers decide their own fallback; this only answers
    the question for a row that does carry an expiry."""
    live_expires_at = _as_aware_utc(live_expires_at)
    if live_expires_at is None:
        return False
    now = _as_aware_utc(now) or now
    return now < live_expires_at
