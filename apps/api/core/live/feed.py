# SPDX-License-Identifier: AGPL-3.0-or-later
"""Privacy-preserving Live projections and feed composition."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from core.domain.live_contracts import (
    LIVE_SIGNAL_SCHEMA,
    IncidentLifecycle,
    IntelTier,
    LiveSignalKind,
    LocationPrecision,
    PublicationStatus,
    SourcePolicy,
    VerificationStatus,
    validate_live_signal,
)
from core.domain.visual_category import visual_category_fields
from core.intel import lifecycle
from core.intel.public_policy import (
    HUMANITARIAN_DRIFT_DOMAINS,
    compartment_for_domain,
    domains_for_mode,
)

# Lifecycle states for which a live, active-looking drift cone would misread as
# "still adrift, still searching". The point/marker stays visible via the
# signal feed; only the trajectory/cone is withheld once the search is over.
# `needs_review` is an OPEN state — a human still has to confirm the outcome —
# so its persisted operational drift stays on the public map. Mirrors
# `core.intel.drift_service._DRIFT_BLOCKING_LIFECYCLES`.
_DRIFT_HIDDEN_LIFECYCLES = frozenset({"resolved", "archived"})
from core.intel.store import IntelEvent, intel_store
from core.live.projection import (
    _approximate_public_point,
    _current_trajectory_estimate,
    _is_publishable_live_drift,
    _public_drift_feature,
    _public_intel_feature,
    dedupe_public_case_items,
    is_useful_public_case_feature,
)
from core.live.retention import is_live_retained
from core.live.vessel_episodes import (
    add_nearby_humanitarian_context,
    coalesce_security_vessel_episodes,
)

logger = logging.getLogger(__name__)


def _parse_live_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

# Public received/safety types that must survive in-memory deque churn. Raw
# Security detector families are intentionally absent: after the canonical
# cutover they remain internal evidence and public Live reads assessed
# InvestigationHypothesis rows through _published_security_hypothesis_features.
_PUBLIC_DURABLE_TYPES = frozenset({
    "distress", "twitter", "mastodon", "bluesky", "ngo_activity",
    "gdacs", "vessel_incident", "iom_incident",
})

# Candidate and count computation must not depend on the response page size.
# The route itself caps returned features at 500; use that same fixed window
# for both modes, then apply ``limit``/``since`` only to the selected payload.
_LIVE_WINDOW_LIMIT = 500
_LIVE_DURABLE_SCAN_LIMIT = 1500
_LIVE_DURABLE_TYPE_SCAN_LIMIT = 500

# Derived SAR-responder activity is an observation in the rolling Live
# timeline, not a current vessel-position marker. Current fleet positions have
# their own much shorter freshness policy; the observation itself remains for
# the same 24-hour retention window as other Live items.
_SAR_ACTIVITY_LIVE_TTL = timedelta(hours=24)


def _is_fresh_sar_activity(event: IntelEvent, *, now: datetime) -> bool:
    meta = event.metadata or {}
    if not (
        event.type == "ngo_activity"
        and meta.get("observation_type") == "sar_responder_activity"
    ):
        return True
    try:
        observed = datetime.fromisoformat(str(event.timestamp_utc).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return now - observed.astimezone(UTC) <= _SAR_ACTIVITY_LIVE_TTL


def _published_security_hypothesis_features(limit: int) -> list[dict[str, Any]]:
    """Project only hypothesis-gated Maritime Intelligence into Live.

    Raw AIS/security detector output is evidence and is blocked in
    core.live.projection. This is the canonical replacement path.
    """
    from core.intel.hypothesis_publication import public_hypothesis_collection

    type_to_anomaly = {
        "dark_transit": "ais_gap",
        "position_spoofing": "position_spoofing",
        "covert_rendezvous": "rendezvous",
        "infrastructure_pattern": "infrastructure",
    }
    features: list[dict[str, Any]] = []
    for feature in public_hypothesis_collection(limit=limit).get("features", []):
        props = dict(feature.get("properties") or {})
        hypothesis_type = str(props.get("hypothesis_type") or "")
        anomaly_type = type_to_anomaly.get(hypothesis_type, hypothesis_type)
        category = visual_category_fields(
            source="SeaCommons assessed intelligence",
            event_type="ais_anomaly",
            maritime_domain="grey_zone",
            metadata={"anomaly_type": anomaly_type},
        )
        from core.domain.incident_taxonomy import taxonomy_fields

        taxonomy = taxonomy_fields(
            event_type="ais_anomaly",
            maritime_domain="grey_zone",
            metadata={
                "anomaly_type": anomaly_type,
                "evidence_stage": props.get("evidence_stage"),
            },
            hypothesis_type=hypothesis_type,
        )
        timestamp = props.get("timestamp_utc")
        if not timestamp or not feature.get("geometry"):
            continue
        parent_id = str(props.get("episode_id") or props["id"])
        public = {
            "type": "Feature",
            "id": parent_id,
            "geometry": feature["geometry"],
            "properties": {
                "schema": LIVE_SIGNAL_SCHEMA,
                "id": parent_id,
                "episode_id": props.get("episode_id"),
                "hypothesis_id": props.get("hypothesis_id") or props.get("id"),
                "live_role": "maritime_episode",
                "type": "ais_anomaly",
                **category,
                **taxonomy,
                "kind": LiveSignalKind.CONTEXT.value,
                "severity": "medium",
                "tier": IntelTier.SIGNAL.value,
                "verification_status": (
                    VerificationStatus.MULTI_SOURCE_CORROBORATED.value
                    if props.get("evidence_stage") == "corroborated"
                    else str(props.get("evidence_stage") or "assessed")
                ),
                "publication_status": PublicationStatus.PUBLISHED.value,
                "source_policy": SourcePolicy.OPERATOR_PUBLISHED.value,
                "title": str(props.get("title") or "Reviewed maritime intelligence")[:255],
                "text": "",
                "url": "",
                "source": "SeaCommons assessed intelligence",
                "timestamp_utc": timestamp,
                "location_precision": LocationPrecision.REPORTED_OR_DERIVED.value,
                "maritime_domain": "grey_zone",
                "hypothesis_type": hypothesis_type,
                "hypothesis_state": "published",
                "reason_codes": list(props.get("reason_codes") or ()),
                "evidence_stage": props.get("evidence_stage"),
                "caveats": list(props.get("caveats") or ()),
            },
        }
        try:
            features.append(validate_live_signal(public))
        except ValueError:
            logger.warning("Dropping hypothesis that violates Live contract id=%s", props.get("id"))
    return features



def _published_open_episode_features(limit: int) -> list[dict[str, Any]]:
    """Project recent public MaritimeEpisode dossiers into Live.

    A dossier may be public while still single-lineage/evidence_candidate.
    That is deliberately weaker than a published InvestigationHypothesis:
    it says "this behaviour is specific enough to investigate", not that the
    interpretation is independently corroborated.
    """
    try:
        from core.db.models import IntelEventDB, MaritimeEpisodeDB
        from core.db.session import session_scope
        from core.domain.incident_taxonomy import taxonomy_fields

        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=24)
        with session_scope() as db:
            orm_rows = (
                db.query(MaritimeEpisodeDB)
                .filter(MaritimeEpisodeDB.end_at >= cutoff.replace(tzinfo=None))
                .order_by(MaritimeEpisodeDB.updated_at.desc())
                .limit(min(max(limit * 4, 200), 2000))
                .all()
            )
            rows: list[dict[str, Any]] = []
            for row in orm_rows:
                end_at = (
                    row.end_at.replace(tzinfo=UTC)
                    if row.end_at.tzinfo is None
                    else row.end_at.astimezone(UTC)
                )
                analysis = ((row.behaviour_context or {}).get("analysis") or {})
                if (
                    row.status == "superseded"
                    or end_at < cutoff
                    or analysis.get("publication_state") != "published"
                    or analysis.get("analysis_state") not in {"evidence_candidate", "evidence"}
                    or analysis.get("resolution_state") == "resolved"
                ):
                    continue
                rows.append({
                    "episode_id": str(row.episode_id),
                    "episode_family": str(row.episode_family or ""),
                    "geometry": row.geometry,
                    "observation_ids": list(row.observation_ids or ()),
                    "independence_groups": list(row.independence_groups or ()),
                    "verification_status": str(row.verification_status or "single_source_observed"),
                    "behaviour_context": dict(row.behaviour_context or {}),
                    "end_at": end_at,
                })
                if len(rows) >= limit:
                    break

            lookup_ids: set[str] = set()
            for row in rows:
                for value in list(row["observation_ids"])[:3]:
                    raw = str(value or "").strip()
                    if raw:
                        lookup_ids.add(raw)
                        if raw.startswith("ais:"):
                            lookup_ids.add(raw.removeprefix("ais:"))
            event_by_id: dict[str, dict[str, Any]] = {}
            ids = sorted(lookup_ids)
            for start in range(0, len(ids), 500):
                for event in db.query(IntelEventDB).filter(
                    IntelEventDB.id.in_(ids[start:start + 500])
                ).all():
                    event_by_id[event.id] = {
                        "id": event.id,
                        "title": event.title or "",
                        "meta": dict(event.meta or {}),
                    }
    except Exception:
        logger.exception("Failed to project open MaritimeEpisode dossiers")
        return []

    family_to_anomaly = {
        "gap_episode": "long_gap",
        "rendezvous_episode": "ais_rendezvous",
        "spoofing_episode": "position_jump",
        "infrastructure_proximity_episode": "infrastructure_pattern",
        "port_call_episode": "sanctioned_port_call",
        "safety_episode": "safety_event",
        "identity_integrity_episode": "identity_anomaly",
    }
    family_to_title = {
        "gap_episode": "AIS reporting-gap candidate",
        "rendezvous_episode": "Sustained vessel rendezvous dossier",
        "spoofing_episode": "AIS position-integrity dossier",
        "infrastructure_proximity_episode": "Infrastructure-proximity dossier",
        "port_call_episode": "Sanctioned vessel port-call dossier",
        "safety_episode": "AIS-reported maritime safety dossier",
        "identity_integrity_episode": "Vessel identity-integrity dossier",
    }
    family_to_summary = {
        "gap_episode": "AIS silence met the reception-expectation gate. This is an investigation candidate, not proof that the transmitter was intentionally disabled.",
        "rendezvous_episode": "A sustained vessel rendezvous met the case-opening threshold. Proximity alone does not establish a transfer or illicit activity.",
        "spoofing_episode": "A sustained AIS position-integrity pattern met the case-opening threshold. A single AIS lineage does not by itself prove spoofing.",
        "infrastructure_proximity_episode": "A sustained infrastructure-proximity pattern met the case-opening threshold. Proximity does not establish interference.",
        "port_call_episode": "Observed vessel movement and identity data met the case-opening threshold. This does not establish sanctions evasion.",
        "safety_episode": "AIS reported an operational safety state. This dossier records the signal and updates; it is not independent confirmation of a casualty.",
        "identity_integrity_episode": "An identity-integrity pattern met the case-opening threshold. This is not an allegation of deceptive intent.",
    }

    features: list[dict[str, Any]] = []
    for row in rows:
        family = str(row["episode_family"] or "")
        anomaly = family_to_anomaly.get(family)
        if anomaly is None or not row["geometry"]:
            continue
        source_event = None
        for value in list(row["observation_ids"])[:3]:
            raw = str(value or "").strip()
            source_event = event_by_id.get(raw)
            if source_event is None and raw.startswith("ais:"):
                source_event = event_by_id.get(raw.removeprefix("ais:"))
            if source_event is not None:
                break
        meta = dict(source_event.get("meta") or {}) if source_event is not None else {}
        verification = str(row["verification_status"] or "single_source_observed")
        corroborated = (
            verification == VerificationStatus.MULTI_SOURCE_CORROBORATED.value
        )
        if family == "gap_episode" and not corroborated:
            reception = meta.get("reception_expectation") or {}
            if not (
                isinstance(reception, dict)
                and reception.get("support_level") == "strong"
            ):
                # Legacy single-lineage gap dossiers remain durable in Play,
                # but Live requires the current reception-expectation gate.
                continue
        opening = (row["behaviour_context"] or {}).get("case_opening") or {}
        reason_codes = list(opening.get("reason_codes") or ())
        safety = family == "safety_episode"
        if (
            safety
            and "REPEATED_AIS_DISTRESS_BEACON" in reason_codes
            and now - row["end_at"] > timedelta(hours=2)
        ):
            # Dedicated AIS beacon dossiers are operational Live signals only
            # while the repeated transmission is still fresh. The durable
            # dossier remains available in Play after this cutoff.
            continue
        sanctions = (
            family == "port_call_episode"
            or "STRONG_SANCTIONS_IDENTITY_MATCH" in reason_codes
        )
        domain = "safety" if safety else "sanctions" if sanctions else "grey_zone"
        event_type = (
            "vessel_incident" if safety
            else "ais_rendezvous" if family == "rendezvous_episode"
            else "vessel_identity" if family == "port_call_episode"
            else "ais_anomaly"
        )
        category = visual_category_fields(
            source="SeaCommons episode engine",
            event_type=event_type,
            maritime_domain=domain,
            metadata={**meta, "anomaly_type": anomaly},
        )
        taxonomy = taxonomy_fields(
            event_type=event_type,
            maritime_domain=domain,
            metadata={**meta, "anomaly_type": anomaly, "episode_family": family},
        )
        observed_at = row["end_at"].isoformat()
        title = (
            str(source_event.get("title") or "")[:255]
            if source_event is not None and source_event.get("title")
            else family_to_title[family]
        )
        public = {
            "type": "Feature",
            "id": str(row["episode_id"]),
            "geometry": row["geometry"],
            "properties": {
                "schema": LIVE_SIGNAL_SCHEMA,
                "id": str(row["episode_id"]),
                "episode_id": str(row["episode_id"]),
                "episode_family": family,
                "live_role": "maritime_episode",
                "type": event_type,
                **category,
                **taxonomy,
                "kind": LiveSignalKind.CONTEXT.value,
                "severity": "medium",
                "tier": IntelTier.SIGNAL.value,
                "verification_status": verification,
                "publication_status": PublicationStatus.PUBLISHED.value,
                "source_policy": SourcePolicy.OPERATOR_PUBLISHED.value,
                "title": title,
                "text": "",
                "url": "",
                "source": "SeaCommons episode engine",
                "timestamp_utc": observed_at,
                "location_precision": LocationPrecision.REPORTED_OR_DERIVED.value,
                "maritime_domain": domain,
                "analysis_state": ((row["behaviour_context"] or {}).get("analysis") or {}).get("analysis_state"),
                "publication_state": "published",
                "resolution_state": ((row["behaviour_context"] or {}).get("analysis") or {}).get("resolution_state") or "open",
                "reason_codes": reason_codes,
                **({
                    "anomaly_type": meta.get("anomaly_type"),
                    "reception_expectation": meta.get("reception_expectation"),
                    "gap_still_open": meta.get("gap_still_open"),
                    "current_silent_seconds": meta.get("current_silent_seconds"),
                    "silent_seconds": meta.get("silent_seconds"),
                    "offshore_reason_codes": meta.get("offshore_reason_codes"),
                    "offshore_rationale": meta.get("offshore_rationale"),
                } if family == "gap_episode" else {}),
                "evidence_stage": "corroborated" if corroborated else "derived",
                "corroborated": corroborated,
                "independent_source_count": len(row["independence_groups"]),
                "public_summary": family_to_summary[family],
            },
        }
        try:
            features.append(validate_live_signal(public))
        except ValueError:
            logger.warning("Dropping open episode that violates Live contract id=%s", row["episode_id"])
    return features


def _published_ingested_features(limit: int) -> list[dict[str, Any]]:
    """
    Project user/partner signals only after an explicit publication decision.

    WhatsApp, SMS and Telegram are private by default. Their raw text, sender
    identifier and provider delivery identifiers never enter this response.
    """
    try:
        from sqlalchemy import select

        from core.db.models import IngestedSignalDB
        from core.db.session import session_scope

        with session_scope() as db:
            rows = [
                {
                    "signal_id": row.signal_id,
                    "source_channel": row.source_channel,
                    "payload": dict(row.payload or {}),
                    "received_at": row.received_at,
                }
                for row in db.execute(
                    select(IngestedSignalDB)
                    .order_by(IngestedSignalDB.received_at.desc())
                    .limit(min(limit * 3, 500))
                ).scalars()
            ]
    except Exception:  # noqa: BLE001 - public feed fails closed when storage is unavailable
        return []

    features: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        if payload.get("publication_status") != PublicationStatus.PUBLISHED.value:
            continue
        lat, lon = payload.get("lat"), payload.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        signal_id = str(payload.get("signal_id") or row["signal_id"])
        public_lat, public_lon = _approximate_public_point(signal_id, float(lat), float(lon))
        condition = str(payload.get("vessel_condition") or "reported distress").replace("_", " ")
        channel = str(payload.get("source_channel") or row["source_channel"] or "partner")
        partner_report = channel in {"webhook", "api", "partner"}
        feature = {
            "type": "Feature",
            "id": f"signal:{signal_id}",
            "geometry": {"type": "Point", "coordinates": [public_lon, public_lat]},
            "properties": {
                "schema": LIVE_SIGNAL_SCHEMA,
                "id": f"signal:{signal_id}",
                "type": "distress",
                "main_category": "humanitarian",
                "incident_type": "distress",
                "corroborated": False,
                "sanctions_matched": False,
                "kind": LiveSignalKind.DISTRESS.value,
                "severity": "high" if payload.get("medical_emergency") else "medium",
                "tier": IntelTier.OPERATIONAL.value,
                "priority": 1,
                "verification_status": VerificationStatus.PARTNER_REPORTED.value
                if partner_report
                else VerificationStatus.USER_REPORTED.value,
                "publication_status": PublicationStatus.PUBLISHED.value,
                "source_policy": SourcePolicy.OPERATOR_PUBLISHED.value,
                "title": f"Maritime signal · {condition}"[:255],
                "text": "",
                "url": "",
                "source": "partner intake" if partner_report else "community report",
                "channel": channel,
                "location_precision": LocationPrecision.APPROXIMATE.value,
                "location_uncertainty_m": 2500,
                "incident_lifecycle": IncidentLifecycle.ACTIVE.value,
                "timestamp_utc": payload.get("event_time_utc")
                or payload.get("timestamp_utc")
                or row["received_at"].replace(tzinfo=UTC).isoformat(),
                "received_at": payload.get("timestamp_utc")
                or row["received_at"].replace(tzinfo=UTC).isoformat(),
            },
        }
        try:
            features.append(validate_live_signal(feature))
        except ValueError:
            logger.warning("Dropping ingested signal that violates Live contract id=%s", signal_id)
            continue
        if len(features) >= limit:
            break
    return features


def _attach_cross_modal_evidence(
    features: list[dict[str, Any]], *, now: datetime
) -> None:
    """Attach privacy-safe Radio/Satellite evidence counts to public cases.

    These channels never become standalone incidents merely because data
    exists. They enrich a case/episode only when storage already links the
    evidence to that case or vessel.
    """
    if not features:
        return
    incident_keys: set[str] = set()
    mmsis: set[str] = set()
    props_by_key: dict[str, list[dict[str, Any]]] = {}
    props_by_mmsi: dict[str, list[dict[str, Any]]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        keys = {
            str(value)
            for value in (
                props.get("id"),
                props.get("episode_id"),
                props.get("humanitarian_case_id"),
                props.get("incident_id"),
            )
            if value
        }
        for key in keys:
            incident_keys.add(key)
            props_by_key.setdefault(key, []).append(props)
        mmsi = str(props.get("linked_mmsi") or props.get("mmsi") or "").strip()
        if mmsi:
            mmsis.add(mmsi)
            props_by_mmsi.setdefault(mmsi, []).append(props)

    try:
        from sqlalchemy import or_

        from core.db.models import RadioAISAssociationDB, SatelliteObservationDB
        from core.db.session import session_scope

        satellite_rows = []
        radio_rows = []
        with session_scope() as db:
            if incident_keys:
                satellite_rows = (
                    db.query(SatelliteObservationDB)
                    .filter(
                        or_(
                            SatelliteObservationDB.incident_id.in_(incident_keys),
                            SatelliteObservationDB.episode_id.in_(incident_keys),
                        )
                    )
                    .all()
                )
            if incident_keys or mmsis:
                radio_scopes = []
                if incident_keys:
                    radio_scopes.append(RadioAISAssociationDB.episode_id.in_(incident_keys))
                if mmsis:
                    radio_scopes.append(RadioAISAssociationDB.mmsi.in_(mmsis))
                radio_rows = (
                    db.query(RadioAISAssociationDB)
                    .filter(
                        or_(*radio_scopes),
                        RadioAISAssociationDB.created_at >= (now - timedelta(hours=24)).replace(tzinfo=None),
                    )
                    .all()
                )
    except Exception:
        logger.debug("Cross-modal Live enrichment unavailable", exc_info=True)
        return

    satellite_by_case: dict[str, list[Any]] = {}
    satellite_by_episode: dict[str, list[Any]] = {}
    for row in satellite_rows:
        satellite_by_case.setdefault(str(row.incident_id), []).append(row)
        if row.episode_id:
            satellite_by_episode.setdefault(str(row.episode_id), []).append(row)
    radio_by_mmsi: dict[str, list[Any]] = {}
    radio_by_episode: dict[str, list[Any]] = {}
    for row in radio_rows:
        radio_by_mmsi.setdefault(str(row.mmsi), []).append(row)
        if row.episode_id:
            radio_by_episode.setdefault(str(row.episode_id), []).append(row)

    for key, targets in props_by_key.items():
        context_rows = satellite_by_case.get(key) or []
        associated_rows = [
            row for row in (satellite_by_episode.get(key) or [])
            if row.association_status == "strong"
        ]
        rows_by_id = {
            str(row.observation_id): row
            for row in [*context_rows, *associated_rows]
        }
        if not rows_by_id:
            continue
        rows = list(rows_by_id.values())
        associated_ids = {str(row.observation_id) for row in associated_rows}
        statuses = sorted({str(row.association_status) for row in rows if row.association_status})
        for props in targets:
            props["satellite_count"] = max(int(props.get("satellite_count") or 0), len(rows))
            props["satellite_evidence_count"] = len(associated_ids)
            props["satellite_context_count"] = len(rows) - len(associated_ids)
            props["has_satellite"] = True
            props["has_satellite_evidence"] = bool(associated_ids)
            props["satellite_association_statuses"] = statuses
            facets = list(props.get("facets") or [])
            if "satellite" not in facets:
                facets.append("satellite")
            props["facets"] = facets

    # Public assessed Maritime cases intentionally do not expose MMSI, so
    # canonical episode linkage must be sufficient to surface associated DSC.
    for key, targets in props_by_key.items():
        associated = [
            row for row in (radio_by_episode.get(key) or [])
            if row.match_status == "strong"
        ]
        if not associated:
            continue
        statuses = sorted({str(row.match_status) for row in associated if row.match_status})
        for props in targets:
            props["radio_count"] = max(int(props.get("radio_count") or 0), len(associated))
            props["radio_evidence_count"] = len(associated)
            props["radio_context_count"] = max(
                0, int(props.get("radio_count") or 0) - len(associated)
            )
            props["has_radio"] = True
            props["has_radio_evidence"] = True
            props["radio_association_statuses"] = sorted(set(
                [*(props.get("radio_association_statuses") or []), *statuses]
            ))
            facets = list(props.get("facets") or [])
            if "radio" not in facets:
                facets.append("radio")
            props["facets"] = facets

    for mmsi, targets in props_by_mmsi.items():
        rows = radio_by_mmsi.get(mmsi) or []
        if not rows:
            continue
        statuses = sorted({str(row.match_status) for row in rows if row.match_status})
        for props in targets:
            parent_id = str(props.get("episode_id") or props.get("id") or "")
            associated = [
                row for row in rows
                if row.match_status == "strong"
                and row.episode_id
                and str(row.episode_id) == parent_id
            ]
            props["radio_count"] = max(int(props.get("radio_count") or 0), len(rows))
            props["radio_evidence_count"] = len(associated)
            props["radio_context_count"] = len(rows) - len(associated)
            props["has_radio"] = True
            props["has_radio_evidence"] = bool(associated)
            props["radio_association_statuses"] = statuses
            facets = list(props.get("facets") or [])
            if "radio" not in facets:
                facets.append("radio")
            props["facets"] = facets


def public_signal_collection(
    *,
    limit: int = 300,
    days: int = 30,
    since: str | None = None,
    mode: str = "humanitarian",
) -> dict[str, Any]:
    requested_mode = str(mode or "humanitarian").strip().lower()
    selected_mode = "maritime" if requested_mode == "security" else requested_mode
    if selected_mode not in {"humanitarian", "maritime", "safety", "all"}:
        selected_mode = "humanitarian"
    memory_events = intel_store.events(limit=600, max_age_days=days)
    # twikit_monitor writes source=author or handle per tweet -- the account's
    # display name ("Alarm Phone") when the tweet carried one, its handle
    # ("alarm_phone") otherwise. Both are real, current values for the same
    # logical source; an exact match on one silently drops the other.
    durable_alarm_phone = intel_store.persisted_events(
        source_in=["Alarm Phone", "alarm_phone"],
        max_age_days=days,
        limit=_LIVE_DURABLE_SCAN_LIMIT,
    )
    # The bounded in-memory deque (600) is now shared with high-volume MDA
    # analysis events (ais_anomaly / vessel_identity / correlated_alert, all
    # operator-internal). They can evict older public distress reports from the
    # deque, leaving the public feed empty. Back the public-eligible types with
    # a direct DB read so churn cannot starve it.
    # Read each public event family independently. A single mixed recency
    # query is unsafe here: high-volume derived families (especially
    # correlated_alert) can fill the whole durable cap in minutes and hide
    # lower-volume public Safety/Humanitarian rows before projection.
    durable_public_by_id: dict[str, IntelEvent] = {}
    for event_type in sorted(_PUBLIC_DURABLE_TYPES):
        for event in intel_store.persisted_events(
            types=[event_type],
            max_age_days=days,
            limit=_LIVE_DURABLE_TYPE_SCAN_LIMIT,
        ):
            durable_public_by_id[event.id] = event
    # Factual sanctions port calls are a low-volume Maritime case family,
    # but their transport type is vessel_identity, which is intentionally not
    # in _PUBLIC_DURABLE_TYPES because the vast majority of identity rows are
    # internal evidence. Recover only this explicitly-published subtype so it
    # survives in-memory AIS/MDA churn without opening the raw identity stream.
    durable_sanction_port_calls: list[IntelEvent] = []
    for event in intel_store.persisted_events(
        types=["vessel_identity"],
        max_age_days=days,
        limit=_LIVE_DURABLE_TYPE_SCAN_LIMIT,
    ):
        meta = event.metadata or {}
        if (
            meta.get("anomaly_type") == "sanctioned_port_call"
            and str(meta.get("publication_status") or "").lower() == "published"
        ):
            durable_public_by_id[event.id] = event
            durable_sanction_port_calls.append(event)
    durable_public = list(durable_public_by_id.values())
    # core.live.retention: a qualified AIS-evidence event (ais_anomaly /
    # ais_rendezvous) must stay visible for its full 24h window regardless of
    # deque residency or process restart. These types are deliberately absent
    # from _PUBLIC_DURABLE_TYPES above (most rows of these types are internal
    # evidence, not public); live_retained_events() re-hydrates only the
    # subset that already earned a durable retention window.
    durable_live_retained = intel_store.live_retained_events(limit=_LIVE_DURABLE_SCAN_LIMIT)
    for event in durable_live_retained:
        durable_public_by_id.setdefault(event.id, event)
    by_id = {event.id: event for event in durable_alarm_phone}
    by_id.update(durable_public_by_id)
    # In-memory objects contain the most recent metadata observations and must
    # win over the durable snapshot when both are present.
    by_id.update({event.id: event for event in memory_events})
    events = list(by_id.values())
    now = datetime.now(UTC)

    # Public-safe explanation for why durable Alarm Phone observations do or
    # do not appear on the rolling Live surface.  This is aggregate-only: no
    # raw text, identifiers or coordinates leave the API.
    humanitarian_candidate_drops: Counter[str] = Counter()
    humanitarian_eligible_features: list[dict[str, Any]] = []
    for candidate in durable_alarm_phone:
        candidate_domain = candidate.maritime_domain()
        candidate_mode = (
            "safety" if candidate_domain == "safety"
            else compartment_for_domain(candidate_domain)
        )
        if candidate_mode != "humanitarian":
            humanitarian_candidate_drops["not_humanitarian_compartment"] += 1
            continue
        candidate_feature = _public_intel_feature(
            candidate, allowed_domains=domains_for_mode("humanitarian")
        )
        if candidate_feature is None:
            humanitarian_candidate_drops["projection_withheld"] += 1
            continue
        if not is_useful_public_case_feature(candidate_feature):
            humanitarian_candidate_drops["case_gate_withheld"] += 1
            continue
        if not lifecycle.is_within_live_retention_window(candidate, now=now):
            humanitarian_candidate_drops["outside_live_window"] += 1
            continue
        humanitarian_eligible_features.append(candidate_feature)

    deduped_candidate_features = dedupe_public_case_items(humanitarian_eligible_features)
    deduplicated_count = len(humanitarian_eligible_features) - len(deduped_candidate_features)
    if deduplicated_count:
        humanitarian_candidate_drops["duplicate_public_case"] += deduplicated_count
    humanitarian_candidate_drops["eligible"] = len(deduped_candidate_features)

    by_source: dict[str, list[IntelEvent]] = {}
    for event in events:
        by_source.setdefault(event.source, []).append(event)
    mode_features: dict[str, list[dict[str, Any]]] = {
        "humanitarian": [],
        "security": [],
        "safety": [],
    }
    mode_context: dict[str, list[dict[str, Any]]] = {
        "humanitarian": [],
        "security": [],
        "safety": [],
    }
    for event in events:
        # Derived responder movement is a short-lived observation. Keeping a
        # 19h-old convergence on the Live map makes a vessel that is now
        # transiting elsewhere look as if it were still on scene.
        if not _is_fresh_sar_activity(event, now=now):
            continue
        # F-07: positive allow-lists, never humanitarian-by-complement.
        # environmental / unknown -> no operational compartment (still
        # fails closed). docs/fixes.md P0.1/P6.4: Maritime Safety
        # (not_under_command/aground/restricted_manoeuvrability) is its
        # own visible compartment -- compartment_for_domain() only knows
        # humanitarian/security, so it is checked explicitly here rather
        # than folded into that fixed complement (which would make it
        # Security, the exact A-01/A-02 defect) or left unhandled (which
        # silently drops it from every mode, the state it was actually in
        # before this fix).
        resolved_domain = event.maritime_domain()
        if resolved_domain == "safety":
            event_mode = "safety"
        else:
            event_mode = compartment_for_domain(resolved_domain)
        if event_mode is None:
            continue
        feature = _public_intel_feature(
            event,
            allowed_domains=domains_for_mode(event_mode),
        )
        if not feature or not is_useful_public_case_feature(feature):
            continue
        kind = feature["properties"].get("kind")
        if kind == "distress" and event.type != "correlated_alert":
            if not lifecycle.is_within_live_retention_window(event, now=now):
                # Hard cutoff: a distress marker's total life on Live is bounded,
                # regardless of whether it was ever resolved. Older history lives
                # in the archive/replay views, not the live pulsing map.
                continue
            # docs/updates.md P0.10: canonical HumanitarianIncident state is
            # the public authority when one exists; falls back to read-time
            # recomputation only for markers with no incident (Maritime
            # Safety, or pre-P0.3 legacy records) -- see
            # core.intel.humanitarian_incident.resolve_public_incident_state.
            from core.intel.humanitarian_incident import resolve_public_incident_state

            incident_state = resolve_public_incident_state(
                event, now=now, same_source=by_source.get(event.source, [])
            )
            # Live is a rolling 24-hour timeline. Terminal real-world
            # statuses remain visible until retention expiry, but are clearly
            # labelled so they are not mistaken for active response cases.
            feature["properties"]["kind"] = LiveSignalKind.DISTRESS.value
            feature["properties"]["incident_lifecycle"] = incident_state["lifecycle"]
            feature["properties"]["incident_status"] = incident_state["incident_status"]
            feature["properties"]["reported_at"] = incident_state["reported_at"]
            feature["properties"]["last_update_at"] = incident_state["last_update_at"]
            feature["properties"]["state_changed_at"] = incident_state["state_changed_at"]
            feature["properties"]["resolved_at"] = incident_state["resolved_at"]
            mode_features[event_mode].append(feature)
        elif kind in ("context", "distress"):
            # Broader OSINT context: news, AIS anomalies, GDACS, vessel
            # incidents, correlated fusion alerts — eligibility (type + maritime
            # compartment) is already decided in _public_intel_feature. Bounded
            # by the same age window, no pulsing lifecycle. Kept in a separate
            # bucket and capped so a chatty context source can never crowd a
            # genuine distress report out of the window.
            #
            # core.live.retention: an event that already earned a durable
            # retention window (qualified AIS evidence) is governed by
            # live_expires_at alone -- server-authoritative, independent of
            # this event row's own timestamp age or resolved/explained
            # operational state. Everything else keeps the rolling 24-hour
            # public timeline contract from core.intel.lifecycle.
            live_expires_raw = (event.metadata or {}).get("live_expires_at")
            if live_expires_raw:
                if not is_live_retained(_parse_live_timestamp(live_expires_raw), now=now):
                    continue
            elif not lifecycle.is_within_live_retention_window(event, now=now):
                continue
            mode_context[event_mode].append(feature)

    def finalize(mode_name: str) -> list[dict[str, Any]]:
        primary = list(mode_features[mode_name])
        context = mode_context[mode_name]
        context.sort(
            key=lambda f: str(f["properties"].get("timestamp_utc") or ""),
            reverse=True,
        )
        if mode_name in ("humanitarian", "safety"):
            # docs/fixes.md A-04/A-05: coalesce_security_vessel_episodes()
            # (the `else` branch below) rewrites domain to sanctions/
            # grey_zone as part of building a security episode -- exactly
            # wrong for Safety content. Safety uses the same simple,
            # recency-sorted cap as Humanitarian instead.
            context_cap = max(0, min(_LIVE_WINDOW_LIMIT - len(primary), _LIVE_WINDOW_LIMIT // 2))
            primary.extend(context[:context_cap])
        else:
            # Maritime traffic is vessel-centric: raw anomaly, incident and
            # fusion records for one MMSI become one episode that receives
            # updates.  Group BEFORE applying a display cap; otherwise a burst
            # of duplicate raw signals can evict a valid vessel episode.
            raw_security = [*primary, *context]
            primary = coalesce_security_vessel_episodes(raw_security)
            primary.sort(
                key=lambda feature: str(
                    (feature.get("properties") or {}).get("timestamp_utc") or ""
                ),
                reverse=True,
            )
            # Detailed AIS tracks are the expensive part.  One bounded batch
            # enriches the newest episodes; older cases still retain the line
            # between their own observed alert/update points.
            # Mobility incidents need their recent AIS path even when their
            # first alert is older than a busy anomaly burst. Fill the rest of
            # the bounded batch with the newest vessel episodes.
            candidate_pool = [
                feature
                for feature in primary
                if bool((feature.get("properties") or {}).get("drift_eligible"))
            ] + primary
            track_candidates = []
            candidate_ids: set[str] = set()
            track_budget = 150
            for feature in candidate_pool:
                feature_id = str((feature.get("properties") or {}).get("id") or "")
                if not feature_id or feature_id in candidate_ids:
                    continue
                candidate_ids.add(feature_id)
                track_candidates.append(feature)
                if len(track_candidates) >= track_budget:
                    break
            vessel_mmsis = {
                str((feature.get("properties") or {}).get("linked_mmsi") or "")
                for feature in track_candidates
            }
            try:
                from core.vessels.track_store import track_store

                track_history = track_store.recent_tracks(
                    vessel_mmsis,
                    since=now - timedelta(hours=24),
                    limit_per_mmsi=60,
                )
            except Exception:  # pragma: no cover - feed remains useful without track DB
                track_history = {}
            if track_history:
                enriched = coalesce_security_vessel_episodes(
                    [
                        feature
                        for feature in raw_security
                        if str((feature.get("properties") or {}).get("linked_mmsi") or "")
                        in vessel_mmsis
                    ],
                    track_history=track_history,
                )
                enriched_by_id = {
                    (feature.get("properties") or {}).get("id"): feature
                    for feature in enriched
                }
                primary = [
                    enriched_by_id.get((feature.get("properties") or {}).get("id"), feature)
                    for feature in primary
                ]
        # Live is a timeline: newest source timestamp always wins. Severity
        # remains a visual attribute and filter, never a second sort.
        primary.sort(
            key=lambda f: str(f["properties"].get("timestamp_utc") or ""),
            reverse=True,
        )
        return primary

    features_by_mode = {
        mode_name: finalize(mode_name)
        for mode_name in ("humanitarian", "security", "safety")
    }
    # Canonical case surfaces have two levels:
    # 1) a public MaritimeEpisode dossier may be open while still
    #    evidence_candidate/single-lineage;
    # 2) a published InvestigationHypothesis is the stronger assessed layer.
    # Both use the same episode_id. A hypothesis replaces, rather than
    # duplicates, its open dossier when it becomes publishable.
    for feature in _published_open_episode_features(_LIVE_WINDOW_LIMIT):
        props = feature.get("properties") or {}
        target_mode = "safety" if props.get("maritime_domain") == "safety" else "security"
        existing_ids = {
            str((item.get("properties") or {}).get("episode_id") or item.get("id") or "")
            for item in features_by_mode[target_mode]
        }
        feature_id = str(props.get("episode_id") or feature.get("id") or "")
        if feature_id not in existing_ids:
            features_by_mode[target_mode].append(feature)

    published_hypotheses = _published_security_hypothesis_features(_LIVE_WINDOW_LIMIT)
    security_index = {
        str((item.get("properties") or {}).get("episode_id") or item.get("id") or ""): index
        for index, item in enumerate(features_by_mode["security"])
    }
    for feature in published_hypotheses:
        props = feature.get("properties") or {}
        feature_id = str(props.get("episode_id") or feature.get("id") or "")
        if feature_id in security_index:
            features_by_mode["security"][security_index[feature_id]] = feature
        else:
            security_index[feature_id] = len(features_by_mode["security"])
            features_by_mode["security"].append(feature)

    for mode_name in ("security", "safety"):
        features_by_mode[mode_name].sort(
            key=lambda f: str((f.get("properties") or {}).get("timestamp_utc") or ""),
            reverse=True,
        )
    add_nearby_humanitarian_context(
        features_by_mode["security"], features_by_mode["humanitarian"]
    )
    features_by_mode["humanitarian"] = dedupe_public_case_items(
        features_by_mode["humanitarian"]
    )
    maritime_features = sorted(
        [*features_by_mode["safety"], *features_by_mode["security"]],
        key=lambda f: str(f["properties"].get("timestamp_utc") or ""),
        reverse=True,
    )
    mode_counts = {
        "humanitarian": len(features_by_mode["humanitarian"]),
        "maritime": len(maritime_features),
    }
    domain_counts = {
        "humanitarian": len(features_by_mode["humanitarian"]),
        "safety": len(features_by_mode["safety"]),
        "security": len(features_by_mode["security"]),
    }
    role_counts = {
        "humanitarian_case": 0,
        "humanitarian_observation": 0,
        "maritime_episode": 0,
        "maritime_evidence": 0,
        "operational_signal": 0,
        "maritime_signal": 0,
    }
    for feature in [*features_by_mode["humanitarian"], *maritime_features]:
        role = str((feature.get("properties") or {}).get("live_role") or "")
        if role in role_counts:
            role_counts[role] += 1
    if selected_mode == "all":
        # The public transport cap protects the browser from Maritime volume;
        # it must never hide an eligible humanitarian distress. Humanitarian
        # is therefore unbounded by `limit`, while Safety then Security fill
        # the remaining transport budget. The real population is reported
        # separately in meta.total/mode_counts.
        humanitarian_reserved = list(features_by_mode["humanitarian"])
        maritime_budget = max(0, limit - len(humanitarian_reserved))
        selected_maritime = maritime_features[:maritime_budget]
        features = sorted(
            humanitarian_reserved + selected_maritime,
            key=lambda f: str(f["properties"].get("timestamp_utc") or ""),
            reverse=True,
        )
    elif selected_mode == "maritime":
        features = maritime_features
    else:
        # Internal compatibility only; the public route does not expose a
        # standalone Safety mode.
        features = features_by_mode[selected_mode]
    if since:
        features = [
            feature
            for feature in features
            if str(feature["properties"].get("timestamp_utc") or "") > since
        ]
    if selected_mode != "all":
        features = features[:limit]

    real_total = (
        sum(mode_counts.values()) if selected_mode == "all"
        else (domain_counts["safety"] if selected_mode == "safety" else mode_counts[selected_mode])
    )
    _attach_cross_modal_evidence(features, now=now)

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "schema": "org.seacommons.live-feed/v1",
            "total": real_total,
            "mode": selected_mode,
            "mode_counts": mode_counts,
            "domain_counts": domain_counts,
            "role_counts": role_counts,
            "memory_candidates": len(memory_events),
            "durable_alarm_phone_candidates": len(durable_alarm_phone),
            "humanitarian_candidate_diagnostics": dict(sorted(humanitarian_candidate_drops.items())),
            "durable_sanction_port_call_candidates": len(durable_sanction_port_calls),
            "with_coords": sum(1 for feature in features if feature.get("geometry") is not None),
            "generated_at": datetime.now(UTC).isoformat(),
            "privacy": "published signals only; private identifiers and raw messages excluded",
        },
    }


def public_drift_collection(limit: int = 100) -> dict[str, Any]:
    """Published model geometry linked to received public signals, without raw content."""
    from core.db.store import get_drift

    features: list[dict[str, Any]] = []
    drift_count = 0
    drift_events = {
        event.id: event
        for event in intel_store.persisted_events(
            source_in=["Alarm Phone", "alarm_phone"], max_age_days=30, limit=min(limit * 5, 1000)
        )
    }
    drift_events.update(
        {event.id: event for event in intel_store.events(limit=min(limit * 3, 500))}
    )
    now = datetime.now(UTC)
    by_source: dict[str, list[IntelEvent]] = {}
    for event in drift_events.values():
        by_source.setdefault(event.source, []).append(event)
    for event in drift_events.values():
        # SeaCommons Drift is a humanitarian SAR model only (docs/deep-research-
        # report.md #17, hard requirement). A positive allow-list, not
        # domains_for_mode("humanitarian") -- that set is env-widenable and
        # includes "piracy" by default, so "not security" alone would still
        # let a piracy-domain event carry a drift cone (docs/deep-research-
        # report (2).md's follow-up finding on this exact gate).
        public_event = _public_intel_feature(
            event, allowed_domains=HUMANITARIAN_DRIFT_DOMAINS
        )
        if public_event is None:
            continue
        # Drift is a derived Live product and obeys the exact same rolling
        # 24h surface boundary as its founding distress signal. A needs_review
        # incident can retain that real-world status in Play without keeping an
        # operational trajectory/cone on Live indefinitely.
        if not lifecycle.is_within_live_window(event, now=now):
            continue
        # Once an incident is resolved or archived, the search is over --
        # an active-looking pulsing drift cone still on the map reads as
        # "still adrift, still searching", which is exactly wrong for a
        # case that's already been rescued or gone stale.
        explicit_state = str(event.metadata.get("incident_lifecycle") or "").lower()
        state = explicit_state or lifecycle.distress_lifecycle(
            event,
            now=now,
            same_source=by_source.get(event.source, []),
        )
        if state in _DRIFT_HIDDEN_LIFECYCLES:
            continue
        # Only a real extracted maritime point is a drift origin. A region-only
        # Alarm Phone incident keeps its red search area (signal feed) but must
        # never carry a fabricated trajectory/cone, even if a stale drift_result
        # row from before an OCR upgrade still exists (product policy §1, §11-C).
        from core.intel.drift_service import is_auto_drift_eligible

        eligible, _reason = is_auto_drift_eligible(event)
        if not eligible:
            continue
        # docs/updates.md P0.11: the incident's current_drift_id is the ONLY
        # authority for which job publishes -- never rediscovered from
        # event.metadata["drift_job_id"] (a stale/replayed value could
        # disagree with what the incident actually owns) and never picked
        # arbitrarily from every completed job for this event (the exact
        # anti-pattern P0.11 names: "must not rediscover arbitrary
        # completed jobs"). No pointer set yet (drift never computed, or
        # not yet synced) -- correctly no Drift publishes.
        from core.intel.drift_ownership import get_current_drift_id

        job_id = get_current_drift_id(event.id)
        if not job_id:
            continue
        drift = get_drift(job_id)
        if not drift or not _is_publishable_live_drift(drift):
            continue
        metadata = drift.get("metadata") or {}
        # The drift inherits its origin signal's semantic category (Alarm Phone
        # drift is red because the origin is Alarm Phone), never a severity.
        category = visual_category_fields(
            source=event.source,
            event_type=event.type,
            maritime_domain=event.maritime_domain(),
            humanitarian_case_type=event.metadata.get("humanitarian_case_type"),
            metadata=event.metadata,
        )
        drift_count += 1
        for feature in (drift.get("trajectory"), drift.get("cone_24h")):
            if feature:
                features.append(
                    _public_drift_feature(
                        feature,
                        event_id=event.id,
                        title=event.title,
                        source=event.source,
                        category=category,
                        metadata=metadata,
                    )
                )
        current_estimate = _current_trajectory_estimate(
            drift.get("trajectory") or {},
            event_timestamp=event.timestamp_utc,
        )
        if current_estimate:
            features.append(
                _public_drift_feature(
                    current_estimate,
                    event_id=event.id,
                    title=event.title,
                    source=event.source,
                    category=category,
                    metadata=metadata,
                )
            )
        for feature in (drift.get("impact_point") or {}).get("features", []):
            features.append(
                _public_drift_feature(
                    feature,
                    event_id=event.id,
                    title=event.title,
                    source=event.source,
                    category=category,
                    metadata=metadata,
                )
            )
        if drift_count >= limit:
            break
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "schema": "org.seacommons.live-drift/v1",
            "drifts": drift_count,
            "generated_at": datetime.now(UTC).isoformat(),
            "privacy": "derived geometry and published signal metadata only",
        },
    }
