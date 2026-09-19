# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public aggregate status for the SeaCommons canonical pipeline.

Only counts and freshness timestamps are exposed. No raw payloads, source
identifiers, private receiver endpoints, vessel identities or operator data.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func

from core.db.models import (
    IntelEventDB,
    InvestigationHypothesisDB,
    MaritimeEpisodeDB,
    RadioBurstDB,
    RadioEventDB,
    SatelliteObservationDB,
    SourceObservationDB,
    VesselTrackDB,
)
from core.db.session import session_scope
from core.live.feed import public_signal_collection

router = APIRouter(tags=["status"])

_STATUS_CACHE_TTL_S = 30.0
_status_cache_lock = threading.Lock()
_status_cache: tuple[float, int, dict[str, Any]] | None = None


def _naive_utc_cutoff(hours: int) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def peek_public_status(hours: int = 24) -> dict[str, Any] | None:
    """Return the current cached public status without triggering recomputation."""
    now_mono = time.monotonic()
    with _status_cache_lock:
        if (
            _status_cache is not None
            and _status_cache[1] == hours
            and now_mono - _status_cache[0] < _STATUS_CACHE_TTL_S
        ):
            return _status_cache[2]
    return None


def build_public_status(hours: int = 24) -> dict[str, Any]:
    global _status_cache
    now_mono = time.monotonic()
    with _status_cache_lock:
        if (
            _status_cache is not None
            and _status_cache[1] == hours
            and now_mono - _status_cache[0] < _STATUS_CACHE_TTL_S
        ):
            return _status_cache[2]

    cutoff = _naive_utc_cutoff(hours)
    with session_scope() as db:
        raw_total = (
            db.query(func.count(SourceObservationDB.observation_id))
            .filter(SourceObservationDB.received_at >= cutoff)
            .scalar()
            or 0
        )
        parsed_total = (
            db.query(func.count(IntelEventDB.id))
            .filter(IntelEventDB.received_at >= cutoff)
            .scalar()
            or 0
        )
        derived_total_raw = (
            db.query(func.count(IntelEventDB.id))
            .filter(
                IntelEventDB.received_at >= cutoff,
                IntelEventDB.type.in_((
                    "ais_anomaly", "ais_rendezvous", "correlated_alert",
                    "dark_candidate", "vessel_identity", "sar_model",
                )),
            )
            .scalar()
            or 0
        )
        anomaly_type = IntelEventDB.meta["anomaly_type"].as_string()
        legacy_gap_context = (
            db.query(func.count(IntelEventDB.id))
            .filter(
                IntelEventDB.received_at >= cutoff,
                IntelEventDB.type == "ais_anomaly",
                IntelEventDB.source == "ais",
                IntelEventDB.id.like("aisanom:%"),
                anomaly_type == "gap",
            )
            .scalar()
            or 0
        )
        derived_total = max(0, int(derived_total_raw) - int(legacy_gap_context))
        episodes_total = (
            db.query(func.count(MaritimeEpisodeDB.episode_id))
            .filter(
                MaritimeEpisodeDB.updated_at >= cutoff,
                MaritimeEpisodeDB.episode_family != "unclassified_episode",
            )
            .scalar()
            or 0
        )
        corroborated_total = (
            db.query(func.count(MaritimeEpisodeDB.episode_id))
            .filter(
                MaritimeEpisodeDB.updated_at >= cutoff,
                MaritimeEpisodeDB.episode_family != "unclassified_episode",
                MaritimeEpisodeDB.verification_status == "multi_source_corroborated",
            )
            .scalar()
            or 0
        )
        hypotheses_total = (
            db.query(func.count(InvestigationHypothesisDB.hypothesis_id))
            .filter(InvestigationHypothesisDB.updated_at >= cutoff)
            .scalar()
            or 0
        )
        review_ready_total = (
            db.query(func.count(InvestigationHypothesisDB.hypothesis_id))
            .filter(
                InvestigationHypothesisDB.updated_at >= cutoff,
                InvestigationHypothesisDB.state.in_(("review_ready", "assessed", "published")),
            )
            .scalar()
            or 0
        )
        ais_fixes = (
            db.query(func.count(VesselTrackDB.id))
            .filter(VesselTrackDB.ts >= cutoff)
            .scalar()
            or 0
        )
        radio_bursts = (
            db.query(func.count(RadioBurstDB.burst_id))
            .filter(RadioBurstDB.created_at >= cutoff)
            .scalar()
            or 0
        )
        radio_events = (
            db.query(func.count(RadioEventDB.event_id))
            .filter(RadioEventDB.created_at >= cutoff)
            .scalar()
            or 0
        )
        satellite_observations = (
            db.query(func.count(SatelliteObservationDB.observation_id))
            .filter(SatelliteObservationDB.discovered_at >= cutoff)
            .scalar()
            or 0
        )
        active_sources = (
            db.query(func.count(func.distinct(SourceObservationDB.source_name)))
            .filter(SourceObservationDB.received_at >= cutoff)
            .scalar()
            or 0
        )
        newest_raw = db.query(func.max(SourceObservationDB.received_at)).scalar()
        newest_parsed = db.query(func.max(IntelEventDB.received_at)).scalar()
        newest_analysis = max(
            (
                db.query(func.max(MaritimeEpisodeDB.updated_at)).scalar(),
                db.query(func.max(InvestigationHypothesisDB.updated_at)).scalar(),
            ),
            key=lambda value: value or datetime.min,
        )

    try:
        live = public_signal_collection(limit=500, days=30, mode="all")
        live_features = list(live.get("features") or [])
    except Exception:
        live_features = []

    humanitarian_live = 0
    maritime_live = 0
    operational_live = 0
    for feature in live_features:
        props = feature.get("properties") or {}
        domain = str(props.get("maritime_domain") or "").lower()
        if domain == "humanitarian":
            humanitarian_live += 1
        else:
            maritime_live += 1
        if str(props.get("tier") or "").lower() == "operational":
            operational_live += 1

    status = {
        "service": "SeaCommons",
        "status": "operational",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": hours,
        "live": {
            "total": len(live_features),
            "operational": operational_live,
            "humanitarian": humanitarian_live,
            "maritime": maritime_live,
        },
        "pipeline": {
            "raw_observations": int(raw_total),
            "parsed_events": int(parsed_total),
            "derived_cues": int(derived_total),
            "analysis_outputs": int(episodes_total + hypotheses_total),
            "maritime_episodes": int(episodes_total),
            "investigation_hypotheses": int(hypotheses_total),
            "corroborated_episodes": int(corroborated_total),
            "review_ready": int(review_ready_total),
        },
        "sensor_activity": {
            "ais_fixes": int(ais_fixes),
            "radio_bursts": int(radio_bursts),
            "radio_events": int(radio_events),
            "satellite_observations": int(satellite_observations),
            "active_source_names": int(active_sources),
        },
        "freshness": {
            "raw_observation": _iso(newest_raw),
            "parsed_event": _iso(newest_parsed),
            "analysis_output": _iso(newest_analysis),
        },
        "semantics": {
            "raw_observations": "Immutable source envelopes received during the selected window.",
            "parsed_events": "Normalized IntelEvent records produced during the selected window.",
            "derived_cues": "Rule/model outputs such as AIS integrity, rendezvous or dark-gap cues; not findings.",
            "analysis_outputs": "Recognized maritime episodes plus investigation hypotheses updated during the selected window.",
            "corroborated_episodes": "Episodes supported by at least two independent evidence lineages.",
            "review_ready": "Corroborated investigations ready for human review; not findings of illegality.",
            "live": "Cases/signals that currently satisfy the public Live publication gate.",
        },
    }
    with _status_cache_lock:
        _status_cache = (now_mono, hours, status)
    return status


@router.get("/api/v1/status")
def public_status(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    return build_public_status(hours)


@router.get("/status", include_in_schema=False)
def public_status_short(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    return build_public_status(hours)
