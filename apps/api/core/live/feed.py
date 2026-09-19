# SPDX-License-Identifier: AGPL-3.0-or-later
"""Privacy-preserving Live projections and feed composition."""

from __future__ import annotations

import logging
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
from core.live.vessel_episodes import (
    add_nearby_humanitarian_context,
    coalesce_security_vessel_episodes,
)

logger = logging.getLogger(__name__)

# Public received/safety types that must survive in-memory deque churn. Raw
# Security detector families are intentionally absent: after the canonical
# cutover they remain internal evidence and public Live reads assessed
# InvestigationHypothesis rows through _published_security_hypothesis_features.
_PUBLIC_DURABLE_TYPES = frozenset({
    "distress", "twitter", "mastodon", "bluesky", "ngo_activity", "news",
    "gdacs", "vessel_incident", "iom_incident",
})

# Candidate and count computation must not depend on the response page size.
# The route itself caps returned features at 500; use that same fixed window
# for both modes, then apply ``limit``/``since`` only to the selected payload.
_LIVE_WINDOW_LIMIT = 500
_LIVE_DURABLE_SCAN_LIMIT = 1500
_LIVE_DURABLE_TYPE_SCAN_LIMIT = 500

# A derived SAR-responder movement cue is operationally useful only while the
# underlying AIS behaviour is fresh. It is not a 24h humanitarian case.
_SAR_ACTIVITY_LIVE_TTL = timedelta(hours=6)


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
        timestamp = props.get("timestamp_utc")
        if not timestamp or not feature.get("geometry"):
            continue
        public = {
            "type": "Feature",
            "id": props["id"],
            "geometry": feature["geometry"],
            "properties": {
                "schema": LIVE_SIGNAL_SCHEMA,
                "id": props["id"],
                "type": "ais_anomaly",
                **category,
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
    by_id = {event.id: event for event in durable_alarm_phone}
    by_id.update(durable_public_by_id)
    # In-memory objects contain the most recent metadata observations and must
    # win over the durable snapshot when both are present.
    by_id.update({event.id: event for event in memory_events})
    events = list(by_id.values())
    now = datetime.now(UTC)
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
            if not lifecycle.is_within_live_window(event, now=now):
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
            # Live is operational only. Terminal/retired real-world statuses
            # belong to Play immediately even when the founding post is recent.
            if incident_state["incident_status"] in {"resolved", "outcome_unknown"}:
                continue
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
            if not lifecycle.is_within_live_window(event, now=now):
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
    # Canonical Security cutover: detector output stays internal evidence;
    # only hypotheses that passed the publication gate enter public Live.
    features_by_mode["security"].extend(
        _published_security_hypothesis_features(_LIVE_WINDOW_LIMIT)
    )
    features_by_mode["security"].sort(
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
