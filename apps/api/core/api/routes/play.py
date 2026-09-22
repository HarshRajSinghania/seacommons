"""Public Play: privacy-safe temporal reconstruction of incidents."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from threading import Lock
from time import monotonic
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from core.domain.incident_taxonomy import taxonomy_fields
from core.intel.humanitarian_incident import public_incident_status
from core.intel.lifecycle import parse_utc
from core.intel.public_policy import domains_for_mode
from core.intel.store import IntelEvent
from core.live.projection import (
    dedupe_public_case_items,
    is_useful_public_case_feature,
    public_archive_event_types,
    public_intel_feature,
)

router = APIRouter(prefix="/api/v1/play", tags=["play"])


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    parsed = parse_utc(str(value))
    return parsed.isoformat() if parsed else str(value)


def _status_for_row(row, *, now: datetime) -> str:
    return public_incident_status({
        "lifecycle": row.lifecycle,
        "incident_status": row.incident_status,
        "last_update_at": row.last_update_at,
    }, now=now)


def _incident_projection(row, event, *, now: datetime) -> dict[str, Any]:
    status = _status_for_row(row, now=now)
    geometry = None
    if event is not None and event.lat is not None and event.lon is not None:
        geometry = {"type": "Point", "coordinates": [event.lon, event.lat]}
    taxonomy = taxonomy_fields(
        event_type=event.type if event is not None else "distress",
        maritime_domain="sar",
        humanitarian_case_type=row.case_type,
        metadata=dict(event.meta or {}) if event is not None else {},
    )
    return {
        "incident_id": row.incident_id,
        "incident_status": status,
        "surface": "play",
        "case_type": row.case_type,
        **taxonomy,
        "reported_at": row.reported_at,
        "last_update_at": row.last_update_at,
        "state_changed_at": _iso(row.state_changed_at),
        "resolved_at": _iso(row.resolved_at),
        "title": event.title if event is not None else "Humanitarian incident",
        "source": event.source if event is not None else None,
        "geometry": geometry,
        "domain": "humanitarian",
    }


def _generic_maritime_status(event) -> str:
    meta = dict(event.meta or {})
    raw = str(meta.get("incident_status") or meta.get("lifecycle") or "").lower()
    if raw == "resolved":
        return "resolved"
    if raw == "needs_review":
        return "needs_review"
    return "outcome_unknown"


def _intel_event_from_row(event) -> IntelEvent:
    meta = dict(event.meta or {})
    if event.maritime_domain and not meta.get("maritime_domain"):
        meta["maritime_domain"] = event.maritime_domain
    return IntelEvent(
        id=event.id, timestamp_utc=event.timestamp_utc, type=event.type,
        severity=event.severity, lat=event.lat, lon=event.lon,
        title=event.title or "", text=event.text or "", url=event.url or "",
        source=event.source or "", linked_mmsi=event.linked_mmsi or "", metadata=meta,
    )


def _is_public_catalog_maritime(event) -> bool:
    """Whether a durable maritime/intel row belongs in the public Play catalog.

    Catalog membership is a publication/privacy decision, not a geometry or
    age decision. Unpositioned records remain searchable/listable and current
    records may coexist with Live; only the map requires geometry.
    """
    feature = public_intel_feature(
        _intel_event_from_row(event), allowed_domains=domains_for_mode("all")
    )
    return is_useful_public_case_feature(feature)


def _generic_maritime_projection(event) -> dict[str, Any]:
    geometry = None
    if event.lat is not None and event.lon is not None:
        geometry = {"type": "Point", "coordinates": [event.lon, event.lat]}
    meta = dict(event.meta or {})
    taxonomy = taxonomy_fields(
        event_type=event.type,
        maritime_domain=event.maritime_domain,
        humanitarian_case_type=event.humanitarian_case_type,
        metadata=meta,
    )
    return {
        "incident_id": event.id,
        "incident_status": _generic_maritime_status(event),
        "surface": "play",
        "case_type": event.type,
        **taxonomy,
        "anomaly_type": str(meta.get("anomaly_type") or ""),
        "analysis_state": str(meta.get("analysis_state") or ""),
        "verification_status": str(meta.get("verification_status") or ""),
        "reported_at": event.timestamp_utc,
        "last_update_at": event.timestamp_utc,
        "state_changed_at": None,
        "resolved_at": None,
        "title": event.title or "Maritime incident",
        "source": event.source,
        "geometry": geometry,
        "domain": "maritime",
    }


# The public promotion boundary: candidate/collecting/expired remain durable
# internal investigation state. review_ready+ are public. Rejected hypotheses
# are private by default and become a public negative case only after an
# explicit preservation decision recorded in review metadata/reason codes.
_PUBLIC_PLAY_INVESTIGATION_STATES = frozenset({"review_ready", "assessed", "published"})
_NEGATIVE_CASE_PRESERVE_REASON = "PRESERVE_NEGATIVE_CASE"


def _is_preserved_rejected_case(hypothesis) -> bool:
    return bool(
        hypothesis.state == "rejected"
        and hypothesis.explicit_review_done
        and _NEGATIVE_CASE_PRESERVE_REASON in set(hypothesis.reason_codes or ())
    )


def _is_public_play_investigation(hypothesis) -> bool:
    if not (hypothesis.hypothesis_type and hypothesis.state):
        return False
    return (
        hypothesis.state in _PUBLIC_PLAY_INVESTIGATION_STATES
        or _is_preserved_rejected_case(hypothesis)
    )


def _archive_decision(hypothesis) -> str:
    if _is_preserved_rejected_case(hypothesis):
        return "rejected"
    if hypothesis.state in {"candidate", "collecting", "rejected", "expired"}:
        return "internal_only"
    return "promoted"


def _investigation_projection(hypothesis, episode=None, evidence_event=None) -> dict[str, Any]:
    label = {
        "dark_transit": "Dark transit",
        "position_spoofing": "Position integrity",
        "covert_rendezvous": "Covert rendezvous",
        "infrastructure_pattern": "Infrastructure pattern",
    }.get(hypothesis.hypothesis_type, "Maritime")
    suffix = {
        "candidate": "candidate",
        "collecting": "evidence collection",
        "review_ready": "investigation",
        "assessed": "assessed investigation",
        "published": "published investigation",
        "rejected": "rejected hypothesis",
    }.get(hypothesis.state, "investigation")
    title = f"{label} {suffix}"
    geometry = episode.geometry if episode is not None else None
    if (
        geometry is None
        and evidence_event is not None
        and evidence_event.lat is not None
        and evidence_event.lon is not None
    ):
        geometry = {
            "type": "Point",
            "coordinates": [evidence_event.lon, evidence_event.lat],
        }
    reported_at = (
        _iso(episode.start_at)
        if episode is not None
        else _iso(evidence_event.timestamp_utc)
        if evidence_event is not None
        else _iso(hypothesis.created_at)
    )
    episode_end = _iso(episode.end_at) if episode is not None else None
    taxonomy = taxonomy_fields(
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        metadata={
            "evidence_stage": hypothesis.evidence_stage,
            "verification_status": (
                episode.verification_status if episode is not None else ""
            ),
        },
        hypothesis_type=hypothesis.hypothesis_type,
    )
    independence_groups = list(episode.independence_groups or []) if episode is not None else []
    verification_status = str(episode.verification_status or "") if episode is not None else ""
    # Epistemically strict: corroborated means >=2 independent evidence
    # lineages, never "assessed"/"confirmed" review state and never a raw
    # evidence-link/detector count. core.intel.fusion.verification_for_event_ids
    # is the canonical classifier that already produces both of these fields
    # on the episode; Play only ever reads them, never re-derives its own.
    corroborated = (
        verification_status == "multi_source_corroborated" or len(independence_groups) >= 2
    )
    canonical_id = (
        str(episode.episode_id) if episode is not None else hypothesis.hypothesis_id
    )
    return {
        "incident_id": canonical_id,
        "episode_id": str(episode.episode_id) if episode is not None else None,
        "hypothesis_id": hypothesis.hypothesis_id,
        "incident_status": hypothesis.state,
        "surface": "play",
        "case_type": hypothesis.hypothesis_type,
        **taxonomy,
        "reported_at": reported_at,
        "last_update_at": _iso(hypothesis.updated_at) or episode_end,
        "state_changed_at": _iso(hypothesis.updated_at),
        "resolved_at": None,
        "title": title,
        "source": "SeaCommons evidence engine",
        "geometry": geometry,
        "domain": "maritime",
        "analysis_state": hypothesis.state,
        "investigation": True,
        "hypothesis_state": hypothesis.state,
        "evidence_stage": hypothesis.evidence_stage,
        "verification_status": verification_status,
        "corroborated": corroborated,
        "independence_groups": independence_groups,
        "evidence_count": len(hypothesis.evidence_links or ()),
        "archive_decision": _archive_decision(hypothesis),
        "review_boundary_crossed": hypothesis.state in {
            "review_ready", "assessed", "published"
        },
        "episode_present": episode is not None,
        "archive_source": "episode" if episode is not None else "legacy_hypothesis",
        "reason_codes": list(hypothesis.reason_codes or []),
        "counter_indicators": list(hypothesis.counter_indicators or []),
    }


_PLAY_COUNTS_TTL_S = 60.0
_PLAY_CATALOG_TTL_S = 60.0
_play_counts_cache: dict[str, Any] = {}
_play_counts_lock = Lock()
_play_catalog_cache: dict[str, Any] = {}
_play_catalog_lock = Lock()


def _compute_play_catalog() -> list[dict[str, Any]]:
    """Compute the complete public SeaCommons catalog used by Play.

    Privacy/publication policy is evaluated before pagination. This is
    intentionally exact: a high-volume block of private/non-public rows can
    never crowd older public history out of the result.
    """
    from core.db.models import (
        HumanitarianIncidentDB,
        IntelEventDB,
        InvestigationHypothesisDB,
        MaritimeEpisodeDB,
    )
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    combined: list[dict[str, Any]] = []
    with session_scope() as db:
        from sqlalchemy import func
        from core.db.models import DriftResultDB, SatelliteObservationDB

        human_rows = db.query(HumanitarianIncidentDB).all()
        human_ids = {row.incident_id for row in human_rows}
        for row in human_rows:
            event = db.get(IntelEventDB, row.incident_id)
            event_meta = dict(event.meta or {}) if event is not None else {}
            if event_meta.get("translation_of") or event_meta.get("publication_status") == "internal":
                continue
            combined.append(_incident_projection(row, event, now=now))

        publication_status = IntelEventDB.meta["publication_status"].as_string()
        rows = (
            db.query(IntelEventDB)
            .filter(
                IntelEventDB.type.in_(public_archive_event_types()),
                (publication_status.is_(None)) | (publication_status != "internal"),
            )
            .yield_per(1000)
        )
        for event in rows:
            if event.id in human_ids:
                continue
            if _is_public_catalog_maritime(event):
                combined.append(_generic_maritime_projection(event))

        # Semantic dedupe is intentionally limited to public incident/event
        # rows. InvestigationHypothesis IDs are already canonical and unique,
        # and Play must retain every lifecycle row rather than comparing
        # thousands of hypotheses pairwise.
        combined = dedupe_public_case_items(combined)

        investigations = (
            db.query(InvestigationHypothesisDB, MaritimeEpisodeDB)
            .outerjoin(
                MaritimeEpisodeDB,
                InvestigationHypothesisDB.episode_id == MaritimeEpisodeDB.episode_id,
            )
            .filter(
                InvestigationHypothesisDB.state.in_(
                    (*_PUBLIC_PLAY_INVESTIGATION_STATES, "rejected")
                )
            )
            .all()
        )
        missing_episode_lookup_ids: set[str] = set()
        for hypothesis, episode in investigations:
            if episode is not None:
                continue
            for evidence_id in hypothesis.evidence_links or ():
                raw = str(evidence_id or "").strip()
                if not raw:
                    continue
                missing_episode_lookup_ids.add(raw)
                if raw.startswith("ais:"):
                    missing_episode_lookup_ids.add(raw.removeprefix("ais:"))

        evidence_by_id: dict[str, Any] = {}
        lookup_ids = sorted(missing_episode_lookup_ids)
        for start in range(0, len(lookup_ids), 500):
            chunk = lookup_ids[start:start + 500]
            for event in db.query(IntelEventDB).filter(IntelEventDB.id.in_(chunk)).all():
                evidence_by_id[event.id] = event

        for hypothesis, episode in investigations:
            if not _is_public_play_investigation(hypothesis):
                continue
            evidence_event = None
            if episode is None:
                for evidence_id in hypothesis.evidence_links or ():
                    raw = str(evidence_id or "").strip()
                    evidence_event = evidence_by_id.get(raw)
                    if evidence_event is None and raw.startswith("ais:"):
                        evidence_event = evidence_by_id.get(raw.removeprefix("ais:"))
                    if evidence_event is not None:
                        break
            combined.append(
                _investigation_projection(
                    hypothesis, episode, evidence_event=evidence_event
                )
            )

        public_ids = [str(item["incident_id"]) for item in combined]
        drift_counts: dict[str, int] = {}
        satellite_counts: dict[str, int] = {}
        if public_ids:
            drift_ids = [*public_ids, *(f"intel:{incident_id}" for incident_id in public_ids)]
            for event_id, count in (
                db.query(DriftResultDB.event_id, func.count(DriftResultDB.drift_id))
                .filter(DriftResultDB.status == "completed", DriftResultDB.event_id.in_(drift_ids))
                .group_by(DriftResultDB.event_id)
                .all()
            ):
                if not event_id:
                    continue
                incident_id = str(event_id)[6:] if str(event_id).startswith("intel:") else str(event_id)
                drift_counts[incident_id] = drift_counts.get(incident_id, 0) + int(count or 0)
            for incident_id, count in (
                db.query(SatelliteObservationDB.incident_id, func.count(SatelliteObservationDB.observation_id))
                .filter(SatelliteObservationDB.incident_id.in_(public_ids))
                .group_by(SatelliteObservationDB.incident_id)
                .all()
            ):
                satellite_counts[str(incident_id)] = int(count or 0)
            # Associated satellite detections are linked directly to the
            # canonical MaritimeEpisode, even when their originating incident
            # key is an evidence child rather than the public parent ID.
            for episode_id, count in (
                db.query(SatelliteObservationDB.episode_id, func.count(SatelliteObservationDB.observation_id))
                .filter(
                    SatelliteObservationDB.episode_id.in_(public_ids),
                    SatelliteObservationDB.association_status == "strong",
                )
                .group_by(SatelliteObservationDB.episode_id)
                .all()
            ):
                if episode_id:
                    key = str(episode_id)
                    satellite_counts[key] = satellite_counts.get(key, 0) + int(count or 0)

        for item in combined:
            incident_id = str(item["incident_id"])
            satellite_count = satellite_counts.get(incident_id, 0)
            item["evidence_counts"] = {
                "drift": drift_counts.get(incident_id, 0),
                "satellite": satellite_count,
            }
            item["has_satellite"] = satellite_count > 0

    combined.sort(
        key=lambda item: str(item.get("last_update_at") or item.get("reported_at") or ""),
        reverse=True,
    )
    return combined


def _play_cache_scope() -> str:
    current_test = os.getenv("PYTEST_CURRENT_TEST", "")
    return current_test.split(" (", 1)[0] if current_test else "runtime"


def _get_play_catalog() -> list[dict[str, Any]]:
    """Return one shared archive snapshot for counts and paginated Play reads."""
    scope = _play_cache_scope()
    now_mono = monotonic()
    cached = _play_catalog_cache.get("payload")
    cached_at = float(_play_catalog_cache.get("at") or 0.0)
    cached_scope = str(_play_catalog_cache.get("scope") or "")
    if (
        cached is not None
        and cached_scope == scope
        and now_mono - cached_at < _PLAY_CATALOG_TTL_S
    ):
        return cached

    with _play_catalog_lock:
        now_mono = monotonic()
        cached = _play_catalog_cache.get("payload")
        cached_at = float(_play_catalog_cache.get("at") or 0.0)
        cached_scope = str(_play_catalog_cache.get("scope") or "")
        if (
            cached is not None
            and cached_scope == scope
            and now_mono - cached_at < _PLAY_CATALOG_TTL_S
        ):
            return cached
        payload = _compute_play_catalog()
        _play_catalog_cache["payload"] = payload
        _play_catalog_cache["at"] = monotonic()
        _play_catalog_cache["scope"] = scope
        return payload


def _compute_play_counts() -> dict[str, Any]:
    """Compute an exact public catalog snapshot using the same eligibility as the index."""
    now = datetime.now(timezone.utc)
    catalog = _get_play_catalog()
    humanitarian_count = sum(1 for item in catalog if item.get("domain") == "humanitarian")
    maritime_count = sum(1 for item in catalog if item.get("domain") == "maritime")
    investigation_count = sum(1 for item in catalog if item.get("investigation") is True)
    return {
        "total_count": len(catalog),
        "humanitarian_count": humanitarian_count,
        "maritime_count": maritime_count,
        "investigation_count": investigation_count,
        "generated_at": now.isoformat(),
    }


@router.get("/counts")
def play_counts():
    """Exact archive snapshot, isolated from the async request loop."""
    scope = _play_cache_scope()
    now_mono = monotonic()
    cached = _play_counts_cache.get("payload")
    cached_at = float(_play_counts_cache.get("at") or 0.0)
    cached_scope = str(_play_counts_cache.get("scope") or "")
    if (
        cached is not None
        and cached_scope == scope
        and now_mono - cached_at < _PLAY_COUNTS_TTL_S
    ):
        return cached

    with _play_counts_lock:
        now_mono = monotonic()
        cached = _play_counts_cache.get("payload")
        cached_at = float(_play_counts_cache.get("at") or 0.0)
        cached_scope = str(_play_counts_cache.get("scope") or "")
        if (
            cached is not None
            and cached_scope == scope
            and now_mono - cached_at < _PLAY_COUNTS_TTL_S
        ):
            return cached
        payload = _compute_play_counts()
        _play_counts_cache["payload"] = payload
        _play_counts_cache["at"] = monotonic()
        _play_counts_cache["scope"] = scope
        return payload


@router.get("/incidents")
def play_incidents(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    from core.db.models import IncidentTransitionDB
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    combined = _get_play_catalog()
    page = [dict(item) for item in combined[offset:offset + limit]]

    page_human_ids = [item["incident_id"] for item in page if item.get("domain") == "humanitarian"]
    history: dict[str, list[dict[str, Any]]] = {incident_id: [] for incident_id in page_human_ids}
    if page_human_ids:
        with session_scope() as db:
            transitions = (
                db.query(IncidentTransitionDB)
                .filter(IncidentTransitionDB.incident_id.in_(page_human_ids))
                .order_by(IncidentTransitionDB.transition_at.asc())
                .all()
            )
            for transition in transitions:
                history.setdefault(transition.incident_id, []).append({
                    "at": _iso(transition.transition_at),
                    "from_state": transition.from_state,
                    "to_state": transition.to_state,
                })
    for item in page:
        item["status_history"] = history.get(item["incident_id"], [])

    next_offset = offset + len(page) if len(combined) > offset + len(page) else None
    return {
        "incidents": page,
        "offset": offset,
        "next_offset": next_offset,
        "total_count": len(combined),
        "generated_at": now.isoformat(),
    }



def _thread_item(incident_id: str, repost: dict, *, reported_at: str | None) -> dict[str, Any] | None:
    at = _iso(repost.get("posted_at"))
    if not at:
        return None
    note = str(repost.get("note") or "").strip()
    from core.intel.geoextract import is_concluded_incident

    if note and is_concluded_incident(note):
        item_type = "resolution"
    else:
        reported = parse_utc(reported_at or "")
        posted = parse_utc(at)
        item_type = (
            "attending_news"
            if reported and posted and (posted - reported).total_seconds() >= 24 * 3600
            else "update"
        )
    return {
        "id": str(repost.get("tweet_id") or f"update:{incident_id}:{at}"),
        "at": at,
        "type": item_type,
        "source": "source_update",
        "title": note or "Source update",
        "geometry": None,
        "properties": {
            "url": repost.get("url"),
            "kind": repost.get("kind"),
        },
    }


def _transition_item(row) -> dict[str, Any]:
    return {
        "id": row.transition_id,
        "at": _iso(row.transition_at),
        "type": "resolution" if row.to_state == "resolved" else "status",
        "source": "incident_state",
        "title": f"Status: {row.to_state}",
        "geometry": None,
        "properties": {
            "from_state": row.from_state,
            "to_state": row.to_state,
            "reason_code": row.reason_code,
            "review_required": bool(row.review_required),
        },
    }


def _sar_mission_item(row) -> dict[str, Any]:
    value = dict(row.value or {})
    asset = value.get("asset_name") or value.get("asset_identity") or "SAR responder"
    mission_state = str(value.get("mission_state") or "insufficient_evidence")
    return {
        "id": row.assessment_id,
        "at": _iso(row.updated_at or row.created_at),
        "type": "sar_mission_assessment",
        "source": "SeaCommons AIS analysis",
        "title": f"SAR asset assessment — {asset}",
        "geometry": None,
        "properties": {
            "asset_identity": value.get("asset_identity"),
            "asset_name": value.get("asset_name"),
            "org": value.get("org"),
            "mission_state": mission_state,
            "reason_codes": list(value.get("reason_codes") or []),
            "distance_nm": value.get("distance_nm"),
            "distance_to_drift_nm": value.get("distance_to_drift_nm"),
            "operational_distance_nm": value.get("operational_distance_nm"),
            "operational_target": value.get("operational_target"),
            "drift_target": value.get("drift_target"),
            "current_drift_id": value.get("current_drift_id"),
            "sar_zones": list(value.get("sar_zones") or []),
            "in_named_srr": bool(value.get("in_named_srr")),
            "in_port_or_land": bool(value.get("in_port_or_land")),
            "heading_toward": bool(value.get("heading_toward")),
            "eta_h": value.get("eta_h"),
            "motion_flags": list(value.get("motion_flags") or []),
            "verification_status": "single_source_observed",
            "evidence_stage": "derived",
            "independence_groups": list(value.get("independence_groups") or ["ais_sensor_lineage"]),
            "confidence": row.confidence,
            "review_state": row.review_state,
            "method_version": row.method_version,
        },
    }


def _drift_item(row) -> dict[str, Any]:
    metadata = dict(row.metadata_json or {})
    return {
        "id": row.drift_id,
        "at": _iso(row.created_at),
        "type": "drift",
        "source": "SeaCommons/OpenDrift",
        "title": "Drift forecast computed",
        "geometry": row.trajectory,
        "properties": {
            "drift_id": row.drift_id,
            "domain": row.domain,
            "model": metadata.get("model") or row.model_version or "OpenDrift",
            "forecast": True,
            "cone_24h": row.cone_24h,
            "impact_point": row.impact_point,
        },
    }


def _radio_item(observation, association) -> dict[str, Any]:
    from core.radio.public_messages import project_public_radio_message

    public = project_public_radio_message(observation)
    geometry = None
    lat, lon = public.get("latitude"), public.get("longitude")
    if lat is not None and lon is not None:
        geometry = {"type": "Point", "coordinates": [lon, lat]}
    return {
        "id": f"radio:{observation.observation_id}",
        "at": _iso(observation.observed_at),
        "type": "radio",
        "source": "Structured maritime radio",
        "title": "DSC radio evidence",
        "geometry": geometry,
        "properties": {
            "kind": public.get("kind"),
            "category": public.get("category"),
            "nature_code": public.get("nature_code"),
            "nature_description": public.get("nature_description"),
            "format": public.get("format"),
            "frequency_hz": public.get("frequency_hz"),
            "match_status": association.match_status,
            "association_confidence": association.confidence,
            "distance_km": association.distance_km,
            "ais_observed_at": association.ais_observed_at,
            "episode_eligible": bool(association.episode_eligible),
            "episode_id": association.episode_id,
            "evidence_role": (
                "corroboration"
                if association.match_status == "strong" and association.episode_id
                else "context"
            ),
            "source_lineage": "radio_transmission",
            "association_uses_lineage": "ais_sensor_lineage",
        },
    }


@router.get("/incidents/{incident_id}/timeline")
def play_incident_timeline(incident_id: str):
    from sqlalchemy import or_

    from core.db.models import (
        AssessmentDB,
        DriftResultDB,
        HumanitarianIncidentDB,
        IncidentTransitionDB,
        IntelEventDB,
        InvestigationHypothesisDB,
        MaritimeEpisodeDB,
        RadioAISAssociationDB,
        SatelliteObservationDB,
        SourceObservationDB,
    )
    from core.db.session import session_scope

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        hypothesis = db.get(InvestigationHypothesisDB, incident_id)
        if hypothesis is None:
            # New canonical URLs use MaritimeEpisode. Keep old hypothesis URLs
            # resolving to the same dossier during the transition.
            hypothesis = (
                db.query(InvestigationHypothesisDB)
                .filter(InvestigationHypothesisDB.episode_id == incident_id)
                .order_by(InvestigationHypothesisDB.updated_at.desc())
                .first()
            )
        if hypothesis is not None and _is_public_play_investigation(hypothesis):
            episode = db.get(MaritimeEpisodeDB, hypothesis.episode_id) if hypothesis.episode_id else None
            evidence_links = [
                str(value or "").strip()
                for value in (hypothesis.evidence_links or [])
                if str(value or "").strip()
            ]
            lookup_ids: set[str] = set(evidence_links)
            lookup_ids.update(
                value.removeprefix("ais:")
                for value in evidence_links
                if value.startswith("ais:")
            )
            evidence_by_id = {
                row.id: row
                for row in (
                    db.query(IntelEventDB)
                    .filter(IntelEventDB.id.in_(sorted(lookup_ids)))
                    .all()
                    if lookup_ids else []
                )
            }
            evidence_events: list[Any] = []
            seen_evidence_ids: set[str] = set()
            for raw in evidence_links:
                evidence = evidence_by_id.get(raw)
                if evidence is None and raw.startswith("ais:"):
                    evidence = evidence_by_id.get(raw.removeprefix("ais:"))
                if evidence is not None and evidence.id not in seen_evidence_ids:
                    seen_evidence_ids.add(evidence.id)
                    evidence_events.append(evidence)
            primary_evidence = evidence_events[0] if evidence_events else None
            projection = _investigation_projection(
                hypothesis, episode, evidence_event=primary_evidence
            )
            canonical_id = str(projection["incident_id"])
            timeline = [{
                "id": canonical_id,
                "at": projection["reported_at"],
                "type": "episode" if episode is not None else "hypothesis",
                "source": "SeaCommons evidence engine",
                "title": projection["title"],
                "geometry": projection["geometry"],
                "properties": {
                    "state": hypothesis.state,
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "episode_id": str(episode.episode_id) if episode is not None else None,
                    "evidence_stage": hypothesis.evidence_stage,
                    "episode_present": episode is not None,
                    "archive_source": projection["archive_source"],
                    "reason_codes": list(hypothesis.reason_codes or []),
                    "counter_indicators": list(hypothesis.counter_indicators or []),
                },
            }]
            for index, evidence in enumerate(evidence_events, start=1):
                meta = dict(evidence.meta or {})
                geometry = ({"type": "Point", "coordinates": [evidence.lon, evidence.lat]}
                            if evidence.lat is not None and evidence.lon is not None else None)
                timeline.append({
                    "id": f"evidence:{index}", "at": _iso(evidence.timestamp_utc),
                    "type": "evidence", "source": evidence.source,
                    "title": str(meta.get("anomaly_type") or evidence.type).replace("_", " "),
                    "geometry": geometry,
                    "properties": {
                        "evidence_stage": hypothesis.evidence_stage,
                        "verification_status": meta.get("verification_status"),
                        "analysis_state": meta.get("analysis_state"),
                        "source_lineage": (
                            meta.get("source_lineage")
                            or meta.get("contributing_independence_groups")
                            or meta.get("independence_groups")
                        ),
                    },
                })

            # Bug fix: this branch used to `return` here, before ever reaching
            # the drift/satellite enrichment queries below -- a promoted
            # Maritime Investigation dossier could never include satellite
            # observations, drift products, or (once linked) radio evidence.
            # Drift and satellite are persisted keyed by the *originating*
            # IntelEvent id, not the hypothesis_id, so key the lookup off
            # every resolved evidence event id (both bare and "intel:"
            # prefixed, matching drift_service's own persistence convention)
            # -- a naive incident_id == hypothesis_id match returns nothing.
            evidence_ids = [e.id for e in evidence_events]
            drift_keys = [
                canonical_id,
                f"intel:{canonical_id}",
                hypothesis.hypothesis_id,
                f"intel:{hypothesis.hypothesis_id}",
            ]
            for evidence_id in evidence_ids:
                drift_keys.append(evidence_id)
                drift_keys.append(f"intel:{evidence_id}")
            drifts = (
                db.query(DriftResultDB)
                .filter(
                    DriftResultDB.status == "completed",
                    DriftResultDB.event_id.in_(drift_keys),
                )
                .order_by(DriftResultDB.created_at.asc())
                .all()
            ) if drift_keys else []
            timeline.extend(_drift_item(row) for row in drifts)

            satellite_ids = [canonical_id, hypothesis.hypothesis_id, *evidence_ids]
            satellites = (
                db.query(SatelliteObservationDB)
                .filter(
                    or_(
                        SatelliteObservationDB.episode_id == canonical_id,
                        SatelliteObservationDB.incident_id.in_(satellite_ids),
                    )
                )
                .order_by(SatelliteObservationDB.acquisition_time.asc())
                .all()
            ) if satellite_ids else []
            timeline.extend(_satellite_item(row) for row in satellites)

            evidence_times = [
                parsed for parsed in (parse_utc(str(event.timestamp_utc)) for event in evidence_events)
                if parsed is not None
            ]
            if episode is not None:
                if episode.start_at is not None:
                    evidence_times.append(
                        episode.start_at.replace(tzinfo=timezone.utc)
                        if episode.start_at.tzinfo is None else episode.start_at.astimezone(timezone.utc)
                    )
                if episode.end_at is not None:
                    evidence_times.append(
                        episode.end_at.replace(tzinfo=timezone.utc)
                        if episode.end_at.tzinfo is None else episode.end_at.astimezone(timezone.utc)
                    )
            radio_start = min(evidence_times) - timedelta(hours=6) if evidence_times else None
            radio_end = max(evidence_times) + timedelta(hours=6) if evidence_times else None
            evidence_mmsis = {
                str(event.linked_mmsi or "").strip()
                for event in evidence_events
                if str(event.linked_mmsi or "").strip()
            }
            radio_rows: list[tuple[Any, Any]] = []
            association_scope = RadioAISAssociationDB.episode_id == canonical_id
            if evidence_mmsis:
                association_scope = or_(
                    association_scope,
                    RadioAISAssociationDB.mmsi.in_(sorted(evidence_mmsis)),
                )
            associations = (
                db.query(RadioAISAssociationDB)
                .filter(
                    association_scope,
                    RadioAISAssociationDB.match_status.in_(("strong", "identity_only")),
                    RadioAISAssociationDB.confidence >= 0.8,
                )
                .order_by(RadioAISAssociationDB.created_at.asc())
                .limit(200)
                .all()
            )
            observation_ids = [row.observation_id for row in associations]
            observation_by_id = {
                row.observation_id: row
                for row in (
                    db.query(SourceObservationDB)
                    .filter(
                        SourceObservationDB.observation_id.in_(observation_ids),
                        SourceObservationDB.observation_type == "dsc_message",
                    )
                    .all()
                    if observation_ids else []
                )
            }
            for association in associations:
                observation = observation_by_id.get(association.observation_id)
                if observation is None:
                    continue
                observed_at = parse_utc(str(observation.observed_at))
                if observed_at is None:
                    continue
                if radio_start is not None and observed_at < radio_start:
                    continue
                if radio_end is not None and observed_at > radio_end:
                    continue
                radio_rows.append((observation, association))
            timeline.extend(_radio_item(observation, association) for observation, association in radio_rows)

            timeline = [item for item in timeline if item.get("at")]
            timeline.sort(key=lambda item: item["at"])
            return {
                "incident_id": canonical_id,
                "requested_incident_id": incident_id,
                "episode_id": str(episode.episode_id) if episode is not None else None,
                "hypothesis_id": hypothesis.hypothesis_id,
                "incident_status": hypothesis.state,
                "surface": "play", "domain": "maritime",
                "main_category": "maritime",
                "incident_type": projection["incident_type"],
                "investigation": True,
                "archive_decision": _archive_decision(hypothesis),
                "hypothesis_state": hypothesis.state,
                "evidence_stage": hypothesis.evidence_stage,
                "verification_status": projection.get("verification_status"),
                "corroborated": projection.get("corroborated"),
                "independence_groups": projection.get("independence_groups"),
                "evidence_count": projection.get("evidence_count"),
                "reason_codes": projection.get("reason_codes"),
                "counter_indicators": projection.get("counter_indicators"),
                "satellite_count": len(satellites),
                "satellite_evidence_count": sum(
                    1 for row in satellites
                    if row.association_status == "strong"
                    and row.episode_id
                    and str(row.episode_id) == canonical_id
                ),
                "satellite_context_count": sum(
                    1 for row in satellites
                    if not (
                        row.association_status == "strong"
                        and row.episode_id
                        and str(row.episode_id) == canonical_id
                    )
                ),
                "radio_count": len(radio_rows),
                "radio_evidence_count": sum(
                    1 for _observation, row in radio_rows
                    if row.match_status == "strong"
                    and row.episode_id
                    and str(row.episode_id) == canonical_id
                ),
                "radio_context_count": sum(
                    1 for _observation, row in radio_rows
                    if not (
                        row.match_status == "strong"
                        and row.episode_id
                        and str(row.episode_id) == canonical_id
                    )
                ),
                "drift_count": len(drifts),
                "timeline": timeline, "generated_at": now.isoformat(),
            }

        incident = db.get(HumanitarianIncidentDB, incident_id)
        event = db.get(IntelEventDB, incident_id)
        generic_maritime = incident is None and event is not None and _is_public_catalog_maritime(event)
        if incident is None and not generic_maritime:
            raise HTTPException(status_code=404, detail="Incident not found")
        incident_status = _status_for_row(incident, now=now) if incident is not None else _generic_maritime_status(event)
        domain = "humanitarian" if incident is not None else "maritime"
        timeline: list[dict[str, Any]] = []
        if event is not None:
            geometry = None
            if event.lat is not None and event.lon is not None:
                geometry = {"type": "Point", "coordinates": [event.lon, event.lat]}
            timeline.append({
                "id": f"report:{incident_id}",
                "at": _iso(event.timestamp_utc),
                "type": "report",
                "source": event.source,
                "title": event.title,
                "geometry": geometry,
                "properties": {"url": event.url or None},
            })
            for repost in (event.meta or {}).get("thread_reposts") or []:
                item = _thread_item(incident_id, repost, reported_at=incident.reported_at if incident is not None else event.timestamp_utc)
                if item is not None:
                    timeline.append(item)
        sar_missions = []
        if incident is not None:
            transitions = (
                db.query(IncidentTransitionDB)
                .filter(IncidentTransitionDB.incident_id == incident_id)
                .order_by(IncidentTransitionDB.transition_at.asc())
                .all()
            )
            timeline.extend(_transition_item(row) for row in transitions)
            sar_missions = (
                db.query(AssessmentDB)
                .filter(
                    AssessmentDB.incident_id == incident_id,
                    AssessmentDB.field_type == "sar_mission",
                )
                .order_by(AssessmentDB.updated_at.asc(), AssessmentDB.assessment_id.asc())
                .all()
            )
            timeline.extend(_sar_mission_item(row) for row in sar_missions)

        drifts = (
            db.query(DriftResultDB)
            .filter(
                DriftResultDB.status == "completed",
                or_(
                    DriftResultDB.event_id == incident_id,
                    DriftResultDB.event_id == f"intel:{incident_id}",
                ),
            )
            .order_by(DriftResultDB.created_at.asc())
            .all()
        )
        timeline.extend(_drift_item(row) for row in drifts)

        satellites = (
            db.query(SatelliteObservationDB)
            .filter(SatelliteObservationDB.incident_id == incident_id)
            .order_by(SatelliteObservationDB.acquisition_time.asc())
            .all()
        )
        timeline.extend(_satellite_item(row) for row in satellites)

    timeline = [item for item in timeline if item.get("at")]
    timeline.sort(key=lambda item: item["at"])
    return {
        "incident_id": incident_id,
        "incident_status": incident_status,
        "surface": "play",
        "domain": domain,
        "sar_mission_count": len(sar_missions) if incident is not None else 0,
        "drift_count": len(drifts),
        "satellite_count": len(satellites),
        "timeline": timeline,
        "generated_at": now.isoformat(),
    }


def _satellite_item(row) -> dict[str, Any]:
    return {
        "id": row.observation_id,
        "at": _iso(row.acquisition_time),
        "type": "satellite",
        "source": row.provider,
        "title": f"{row.mission} observation",
        "geometry": row.footprint,
        "properties": {
            "mission": row.mission,
            "product_id": row.product_id,
            "sensor_type": row.sensor_type,
            "temporal_relation": row.temporal_relation,
            "temporal_delta_s": row.temporal_delta_s,
            "asset_ref": row.asset_ref,
            "source_url": row.source_url,
            "bbox": row.bbox,
            "resolution_m": row.resolution_m,
            "cloud_cover": row.cloud_cover,
            "polarisation": row.polarisation,
            "evidence_status": row.evidence_status,
            "association_status": row.association_status,
            "episode_id": row.episode_id,
            "evidence_role": (
                "corroboration"
                if row.association_status == "strong" and row.episode_id
                else "context"
            ),
            "case_targets": list((row.provenance or {}).get("case_targets") or []),
            "provenance": row.provenance or {},
        },
    }
