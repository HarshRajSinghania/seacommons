# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure privacy and geometry projections for the public Live contracts."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from core.domain.live_contracts import (
    APPROVED_SOURCE_POLICIES,
    LIVE_SIGNAL_SCHEMA,
    LiveSignalKind,
    PublicationStatus,
    Severity,
    SourcePolicy,
    VerificationStatus,
    validate_live_signal,
)
from core.domain.visual_category import visual_category_fields
from core.intel import lifecycle
from core.intel.assessment import build_assessment
from core.intel.public_geometry import public_geometry_and_precision
from core.intel.public_policy import (
    is_blocked_source,
    is_explicitly_private,
    public_maritime_domains,
)
from core.intel.store import IntelEvent

logger = logging.getLogger(__name__)

_PUBLIC_INTEL_TYPES = frozenset({"distress", "twitter", "mastodon", "ngo_activity"})
# OSINT context types that may appear on the public map when their maritime
# compartment is allow-listed (PUBLIC_MARITIME_DOMAINS). A sanctions / grey-zone
# signal never surfaces on the default (sar, piracy) posture — only the SAR /
# safety / environmental context does.
# ais_spike (routine loiter / stop clusters) is deliberately excluded: it is
# high-volume and low-signal, and would swamp the public feed. Only the
# meaningful AIS derivative — ais_anomaly (spoofing / dark-zone / impossible
# speed) — and the fused alert it may feed are eligible.
_PUBLIC_CONTEXT_TYPES = frozenset(
    {"news", "bluesky", "gdacs", "vessel_incident", "iom_incident",
     "ais_anomaly", "ais_rendezvous", "correlated_alert", "oil_spill",
     # vessel_identity (sanctions/identity findings) and dark_candidate
     # (satellite-vs-AIS mismatch) are Security-mode content -- eligible
     # here, actually reachable only when mode=security opens their domain
     # (sanctions/grey_zone) via domains_for_mode(). Humanitarian mode's
     # allow-list never includes those domains, so this addition changes
     # nothing for the existing default feed.
     "vessel_identity", "dark_candidate"}
)
# Types SeaCommons computes from telemetry (AIS, sensor fusion) rather than
# scrapes — they carry no source_policy but are safe to surface, still subject
# to the domain + geometry gates below.
_SEACOMMONS_DERIVED_TYPES = frozenset(
    {"ais_anomaly", "correlated_alert", "vessel_incident", "vessel_identity", "dark_candidate"}
)
# GDACS event types worth showing on a maritime SAR map (TC cyclone, EQ
# earthquake / tsunami, FL flood, VO volcano) — excludes WF wildfire, DR drought.
_MARITIME_GDACS_TYPES = frozenset({"TC", "EQ", "FL", "VO"})

_SAFETY_OPERATIONAL_LABELS = {
    "aground": "Aground",
    "not_under_command": "Not Under Command",
    "restricted_manoeuvrability": "Restricted Manoeuvrability",
    "restricted_maneuverability": "Restricted Manoeuvrability",
}


def _operational_label(event: IntelEvent, *, resolved_domain: str) -> str | None:
    if event.type == "dsc_distress" or str(event.metadata.get("dsc_category") or "").lower() == "distress":
        return "DSC distress"
    nav_kind = str(event.metadata.get("ais_nav_status_kind") or "").strip().lower()
    if nav_kind in _SAFETY_OPERATIONAL_LABELS:
        return _SAFETY_OPERATIONAL_LABELS[nav_kind]
    if resolved_domain == "safety":
        return "Maritime safety"
    return None


def _input_modality(event: IntelEvent, *, source_policy: str) -> str:
    source = str(event.source or "").strip().lower()
    metadata = event.metadata or {}
    if (
        event.type == "dsc_distress"
        or metadata.get("receiver_id")
        or metadata.get("physical_lineage")
        or source.startswith("radio_receiver:")
    ):
        return "radio"
    if (
        source in {"ais", "aisstream", "aisstream.io"}
        or source.startswith("seacommons ais")
        or metadata.get("ais_nav_status_kind")
        or str(metadata.get("coordinate_source") or "").lower() == "ais_position"
        or event.type in {"ais_anomaly", "vessel_identity", "dark_candidate"}
    ):
        return "ais"
    channel = str(metadata.get("source_channel") or "").strip().lower()
    if channel in {"webhook", "api", "partner"}:
        return "partner"
    if event.type in {"news", "gdacs", "iom_incident"}:
        return "public_feed"
    if event.type in {"distress", "twitter", "mastodon", "ngo_activity"} and source_policy in {
        "official_api", "official_site_embed", "operator_published"
    }:
        return "first_party"
    if source_policy in {"official_api", "official_site_embed"}:
        return "public_feed"
    return "other"
_PUBLIC_METADATA = frozenset(
    {
        "category",
        "coordinate_review_status",
        "coordinate_source",
        "country",
        "dead",
        "distress_classification",
        "drift_status",
        "drift_job_id",
        "first_source_seen_at",
        "incident_id",
        "is_distress",
        "maritime_domain",
        "alert_type",
        "confidence",
        "contributing_sources",
        "contributing_independence_groups",
        "independent_source_count",
        "evidence_count",
        "evidence_stage",
        "live_valid_for_s",
        "source_lineage",
        "activity_kind",
        "observation_type",
        "operator_type",
        "org",
        "vessel_role",
        "normalized_from",
        "possible_response_to",
        "verification_explanation",
        "cluster_id",
        "anomaly_type",
        "ais_nav_status_kind",
        "anomaly_confidence",
        "confidence_v2",
        "sanctions_matched",
        "sanctions",
        "port_call",
        "episode_family",
        "analysis_state",
        "publication_state",
        "resolution_state",
        "lineage_ids",
        "detection_reason",
        "detail",
        "public_summary",
        "live_role",
        "offshore_context",
        "offshore_anomaly_qualified",
        "offshore_reason_codes",
        "offshore_rationale",
        "drift_eligible",
        "drift_event_id",
        "drift_vessel_type",
        "episode_update_count",
        "first_observed_at",
        "in_jamming_zone",
        "infrastructure",
        "jamming_score",
        "last_observed_at",
        "loiter_minutes",
        "observed_track",
        "vessel_name",
        "imo",
        "ship_type",
        "flag",
        "spike_type",
        "last_source_seen_at",
        "location_uncertainty_m",
        "location_status",
        "ocr_queue_state",
        "media_transport",
        "humanitarian_case_id",
        "humanitarian_case_type",
        "humanitarian_status",
        "people_reported",
        "people_precision",
        "verification_level",
        "source_count",
        "ocr_engine",
        "missing",
        "ocr_attempted",
        "platform",
        "region",
        "source_policy",
        "source_scan_count",
        "repost_count",
        "last_repost_at",
        "area_weather_narrowed",
    }
)


def public_archive_event_types() -> frozenset[str]:
    """Event types eligible for the public historical Play projection.

    Play deliberately reuses Live's canonical eligibility set rather than
    maintaining a second hand-written maritime allow-list.
    """
    return _PUBLIC_INTEL_TYPES | _PUBLIC_CONTEXT_TYPES


def public_intel_feature(
    event: IntelEvent, *, allowed_domains: frozenset[str] | None = None
) -> dict[str, Any] | None:
    """Public wrapper around the canonical Live privacy/eligibility projection."""
    return _public_intel_feature(event, allowed_domains=allowed_domains)


def dedupe_public_case_items(items: list[dict[str, Any]], *, window_seconds: int = 120) -> list[dict[str, Any]]:
    """Collapse near-simultaneous Alarm Phone translations in public projections.

    The source monitor can receive language variants as separate posts. They remain
    separate evidence internally, but public case surfaces keep the earlier report
    when count, point and time make them the same operational case.
    """
    def fields(item: dict[str, Any]) -> tuple[str, str, str, Any, str]:
        raw_props = item.get("properties")
        props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else item
        source = str(props.get("source") or "").lower().replace(" ", "_").replace("-", "_")
        title = str(props.get("title") or "")
        timestamp = str(
            props.get("timestamp_utc") or props.get("source_timestamp_utc")
            or props.get("reported_at") or item.get("reported_at") or ""
        )
        geometry = item.get("geometry")
        item_id = str(props.get("id") or props.get("incident_id") or item.get("incident_id") or "")
        return source, title, timestamp, geometry, item_id

    def parsed_time(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None

    def duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
        sa, ta, tsa, ga, _ = fields(a)
        sb, tb, tsb, gb, _ = fields(b)
        if sa != "alarm_phone" or sb != "alarm_phone":
            return False
        ca = re.search(r"\b(\d{1,3})\b", ta)
        cb = re.search(r"\b(\d{1,3})\b", tb)
        if ca is None or cb is None or ca.group(1) != cb.group(1):
            return False
        if ga is None or gb is None or ga.get("type") != "Point" or gb.get("type") != "Point":
            return False
        axy = ga.get("coordinates") or []
        bxy = gb.get("coordinates") or []
        if len(axy) < 2 or len(bxy) < 2:
            return False
        try:
            if abs(float(axy[0]) - float(bxy[0])) > 0.01 or abs(float(axy[1]) - float(bxy[1])) > 0.01:
                return False
        except (TypeError, ValueError):
            return False
        da, db = parsed_time(tsa), parsed_time(tsb)
        return da is not None and db is not None and abs((da - db).total_seconds()) <= window_seconds

    ordered = sorted(
        items,
        key=lambda item: parsed_time(fields(item)[2]) or datetime.max.replace(tzinfo=UTC),
    )
    duplicate_ids: set[str] = set()
    for index, item in enumerate(ordered):
        item_id = fields(item)[4]
        if item_id in duplicate_ids:
            continue
        for later in ordered[index + 1:]:
            later_id = fields(later)[4]
            if later_id and duplicate(item, later):
                duplicate_ids.add(later_id)
    return [item for item in items if fields(item)[4] not in duplicate_ids]


def is_useful_public_case_feature(feature: dict[str, Any] | None) -> bool:
    """Case-surface quality gate applied after privacy/publication projection.

    Neutral AIS status is still projectable as evidence, but a lone transponder
    status is not a public incident. Legacy fusion/security detector rows are
    evidence too; reviewed intelligence reaches public surfaces only through
    the InvestigationHypothesis projection.
    """
    if not feature:
        return False
    props = feature.get("properties") or {}
    event_type = str(props.get("type") or "")
    anomaly_type = str(props.get("anomaly_type") or "")
    if event_type in {"correlated_alert", "dark_candidate"}:
        return False
    if event_type == "vessel_identity" and anomaly_type != "sanctioned_port_call":
        return False
    if event_type == "vessel_identity" and anomaly_type == "sanctioned_port_call":
        raw_call = props.get("port_call")
        call: dict[str, Any] = raw_call if isinstance(raw_call, dict) else {}
        if int(call.get("ais_fixes") or 0) < 2:
            return False
        activity = lifecycle.parse_utc(str(call.get("departed_at") or call.get("last_seen_at") or ""))
        if activity is None or datetime.now(UTC) - activity > timedelta(hours=24):
            return False
    if event_type == "distress" and str(props.get("ais_nav_status_kind") or "") == "distress_beacon":
        beacon_mmsi = str(props.get("linked_mmsi") or props.get("mmsi") or "")
        if not beacon_mmsi.startswith(("970", "972", "974")):
            return False
        last_seen = lifecycle.parse_utc(str(
            props.get("last_observed_at")
            or props.get("source_timestamp_utc")
            or props.get("timestamp_utc")
            or ""
        ))
        if last_seen is None or datetime.now(UTC) - last_seen > timedelta(hours=2):
            # A dedicated AIS-SART/MOB/EPIRB transmission is operational only
            # while it is still being observed. A lone historical ping remains
            # available in Play but must not sit in Live indefinitely.
            return False
        verification = str(props.get("verification_status") or "")
        evidence_stage = str(props.get("evidence_stage") or "")
        independently_corroborated = (
            verification == "multi_source_corroborated"
            or evidence_stage in {"corroborated", "assessed", "confirmed"}
            or int(props.get("independent_source_count") or 0) >= 2
        )
        if not independently_corroborated:
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            if geometry.get("type") == "Point" and len(coordinates) >= 2:
                try:
                    lon, lat = float(coordinates[0]), float(coordinates[1])
                    from core.mda.reference import reference

                    # A fresh dedicated beacon in a port/anchorage or clearly on
                    # land is still a real safety observation, but is too often a
                    # test/maintenance/on-board transmission to be a public Live
                    # casualty without an independent SAR/DSC/human source.
                    if reference.in_port_or_anchorage(lat, lon) or reference.is_land(lat, lon):
                        return False
                except (TypeError, ValueError):
                    pass
                except Exception:
                    # Reference-data failure must not hide a genuine fresh distress
                    # beacon; fail open on context lookup, not on freshness.
                    pass
    if event_type == "ais_anomaly" and not props.get("hypothesis_type"):
        if not (props.get("offshore_anomaly_qualified") and props.get("analysis_state") == "evidence_candidate"):
            return False
        if anomaly_type == "impossible_speed":
            verification = str(props.get("verification_status") or "")
            evidence_count = int(props.get("evidence_count") or 0)
            if verification == "single_source_observed" and evidence_count < 2:
                # A single impossible-speed jump can be one malformed AIS fix.
                # Keep it in Play/evidence, but require a repeated or otherwise
                # corroborated observation before it becomes a public Live case.
                return False
    source = str(props.get("source") or "").lower()
    category = str(props.get("visual_category") or "")
    verification = str(props.get("verification_status") or "")
    if source == "ais" and category == "navigation_casualty" and verification == "ais_transponder":
        independent = int(props.get("independent_source_count") or 0) >= 2
        evidence_stage = str(props.get("evidence_stage") or "")
        if not independent and evidence_stage not in {"corroborated", "assessed", "confirmed"}:
            return False
    return True


def _safe_public_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else ""


def _public_intel_feature(
    event: IntelEvent, *, allowed_domains: frozenset[str] | None = None
) -> dict[str, Any] | None:
    """Convert an internal event to the stable public signal contract.

    allowed_domains defaults to the humanitarian posture (PUBLIC_MARITIME_
    DOMAINS, e.g. sar/piracy/safety) for full backward compatibility with
    every existing caller. Live-mode callers (see feed.py's `mode` param)
    pass a different set to open sanctions/grey_zone content instead,
    without touching the default behaviour.
    """
    from core.intel.analysis_state import annotate_event_analysis

    annotate_event_analysis(event)
    if (
        event.type == "sar_model"
        or (event.title or "").strip().lower() == "computed sar drift product"
    ):
        # Model outputs belong to Play/Engine, never to the received-signal feed.
        return None
    if event.type == "news" and event.verification_status() not in {
        VerificationStatus.MULTI_SOURCE_CORROBORATED.value,
        "confirmed",
    }:
        # A published URL proves provenance, not the claims in the article.
        # Live only carries news with an explicit corroboration decision.
        return None
    publication = str(event.metadata.get("publication_status") or "").lower()
    source_policy = str(event.metadata.get("source_policy") or "").lower()
    if publication == "internal":
        # Explicitly internal analytical episodes never cross the public boundary.
        return None
    if is_blocked_source(event.metadata):
        # Old scraper records may still be persisted; they must never re-enter Live.
        return None
    # Explicit privacy is absolute. An approved transport/source policy may
    # make an otherwise-unlabelled public observation eligible, but it must
    # never override a producer's explicit private decision (RSS/news and
    # direct-message channels rely on this guarantee).
    if is_explicitly_private(event.metadata):
        return None
    domains = allowed_domains if allowed_domains is not None else public_maritime_domains()
    resolved_domain = str(event.maritime_domain() or "sar").strip().lower()
    domain_public = resolved_domain in domains
    # Canonical cutover: raw Security detector output is evidence, not a
    # public case. Grey-zone/sanctions AIS anomalies, fused alerts, identity
    # flags and satellite dark candidates can reach public Live only after
    # they have become a published InvestigationHypothesis.
    offshore_evidence = bool(
        event.metadata.get("offshore_anomaly_qualified")
        and str(event.metadata.get("analysis_state") or "") == "evidence_candidate"
        and publication == "published"
        and event.type in {"ais_anomaly", "ais_rendezvous"}
    )
    if (
        resolved_domain in {"grey_zone", "sanctions"}
        and event.type in {"ais_anomaly", "ais_rendezvous", "correlated_alert", "vessel_identity", "dark_candidate"}
        and not (
            (resolved_domain == "sanctions"
             and event.type == "vessel_identity"
             and str(event.metadata.get("anomaly_type") or "") == "sanctioned_port_call"
             and publication == "published")
            or offshore_evidence
        )
    ):
        return None
    # Security correlated alerts are derived interpretations, not raw facts.
    # Historical records may pre-date an explicit publication_status; fail
    # closed so domain eligibility / source policy / corroboration cannot act
    # as an implicit publication decision. Reviewed/approved records must carry
    # publication_status=published explicitly.
    if (
        event.type == "correlated_alert"
        and resolved_domain in {"grey_zone", "sanctions"}
        and publication != PublicationStatus.PUBLISHED.value
    ):
        return None
    is_derived = event.type in _SEACOMMONS_DERIVED_TYPES
    linked_mmsi = str(event.linked_mmsi or event.metadata.get("mmsi") or "").strip()
    if (
        event.type == "correlated_alert"
        and event.metadata.get("alert_type") in {"infra_proximity", "infrastructure_threat"}
        and not (len(linked_mmsi) == 9 and linked_mmsi.isdigit())
    ):
        # Anonymous GFW loiter detections cannot support professional vessel
        # identity or an AIS trajectory. Keep them as operator research cues,
        # never present them as public vessel cases.
        return None
    if (
        publication != PublicationStatus.PUBLISHED.value
        and source_policy not in APPROVED_SOURCE_POLICIES
        and not (is_derived and domain_public)
    ):
        return None
    type_eligible = (
        event.type in _PUBLIC_INTEL_TYPES
        or (event.type in _PUBLIC_CONTEXT_TYPES and domain_public)
    )
    if not type_eligible and publication != PublicationStatus.PUBLISHED.value:
        return None
    # Feed-volume filter for non-operational OSINT *chatter* — secondary news,
    # social posts, generic GDACS notifications. It reaches the public map only
    # when explicitly published or multi-source corroborated. This is a volume
    # control on secondary reporting, NOT a risk score on maritime
    # intelligence: SeaCommons classifies by category, it does not score
    # (product policy §4). SeaCommons-derived context (ais_anomaly, vessel
    # identity, dark candidate, oil spill, IOM, vessel incident) is the signal
    # itself and passes on its category + domain + type gates alone.
    if (
        event.type in {"news", "bluesky", "gdacs"}
        and publication != PublicationStatus.PUBLISHED.value
        and event.verification_status() not in {
            VerificationStatus.MULTI_SOURCE_CORROBORATED.value,
            "confirmed",
        }
    ):
        return None
    # GDACS: only genuinely SAR-relevant natural hazards (cyclone, coastal
    # quake / tsunami, flood, volcano) — never wildfires / droughts inland.
    if event.type == "gdacs" and str(
        event.metadata.get("gdacs_event_type") or ""
    ).upper() not in _MARITIME_GDACS_TYPES:
        return None
    try:
        canonical_source_policy = (
            SourcePolicy(source_policy).value
            if source_policy
            else SourcePolicy.OPERATOR_PUBLISHED.value
        )
    except ValueError:
        logger.warning("Dropping public event with unknown source policy id=%s", event.id)
        return None
    try:
        severity = Severity(event.severity or Severity.LOW.value).value
    except ValueError:
        severity = Severity.LOW.value
    metadata = {key: event.metadata[key] for key in _PUBLIC_METADATA if key in event.metadata}
    from core.intel.public_policy import compartment_for_domain

    if compartment_for_domain(resolved_domain) == "humanitarian":
        # Humanitarian Live may name a public SAR responder in the title, but
        # never exports its tracker dossier as structured fields.
        for identity_key in ("linked_mmsi", "mmsi", "imo", "ship_type", "flag", "vessel_name"):
            metadata.pop(identity_key, None)
    # Always publish the resolved compartment. This also prevents a legacy raw
    # metadata value from overwriting a compatibility reclassification below.
    metadata["maritime_domain"] = resolved_domain
    if (
        resolved_domain == "sar"
        and event.metadata.get("is_distress")
        and event.metadata.get("tweet_id")
        and "humanitarian_case_id" not in metadata
    ):
        from core.intel.humanitarian import humanitarian_case_metadata

        metadata.update(humanitarian_case_metadata(
            event.text,
            incident_id=str(event.metadata["tweet_id"]),
            source=str(event.metadata.get("tracked_account") or event.source),
            distress=True,
            resolved=(
                str(event.metadata.get("report_kind") or "") == "resolved"
                or lifecycle.has_own_reply_resolution(event)
            ),
        ))
    if event.is_vessel_mobility_incident():
        # Legacy fusion alerts pre-date the dedicated vessel-incident monitor.
        # Tag them so they still coalesce by MMSI and gain the observed AIS
        # path -- but docs/fixes.md P0.2 / A-02: never infer cargo-Drift
        # eligibility here. A NUC/disabled/adrift subject is a Maritime
        # Safety observation, never cargo-Drift eligible (docs/fixes.md
        # invariant), regardless of how old the record is or whether it
        # predates PR #62's explicit drift_eligible=False. This used to
        # setdefault drift_eligible=True / drift_vessel_type="cargo" for any
        # record missing those keys, which is exactly the Safety->Drift
        # behaviour PR #62 removed at the producer -- resurrected here for
        # every record that predates it.
        metadata.setdefault("ais_nav_status_kind", "not_under_command")
        metadata["drift_eligible"] = False
    # MMSI/IMO/name/flag are professional vessel identifiers broadcast in AIS
    # or drawn from the local public registry.  Keeping them on every linked
    # alert is what lets the client join updates into one vessel episode --
    # legitimate for Maritime/security-mode content, but docs/fixes.md
    # M14.4's canonical publication policy is explicit that Public
    # Humanitarian output must never carry MMSI/IMO/tracker-dossier fields
    # (core.intel.publication_policy._VESSEL_IDENTITY_FIELDS), even when a
    # distress/SAR case happens to link a vessel (its own AIS, or a
    # rescuing vessel's). compartment_for_domain() is the same canonical
    # humanitarian/security split publication_policy's targets are built
    # around -- this only withholds the dossier fields on the humanitarian
    # side, the vessel-episode join behaviour for Maritime content below is
    # unchanged.
    mmsi = linked_mmsi
    if len(mmsi) == 9 and mmsi.isdigit() and compartment_for_domain(resolved_domain) != "humanitarian":
        metadata["linked_mmsi"] = mmsi
        metadata["mmsi"] = mmsi
        try:
            from core.vessels.registry import registry
            from core.mda.identity import mmsi_flag

            vessel = (getattr(registry, "_cache", {}) or {}).get(mmsi, {})
            identity_fields = {
                "vessel_name": vessel.get("ship_name"),
                "imo": vessel.get("imo"),
                "ship_type": vessel.get("ship_type"),
                "flag": vessel.get("flag") or mmsi_flag(mmsi),
            }
            for key, value in identity_fields.items():
                if value not in (None, "") and key not in metadata:
                    metadata[key] = value
        except Exception:  # pragma: no cover - registry enrichment is best effort
            pass
    # Unlike the event's own `text` (stripped everywhere on public Live,
    # since it may originate from a private WhatsApp/SMS caller who never
    # consented to publication), a thread_reposts `note` only ever comes from
    # the tracked account's OWN public quote/reply to its OWN tweet — already
    # readable by anyone on X. Without it the public "Update" panel would
    # show a link with no indication of what the update actually says, which
    # defeats the point of surfacing it at all.
    thread_reposts = event.metadata.get("thread_reposts")
    if thread_reposts:
        metadata["thread_reposts"] = [
            {
                "tweet_id": r.get("tweet_id"),
                "posted_at": r.get("posted_at"),
                "url": r.get("url"),
                "kind": r.get("kind"),
                "note": r.get("note"),
            }
            for r in thread_reposts
        ]

    # docs/prompt.md P1: expose the SAFE subset of media evidence (durable
    # thumbnail + provenance), never the raw ocr_text or coordinate candidates.
    media_evidence = event.metadata.get("media_evidence")
    if media_evidence:
        from core.intel.media_evidence import MediaEvidence

        safe = []
        for entry in media_evidence:
            if isinstance(entry, dict):
                fields = {k: entry.get(k) for k in MediaEvidence.__dataclass_fields__}
                safe.append(MediaEvidence(**fields).public_dict())
        if safe:
            metadata["media_evidence"] = safe
    if event.metadata.get("media_outcome"):
        metadata["media_outcome"] = event.metadata["media_outcome"]
    # SeaCommons-derived MDA events (spoofing, sanctions, vessel incidents)
    # never went through core.intel.lifecycle's distress state machine --
    # they had no incident_lifecycle at all and sat "active" forever, even
    # a week later. There's no reply-thread to detect resolution from (no
    # human posts an update when a vessel resumes normal AIS behaviour), so
    # this only tracks staleness, same ARCHIVE_AFTER_HOURS threshold as
    # distress markers: unrefreshed past that window is no longer worth
    # highlighting as current, though it stays visible and searchable.
    lifecycle_state = None
    explicit_lifecycle = str(event.metadata.get("incident_lifecycle") or "").lower()
    if explicit_lifecycle in {"active", "resolved", "needs_review", "archived"}:
        lifecycle_state = explicit_lifecycle
    elif is_derived:
        observed = lifecycle.parse_utc(event.timestamp_utc)
        if observed is not None:
            age_hours = (datetime.now(UTC) - observed).total_seconds() / 3600
            lifecycle_state = (
                "archived" if age_hours >= lifecycle.ARCHIVE_AFTER_HOURS else "active"
            )

    # docs/fixes.md M0.2: case-specific EventAssessment, projected as a
    # nested `assessment` object so ConePanel can stop using
    # descriptionOf(props.type) as the event-specific Interpretation.
    # build_assessment() returns None for a kind it has no assessor for
    # (v0 scope: not_under_command/aground/restricted_manoeuvrability) --
    # that event omits the block entirely rather than inventing generic
    # prose (docs/fixes.md M0.2 required behaviour).
    assessment = build_assessment(event)
    assessment_block = (
        {
            "observation": assessment.observation,
            "interpretation": assessment.interpretation,
            "evidence_level": assessment.evidence_level,
            "confidence": assessment.confidence,
            "confidence_basis": assessment.confidence_basis,
            "supporting_evidence": assessment.supporting_evidence,
            "contradicting_evidence": assessment.contradicting_evidence,
            "caveats": assessment.caveats,
            "recommended_action": assessment.recommended_action,
            "rule_ids": assessment.rule_ids,
            "classification_version": assessment.classification_version,
        }
        if assessment is not None
        else None
    )

    geometry, location_precision = public_geometry_and_precision(event)
    # Canonical semantic visual taxonomy. Colour/identity is a pure function of
    # category — never severity, OCR confidence or lifecycle. Alarm Phone is
    # always `humanitarian_alarm_phone` (red).
    category = visual_category_fields(
        source=event.source,
        event_type=event.type,
        maritime_domain=resolved_domain,
        humanitarian_case_type=metadata.get("humanitarian_case_type"),
        metadata=metadata,
    )
    operational_label = _operational_label(event, resolved_domain=resolved_domain)
    input_modality = _input_modality(event, source_policy=canonical_source_policy)
    anomaly_type = str(metadata.get("anomaly_type") or "")
    if anomaly_type == "sanctioned_port_call":
        metadata["live_role"] = "maritime_episode"
    elif metadata.get("offshore_anomaly_qualified") and metadata.get("analysis_state") == "evidence_candidate":
        metadata["live_role"] = "maritime_evidence"
    elif str(metadata.get("ais_nav_status_kind") or "") == "distress_beacon":
        metadata["live_role"] = "operational_signal"
    elif (
        compartment_for_domain(resolved_domain) == "humanitarian"
        and event.type == "ngo_activity"
        and metadata.get("observation_type") == "sar_responder_activity"
    ):
        metadata["live_role"] = "humanitarian_observation"
    elif compartment_for_domain(resolved_domain) == "humanitarian":
        metadata["live_role"] = "humanitarian_case"
    else:
        metadata.setdefault("live_role", "maritime_signal")
    if not metadata.get("public_summary"):
        if anomaly_type == "sanctioned_port_call" and isinstance(metadata.get("port_call"), dict):
            call = metadata["port_call"]
            dwell = call.get("dwell_minutes")
            dwell_text = f"{dwell:g} minutes" if isinstance(dwell, (int, float)) else "a sustained period"
            metadata["public_summary"] = (
                f"AIS-derived port stay at {call.get('port') or 'a known port'}; "
                f"{int(call.get('ais_fixes') or 0)} AIS fixes over {dwell_text}. "
                "The vessel identity matches a sanctions list on a strong identifier. "
                "This does not by itself establish sanctions evasion."
            )
        elif str(metadata.get("ais_nav_status_kind") or "") == "distress_beacon":
            beacon_mmsi = str(metadata.get("linked_mmsi") or metadata.get("mmsi") or event.linked_mmsi or "")
            beacon_kind = (
                "AIS-SART" if beacon_mmsi.startswith("970")
                else "AIS-MOB" if beacon_mmsi.startswith("972")
                else "AIS-EPIRB" if beacon_mmsi.startswith("974")
                else "AIS safety"
            )
            metadata["public_summary"] = (
                f"Dedicated {beacon_kind} identity {beacon_mmsi or 'unknown'} is transmitting a distress signal. "
                "This is an operational AIS safety signal, not independent confirmation of a casualty."
            )
        elif metadata.get("offshore_anomaly_qualified"):
            metadata["public_summary"] = str(metadata.get("offshore_rationale") or metadata.get("detection_reason") or "Offshore anomaly retained for investigation.")[:600]
        elif assessment_block is not None:
            metadata["public_summary"] = assessment_block.get("observation") or assessment_block.get("interpretation")
        elif metadata.get("detection_reason"):
            metadata["public_summary"] = str(metadata.get("detection_reason"))[:600]
        elif event.type in {"twitter", "mastodon", "bluesky", "ngo_activity"}:
            metadata["public_summary"] = str(event.title or "")[:600]
    feature = {
        "type": "Feature",
        "id": f"intel:{event.id}",
        "geometry": geometry,
        "properties": {
            "schema": LIVE_SIGNAL_SCHEMA,
            "id": f"intel:{event.id}",
            "type": event.type,
            **category,
            "kind": (
                LiveSignalKind.DISTRESS.value
                if event.tier() == "operational"
                else LiveSignalKind.CONTEXT.value
            ),
            "severity": severity,
            "tier": event.tier(),
            "priority": event.priority(),
            "maritime_domain": event.maritime_domain(),
            "verification_status": event.verification_status(),
            "publication_status": PublicationStatus.PUBLISHED.value,
            "source_policy": canonical_source_policy,
            "input_modality": input_modality,
            **({"operational_label": operational_label} if operational_label else {}),
            "title": (event.title or "Maritime signal")[:255],
            # Public Live deliberately excludes raw text and author identifiers.
            "text": "",
            "url": _safe_public_url(event.url),
            "source": (event.source or event.type or "public feed")[:64],
            "timestamp_utc": event.timestamp_utc,
            "source_timestamp_utc": event.timestamp_utc,
            "received_at": event.metadata.get("first_source_seen_at") or event.timestamp_utc,
            "location_precision": location_precision,
            **({"incident_lifecycle": lifecycle_state} if lifecycle_state else {}),
            **({"assessment": assessment_block} if assessment_block is not None else {}),
            **metadata,
        },
    }
    try:
        return validate_live_signal(feature)
    except ValueError:
        logger.warning("Dropping event that violates the public Live contract id=%s", event.id)
        return None


def _approximate_public_point(signal_id: str, lat: float, lon: float) -> tuple[float, float]:
    """Deterministically displace sensitive inbound coordinates by 0.8-2.5 km."""
    digest = hashlib.blake2s(signal_id.encode(), digest_size=8).digest()
    angle = int.from_bytes(digest[:4], "big") / (2**32) * 2 * math.pi
    radius_m = 800 + int.from_bytes(digest[4:], "big") / (2**32) * 1700
    lat_offset = math.sin(angle) * radius_m / 111_320
    lon_scale = max(0.2, math.cos(math.radians(lat)))
    lon_offset = math.cos(angle) * radius_m / (111_320 * lon_scale)
    return round(lat + lat_offset, 5), round(lon + lon_offset, 5)


def _public_drift_feature(
    feature: dict[str, Any],
    *,
    event_id: str,
    title: str,
    source: str,
    category: dict[str, str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    properties = feature.get("properties") or {}
    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": {
            "type": properties.get("type"),
            "horizon_h": properties.get("horizon_h"),
            "radius_m": properties.get("radius_m"),
            "timestamps_utc": properties.get("timestamps_utc"),
            "speed_ms": properties.get("speed_ms"),
            "speed_kn": properties.get("speed_kn"),
            "course_deg": properties.get("course_deg"),
            "distance_m": properties.get("distance_m"),
            "mean_speed_ms": properties.get("mean_speed_ms"),
            "max_speed_ms": properties.get("max_speed_ms"),
            "sample_interval_s": properties.get("sample_interval_s"),
            "sample_count": properties.get("sample_count"),
            "elapsed_hours": properties.get("elapsed_hours"),
            "estimate_time_utc": properties.get("estimate_time_utc"),
            "trajectory_state": properties.get("trajectory_state"),
            "intel_event_id": event_id,
            "intel_title": title[:80],
            "intel_source": source[:64],
            # Drift colour inherits its origin signal's category. No severity.
            "origin_category": category.get("visual_category"),
            "visual_category": category.get("visual_category"),
            "visual_color": category.get("visual_color"),
            "category_label": category.get("category_label"),
            "auto_drift": True,
            "publication_status": PublicationStatus.PUBLISHED.value,
            "trajectory_kind": "model_forecast",
            "observed_track": False,
            "model": metadata.get("model"),
            "forcing_resolution": metadata.get("forcing_resolution"),
            "forcing_quality": metadata.get("forcing_quality"),
            "verification_status": VerificationStatus.MODELLED_SPATIOTEMPORAL.value,
        },
    }


def _current_trajectory_estimate(
    trajectory: dict[str, Any],
    *,
    event_timestamp: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Interpolate the modelled position at wall-clock time."""
    geometry = trajectory.get("geometry") or {}
    properties = trajectory.get("properties") or {}
    coordinates = geometry.get("coordinates") or []
    timestamps = properties.get("timestamps_utc") or []
    if len(coordinates) < 2 or len(timestamps) != len(coordinates):
        return None
    try:
        parsed_times = [datetime.fromisoformat(str(value)).astimezone(UTC) for value in timestamps]
        event_time = datetime.fromisoformat(event_timestamp)
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        event_time = event_time.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        return None
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current <= parsed_times[0]:
        coordinate = coordinates[0]
        state = "before_model_start"
    elif current >= parsed_times[-1]:
        coordinate = coordinates[-1]
        state = "model_horizon_reached"
    else:
        upper = next(index for index, value in enumerate(parsed_times) if value >= current)
        lower = upper - 1
        span = max(1.0, (parsed_times[upper] - parsed_times[lower]).total_seconds())
        ratio = (current - parsed_times[lower]).total_seconds() / span
        coordinate = [
            float(coordinates[lower][0])
            + (float(coordinates[upper][0]) - float(coordinates[lower][0])) * ratio,
            float(coordinates[lower][1])
            + (float(coordinates[upper][1]) - float(coordinates[lower][1])) * ratio,
        ]
        state = "interpolated"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coordinate[:2]},
        "properties": {
            "type": "current_estimate",
            "elapsed_hours": round(max(0.0, (current - event_time).total_seconds() / 3600), 2),
            "estimate_time_utc": current.isoformat(),
            "trajectory_state": state,
        },
    }


def _is_publishable_live_drift(drift: dict[str, Any]) -> bool:
    """Only expose model runs backed by varying forcing, never demo fallbacks."""
    metadata = drift.get("metadata") or {}
    trajectory = drift.get("trajectory") or {}
    properties = trajectory.get("properties") or {}
    coordinates = (trajectory.get("geometry") or {}).get("coordinates") or []
    return bool(
        drift.get("status") == "completed"
        and str(metadata.get("model") or "").startswith("OpenDrift ")
        and metadata.get("forcing_quality") in {
            "spatiotemporal",  # persisted legacy runs
            "observed-spatiotemporal",
        }
        and metadata.get("operational_use") is True
        and len(coordinates) >= 2
        and len(properties.get("timestamps_utc") or []) == len(coordinates)
        and len(properties.get("speed_ms") or []) == len(coordinates)
    )
