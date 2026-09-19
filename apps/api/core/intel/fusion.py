# SPDX-License-Identifier: AGPL-3.0-or-later
"""OSINT cross-source fusion — turns independent signals into correlated alerts.

Every intel event funnels through ``intel_store.add()``; this module subscribes
to that single fan-out point (``intel_store.subscribe``) and, for each new
positioned event, runs a small set of correlation rules against the recent
event window. A rule that fires:

  * emits a ``correlated_alert`` IntelEvent back through ``intel_store.add()``
    (so it reaches the map / feed / WebSocket / DB like any other event),
  * links the contributing events onto a case it auto-opens
    (``core.cases.service.open_case`` + ``case_intel_events``),
  * fires an outbound notification (``core.notifications.notify_alert``).

v1 rules:
  1. SAR multi-source     — reuses ``triangulation.evaluate`` (>=2 independent
     channels agree on a place/time) → domain ``sar``.
  2. Dark-fleet / spoofing — two distinct AIS anomalies for one MMSI inside a
     window → domain ``sanctions``.
  3. Grey-zone / infra    — an AIS gap/loiter close to offshore infrastructure
     → domain ``grey_zone``.

The engine is intentionally separate from ``core.anomaly.correlation`` (physical
sensor fusion); they share the weighting *pattern*, not the channels.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from core.config import config
from core.geo import cluster_key, haversine_km
from core.intel.evidence_lineage import lineage_for_event
from core.intel.store import IntelEvent, intel_store

logger = logging.getLogger(__name__)

ALERT_TYPE = "correlated_alert"

_AIS_KINDS = {"ais_anomaly", "ais_spike"}
_SPOOFING_ANOMALIES = {
    "dark_zone_entry", "impossible_speed", "position_jump",
    "gap", "long_gap", "zone_incursion", "ais_rendezvous", "sdn_match",
}
_GREY_ZONE_ANOMALIES = {"gap", "long_gap", "loiter", "dark_zone_entry", "cable_proximity"}

_DOMAIN_CASE_TYPE = {
    "sar": "distress_sar",
    "sanctions": "sanctions_watch",
    "grey_zone": "subsea_infrastructure",
    "safety": "vessel_incident",
    "piracy": "piracy_incident",
}

# A few subsea cable / pipeline corridors in the central + eastern Med, as
# ordered (lat, lon) waypoints. Distance is measured to the nearest segment.
_SUBSEA_CORRIDORS: list[tuple[str, list[tuple[float, float]]]] = [
    ("Melita-1 / Malta-Sicily", [(37.05, 14.50), (35.90, 14.45)]),
    ("Greenstream pipeline (Mellitah-Gela)", [(32.90, 13.30), (37.07, 14.25)]),
    ("EllaLink / IMEWE Sicily approach", [(37.50, 15.10), (36.70, 15.60)]),
    ("SEA-ME-WE Egypt landing (Alexandria)", [(31.20, 29.90), (33.40, 30.60)]),
]


@dataclass
class FusionSignal:
    event_id: str
    kind: str
    anomaly_type: str
    lat: Optional[float]
    lon: Optional[float]
    ts: float  # epoch seconds
    mmsi: str = ""
    imo: str = ""
    source: str = ""
    source_family: str = "unknown"
    independence_group: str = "unknown"
    sensor_family: str = "unknown"
    severity: str = ""
    weight: float = 0.0


@dataclass
class FusedAlert:
    alert_type: str
    domain: str
    severity: str
    confidence: float
    lat: Optional[float]
    lon: Optional[float]
    ts: float
    contributing_event_ids: list[str]
    contributing_sources: list[str]
    summary: str
    open_case: bool = True
    case_type: str = "unspecified"
    vessel_mmsi: str = ""
    cluster_id: str = field(default="")

    def __post_init__(self) -> None:
        if self.cluster_id:
            return
        # Prefer an identity that is stable no matter which signal of the set
        # triggered the rule (so two near-simultaneous evaluations of the same
        # incident collapse to one alert): the sorted contributing event ids,
        # else a coarse place/time bucket.
        if self.contributing_event_ids:
            key = ",".join(sorted(self.contributing_event_ids))
        else:
            key = cluster_key(self.lat, self.lon, self.ts)
        self.cluster_id = f"{self.alert_type}:{key}"


# ── timestamps ────────────────────────────────────────────────────────────────

def _epoch(value: str) -> float:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).timestamp()


# ── normalisation ─────────────────────────────────────────────────────────────

def normalize(event: IntelEvent) -> Optional[FusionSignal]:
    """Project an IntelEvent onto the fields the rules need. None → skip."""
    if event.type == ALERT_TYPE:
        return None  # never correlate an alert with itself
    # vessel_identity events (a sanctions hit / duplicate MMSI) are worth
    # correlating even without a position — everything else needs one.
    if (event.lat is None or event.lon is None) and event.type != "vessel_identity":
        return None
    meta = event.metadata or {}
    anomaly_type = str(
        meta.get("anomaly_type")
        or meta.get("spike_type")
        or meta.get("subtype")
        or meta.get("ais_nav_status_kind")
        or ""
    ).lower()
    mmsi = str(event.linked_mmsi or meta.get("mmsi") or meta.get("MMSI") or "").strip()
    imo = str(meta.get("imo") or meta.get("IMO") or "").strip()
    lineage = lineage_for_event(event)
    return FusionSignal(
        event_id=event.id,
        kind=event.type,
        anomaly_type=anomaly_type,
        lat=float(event.lat) if event.lat is not None else None,
        lon=float(event.lon) if event.lon is not None else None,
        ts=_epoch(event.timestamp_utc),
        mmsi=mmsi,
        imo=imo,
        source=event.source or event.type,
        source_family=lineage.source_family,
        independence_group=lineage.independence_group,
        sensor_family=lineage.sensor_family,
        severity=event.severity or "",
    )


def _recent_signals(exclude_id: str, limit: int = 600) -> list[FusionSignal]:
    out: list[FusionSignal] = []
    for candidate in intel_store.events(limit=limit):
        if candidate.id == exclude_id:
            continue
        sig = normalize(candidate)
        if sig is not None:
            out.append(sig)
    return out


def verification_for_event_ids(event_ids: list[str]) -> tuple[str, list[str], int]:
    """Classify corroboration from evidence lineage, never detector count."""
    groups: set[str] = set()
    found = 0
    for event_id in dict.fromkeys(event_ids):
        event = intel_store.get_durable(event_id)
        if event is None:
            continue
        found += 1
        group = lineage_for_event(event).independence_group
        if group != "unknown":
            groups.add(group)
    ordered = sorted(groups)
    if len(ordered) >= 2:
        return "multi_source_corroborated", ordered, found
    if found >= 2:
        return "single_source_multi_indicator", ordered, found
    return "single_source_observed", ordered, found


def has_independent_corroboration(event_ids: list[str]) -> bool:
    status, _groups, _count = verification_for_event_ids(event_ids)
    return status == "multi_source_corroborated"


def verification_explanation(status: str, groups: list[str], evidence_count: int) -> str:
    if status == "multi_source_corroborated":
        return (
            f"{evidence_count} evidence items across {len(groups)} independent evidence lineages"
        )
    if status == "single_source_multi_indicator":
        group_count = max(1, len(groups))
        noun = "lineage" if group_count == 1 else "lineages"
        return (
            f"{evidence_count} indicators from {group_count} evidence {noun}; "
            "independent corroboration not established"
        )
    if groups:
        return "1 observation from 1 evidence lineage; independent corroboration not established"
    return "source lineage unresolved; independent corroboration not established"


# ── rules ─────────────────────────────────────────────────────────────────────

def _rule_sar_multisource(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    from core.intel import triangulation

    summary = triangulation.evaluate(event)
    if not summary:
        return None
    confidence = float(summary.get("corroboration_confidence") or 0.0)
    sources = list(summary.get("corroborating_sources") or [])
    ids = list(summary.get("corroborating_event_ids") or [event.id])
    return FusedAlert(
        alert_type="sar_corroborated",
        domain="sar",
        severity="high",
        confidence=round(confidence, 3),
        lat=new.lat, lon=new.lon, ts=new.ts,
        contributing_event_ids=ids,
        contributing_sources=sources,
        summary="Multi-source SAR corroboration: " + ", ".join(sources),
        case_type="distress_sar",
    )


def _rule_spoofing(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    if new.kind not in _AIS_KINDS or not new.mmsi:
        return None
    if new.anomaly_type not in _SPOOFING_ANOMALIES:
        return None
    window = config.FUSION_SPOOFING_WINDOW_S
    radius = config.FUSION_SPOOFING_RADIUS_KM
    for other in _recent_signals(new.event_id):
        if other.kind not in _AIS_KINDS or other.mmsi != new.mmsi:
            continue
        if other.anomaly_type not in _SPOOFING_ANOMALIES:
            continue
        if other.anomaly_type == new.anomaly_type:
            continue  # need two *distinct* kinds of evidence
        if abs(other.ts - new.ts) > window:
            continue
        if haversine_km(new.lat, new.lon, other.lat, other.lon) > radius:
            continue
        evidence = {new.anomaly_type, other.anomaly_type}
        confidence = round(min(0.95, 0.45 + 0.2 * len(evidence)), 3)
        ids = [new.event_id, other.event_id]
        return FusedAlert(
            alert_type="spoofing",
            domain="grey_zone",
            severity="high",
            confidence=confidence,
            lat=new.lat, lon=new.lon, ts=new.ts,
            contributing_event_ids=ids,
            contributing_sources=sorted({new.source, other.source}),
            summary=(
                f"MMSI {new.mmsi}: {other.anomaly_type} + {new.anomaly_type} "
                f"within {int(window // 3600)}h"
            ),
            open_case=has_independent_corroboration(ids),
            case_type="monitoring",
            vessel_mmsi=new.mmsi,
        )
    return None


def _nearest_infrastructure(lat: float, lon: float, max_km: float) -> Optional[dict]:
    """Nearest subsea cable / pipeline / platform, from the MDA reference index
    (real geometry from EMODnet / submarinecablemap, with a bundled fallback)."""
    try:
        from core.mda.reference import reference

        hit = reference.nearest_infrastructure(lat, lon, max_km=max_km)
        if hit is not None:
            return {"name": f"{hit.name} ({hit.kind})", "distance_km": hit.distance_km,
                    "kind": hit.kind, "inside": hit.inside}
    except Exception:  # pragma: no cover - fall back to the bundled corridors
        pass
    best: Optional[dict] = None
    for name, waypoints in _SUBSEA_CORRIDORS:
        for i in range(len(waypoints) - 1):
            dist = _point_to_segment_km(lat, lon, waypoints[i], waypoints[i + 1])
            if dist <= max_km and (best is None or dist < best["distance_km"]):
                best = {"name": name, "distance_km": dist, "inside": False}
    return best


def _point_to_segment_km(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Approximate distance from a point to a short segment (planar, Med-scale)."""
    ax, ay = a[1], a[0]
    bx, by = b[1], b[0]
    px, py = lon, lat
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / denom))
    cx, cy = ax + t * abx, ay + t * aby
    return haversine_km(lat, lon, cy, cx)


def _rule_grey_zone(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    if new.kind not in _AIS_KINDS:
        return None
    if new.anomaly_type not in _GREY_ZONE_ANOMALIES:
        return None
    hit = _nearest_infrastructure(new.lat, new.lon, config.FUSION_INFRA_PROXIMITY_KM)
    if hit is None:
        return None

    ids = [new.event_id]
    sources = {new.source}
    high_specificity_identity = False
    for other in _recent_signals(new.event_id):
        if not other.mmsi or other.mmsi != new.mmsi:
            continue
        relevant_ais = (
            other.kind == "ais_anomaly"
            and other.anomaly_type in ("gap", "long_gap", "cable_proximity", "loiter")
        )
        if other.kind != "vessel_identity" and not relevant_ais:
            continue
        ids.append(other.event_id)
        sources.add(other.source)
        high_specificity_identity = other.kind == "vessel_identity"
        break

    independently_corroborated = has_independent_corroboration(ids)
    case_supported = high_specificity_identity or independently_corroborated
    return FusedAlert(
        alert_type="infrastructure_threat" if case_supported else "infra_proximity",
        domain="grey_zone",
        severity="high" if case_supported else "medium",
        confidence=round(
            max(0.4, 0.75 - hit["distance_km"] / 40.0) + 0.15 * case_supported, 3
        ),
        lat=new.lat, lon=new.lon, ts=new.ts,
        contributing_event_ids=ids,
        contributing_sources=sorted(sources),
        summary=(
            f"AIS {new.anomaly_type} inside {hit['name']}"
            if hit.get("inside")
            else f"AIS {new.anomaly_type} within {hit['distance_km']:.1f} km of {hit['name']}"
        ),
        open_case=case_supported,
        case_type="subsea_infrastructure",
        vessel_mmsi=new.mmsi,
    )


# A self-reported navigational status (not_under_command / aground /
# restricted_manoeuvrability -- "disabled"/"adrift" are the same observation
# under other names used upstream) is a Maritime Safety observation, never
# a Maritime Intelligence hypothesis (docs/fixes.md Global Constraints,
# core.intel.service_taxonomy). It still gets its own single-source
# vessel_casualty case below (an operator legitimately wants to review a
# sustained NUC report) -- what changed is that _rule_single_source no
# longer tags that case grey_zone, and there used to be a second rule here
# (_rule_vessel_mobility_episode) that correlated a nav-status report with
# an unrelated AIS movement anomaly (a gap, a position jump, ...) on the
# same MMSI into a single "vessel_mobility_anomaly" grey_zone alert from
# nav-status evidence alone. That was removed: fusing a benign self-report
# with an independent, equally-benign-until-proven-otherwise movement signal
# is exactly the single-signal promotion to intelligence docs/fixes.md
# prohibits, not the multi-source corroboration _rule_maritime_strike (below)
# legitimately requires before treating the same nav-status kinds as
# corroborating evidence for a much rarer, already evidence-gated hypothesis.
_GROUNDING_SUBTYPES = {"aground", "grounding", "not_under_command", "disabled", "adrift"}


def _rule_single_source(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    """Sources with no correlation partner still need a case: a serious vessel
    incident, or a high-severity maritime natural-hazard alert near the AOI.
    """
    from core.intel.landmask import in_operational_region

    if not in_operational_region(new.lat, new.lon):
        return None

    if new.kind == "vessel_incident" and new.anomaly_type in _GROUNDING_SUBTYPES:
        # docs/fixes.md M-04: a self-reported nav status (not_under_command /
        # disabled / adrift, same as aground/grounding) is Maritime Safety,
        # never Maritime Intelligence -- this used to be "grey_zone" for the
        # mobility subtypes specifically.
        return FusedAlert(
            alert_type="vessel_casualty",
            domain="safety",
            severity=new.severity or "high",
            confidence=0.7,
            lat=new.lat, lon=new.lon, ts=new.ts,
            contributing_event_ids=[new.event_id],
            contributing_sources=[new.source],
            # event.title is already plain-language ("Vessel unable to
            # manoeuvre — NAME") — don't re-prepend the raw technical
            # anomaly_type in front of it, that just duplicates the same
            # information in jargon.
            summary=event.title[:150],
            case_type="vessel_incident",
            vessel_mmsi=new.mmsi,
        )

    if new.kind == "gdacs" and new.severity in {"high", "critical"}:
        return FusedAlert(
            alert_type="natural_hazard",
            domain="safety",
            severity=new.severity,
            confidence=0.55,
            lat=new.lat, lon=new.lon, ts=new.ts,
            contributing_event_ids=[new.event_id],
            contributing_sources=[new.source],
            summary=f"GDACS {new.severity} hazard near the operational area — {event.title[:120]}",
            open_case=False,
            case_type="monitoring",
        )
    return None


def _rule_dark_sts(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    """A ship-to-ship rendezvous (core/mda/watch.scan_rendezvous) — always an
    alert; opens a sanctions case when it involves a tanker, a dark party, or
    sits in a known STS zone."""
    if new.kind != "ais_rendezvous":
        return None
    meta = event.metadata or {}
    tanker = bool(meta.get("tanker"))
    dark = bool(meta.get("dark"))
    zone = meta.get("sts_zone")
    vessels = meta.get("vessels") or []
    ids = [new.event_id]
    # fold in a matching sanctions / identity flag on either MMSI
    mmsis = {v.get("mmsi") for v in vessels} | {new.mmsi}
    sanctioned = False
    for other in _recent_signals(new.event_id):
        if other.kind == "vessel_identity" and other.mmsi in mmsis:
            ids.append(other.event_id)
            sanctioned = True
    # A sustained AIS-only proximity pair is still one telemetry lineage.
    # Do not manufacture a second "fusion alert" unless the rendezvous has
    # contextual specificity (tanker/dark/known STS zone) or independent
    # sanctions identity evidence.
    if not (tanker or dark or zone or sanctioned):
        return None
    confidence = round(min(0.95, 0.5 + 0.15 * tanker + 0.15 * dark + 0.1 * bool(zone) + 0.2 * sanctioned), 3)
    return FusedAlert(
        alert_type="dark_sts" if dark else "sts_transfer",
        domain="sanctions" if sanctioned else "grey_zone",
        severity="high" if (tanker or dark or sanctioned) else "medium",
        confidence=confidence,
        lat=new.lat, lon=new.lon, ts=new.ts,
        contributing_event_ids=ids,
        contributing_sources=sorted({new.source} | {"mda"}),
        summary=(f"{'Dark ' if dark else ''}possible ship-to-ship transfer"
                 + (f" in {zone}" if zone else "") + f": {event.title[:120]}"),
        open_case=bool(tanker or dark or zone or sanctioned),
        case_type="sanctions_watch" if sanctioned else "dark_rendezvous",
        vessel_mmsi=new.mmsi,
    )


def _rule_identity_fraud(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    """A sanctioned vessel sighting, a duplicate MMSI, or an identity anomaly
    corroborated by a second signal on the same hull."""
    if new.kind != "vessel_identity":
        return None
    meta = event.metadata or {}
    atype = new.anomaly_type
    if atype in ("sdn_match", "mmsi_duplicate"):
        # A sanctioned vessel simply transiting the Med is expected — alert, but
        # only open a case for a duplicate MMSI (a real fraud signal) or a
        # sanctioned vessel that ALSO shows an anomaly (gap / STS / spoof).
        corroborated = atype == "mmsi_duplicate" or any(
            s.mmsi == new.mmsi and s.kind in ("ais_anomaly", "ais_rendezvous")
            for s in _recent_signals(new.event_id))
        return FusedAlert(
            alert_type=atype,
            domain="sanctions",
            severity="high",
            confidence=0.9 if atype == "sdn_match" else 0.75,
            # None, never 0.0 -- (0, 0) is a real point off the Gulf of
            # Guinea, not "no position yet". collect_mda_anomalies() backfills
            # from a contributing raw event's position when one becomes
            # available instead.
            lat=new.lat,
            lon=new.lon,
            ts=new.ts,
            contributing_event_ids=[new.event_id],
            contributing_sources=[new.source],
            summary=event.title[:180],
            open_case=corroborated,
            case_type="sanctions_watch",
            vessel_mmsi=new.mmsi,
        )
    # a weaker identity anomaly needs a corroborating anomaly on the same MMSI
    partners = [s for s in _recent_signals(new.event_id)
                if s.mmsi and s.mmsi == new.mmsi
                and s.kind in ("ais_anomaly", "ais_rendezvous")]
    if not partners:
        return None
    return FusedAlert(
        alert_type="identity_fraud",
        domain="sanctions",
        severity="high",
        confidence=0.7,
        lat=new.lat if new.lat is not None else partners[0].lat,
        lon=new.lon if new.lon is not None else partners[0].lon,
        ts=new.ts,
        contributing_event_ids=[new.event_id, partners[0].event_id],
        contributing_sources=sorted({new.source, partners[0].source}),
        summary=f"Identity anomaly + {partners[0].anomaly_type} on MMSI {new.mmsi}",
        open_case=True,
        case_type="sanctions_watch",
        vessel_mmsi=new.mmsi,
    )


_STRIKE_KINDS = {"conflict_event", "navwarning", "vessel_incident"}
_STRIKE_ANOM = {"strike_warning", "conflict_event", "explosion", "fire",
                "not_under_command", "aground", "missile_test"}


def _rule_maritime_strike(new: FusionSignal, event: IntelEvent) -> Optional[FusedAlert]:
    """A vessel incident / conflict event / strike warning corroborated by two
    other grey-zone signals nearby in space and time = a probable strike on
    shipping (Black Sea, Red Sea, E. Med)."""
    if new.kind not in _STRIKE_KINDS:
        return None
    if new.kind == "vessel_incident" and new.anomaly_type not in _STRIKE_ANOM:
        return None
    if new.lat is None or new.lon is None:
        return None
    window_s = 6 * 3600
    corrob: list[FusionSignal] = []
    kinds_seen: set[str] = set()
    for other in _recent_signals(new.event_id):
        if other.lat is None or other.kind == new.kind:
            continue
        if abs(other.ts - new.ts) > window_s:
            continue
        if haversine_km(new.lat, new.lon, other.lat, other.lon) > 120:
            continue
        if other.kind in ("conflict_event", "navwarning", "vessel_incident") \
                or other.anomaly_type in ("seismic", "explosion") \
                or (other.kind == "ais_anomaly" and other.anomaly_type in ("gap", "long_gap")):
            if other.kind not in kinds_seen:
                corrob.append(other)
                kinds_seen.add(other.kind)
    if len(corrob) < 2:
        return None
    ids = [new.event_id, *[c.event_id for c in corrob]]
    return FusedAlert(
        alert_type="maritime_strike",
        domain="grey_zone",
        severity="critical",
        confidence=round(min(0.95, 0.55 + 0.15 * len(corrob)), 3),
        lat=new.lat, lon=new.lon, ts=new.ts,
        contributing_event_ids=ids,
        contributing_sources=sorted({new.source, *[c.source for c in corrob]}),
        summary=(f"Probable strike on shipping — {event.title[:120]} "
                 f"(+{len(corrob)} corroborating signals)"),
        open_case=True,
        case_type="vessel_incident",
        vessel_mmsi=new.mmsi,
    )


_RULES: list[Callable[[FusionSignal, IntelEvent], Optional[FusedAlert]]] = [
    _rule_sar_multisource,
    _rule_spoofing,
    _rule_dark_sts,
    _rule_identity_fraud,
    _rule_maritime_strike,
    _rule_grey_zone,
    _rule_single_source,
]


# ── emission ──────────────────────────────────────────────────────────────────

_EMIT_LOCK = threading.Lock()


def _already_alerted(cluster_id: str) -> bool:
    for existing in intel_store.events(limit=300):
        if existing.type == ALERT_TYPE and existing.metadata.get("cluster_id") == cluster_id:
            return True
    return False


def _emit(alert: FusedAlert) -> None:
    with _EMIT_LOCK:
        _emit_locked(alert)


def _alert_id(cluster_id: str) -> str:
    """Deterministic, DB-column-length id from a cluster_id.

    A correlated_alert built with the default random id (as before this
    fix) relied solely on the bounded in-memory dedup checks below --
    _seen caps at DEDUP_WINDOW entries and _already_alerted only scans the
    last 300 events, both far smaller than the event volume a busy MDA
    scan cycle produces. Once either window rolled past a cluster's last
    alert, the *same* rendezvous/proximity/sanctions cluster was treated
    as new and re-inserted as a fresh row -- e.g. one recurring STS pair
    alone produced 94k+ correlated_alert rows in two days in production.
    A deterministic id makes core.intel.store._persist_sync's existing
    "deterministic IDs are updateable machine episodes" upsert collapse
    every re-emission of the same cluster onto one DB row, independent of
    event volume or process uptime.
    """
    return "fus:" + hashlib.sha256(cluster_id.encode()).hexdigest()[:12]


def _emit_locked(alert: FusedAlert) -> None:
    if _already_alerted(alert.cluster_id):
        logger.debug("fusion: cluster %s already alerted, skipping", alert.cluster_id)
        return

    verification_status, independence_groups, evidence_count = verification_for_event_ids(
        alert.contributing_event_ids
    )
    metadata = {
        "alert_type": alert.alert_type,
        "maritime_domain": alert.domain,
        "confidence": alert.confidence,
        "contributing": alert.contributing_event_ids,
        "contributing_sources": alert.contributing_sources,
        "contributing_independence_groups": independence_groups,
        "independent_source_count": len(independence_groups),
        "evidence_count": evidence_count,
        "verification_explanation": verification_explanation(
            verification_status, independence_groups, evidence_count
        ),
        "cluster_id": alert.cluster_id,
        "is_distress": alert.domain == "sar",
        "verification_status": verification_status,
        "coordinate_source": "post_text",
    }
    if (
        alert.domain in {"grey_zone", "sanctions"}
        and verification_status != "multi_source_corroborated"
    ):
        # A single source can establish an observation or an official-list
        # fact, but a fused security alert is a derived interpretation. Keep
        # it internal until independent corroboration/review clears the
        # publication boundary. Official sanctions facts have their own
        # source-cited projection and must not borrow this fused-alert path.
        metadata["publication_status"] = "internal"
    if alert.vessel_mmsi:
        metadata["mmsi"] = alert.vessel_mmsi
    alert_event = IntelEvent(
        id=_alert_id(alert.cluster_id),
        type=ALERT_TYPE,
        severity=alert.severity,
        lat=alert.lat,
        lon=alert.lon,
        title=alert.summary[:255],
        text=alert.summary[:600],
        source="SeaCommons fusion",
        linked_mmsi=alert.vessel_mmsi,
        metadata=metadata,
    )
    added = intel_store.add(alert_event, dedup_key=f"fusion:{alert.cluster_id}")
    if not added:
        return
    logger.warning(
        "FUSION ALERT %s [%s] confidence=%.2f sources=%s",
        alert.alert_type, alert.domain, alert.confidence, alert.contributing_sources,
    )

    case_id = None
    if alert.open_case:
        case_id = _open_case_for_alert(alert, alert_event.id)
        if case_id:
            intel_store.update_metadata(alert_event.id, metadata={"case_id": case_id})

    payload = {
        "id": alert_event.id, "alert_type": alert.alert_type, "domain": alert.domain,
        "confidence": alert.confidence, "lat": alert.lat, "lon": alert.lon,
        "contributing_sources": alert.contributing_sources, "cluster_id": alert.cluster_id,
        "case_id": case_id,
    }
    try:
        from core.notifications import notify_alert

        notify_alert(payload)
    except Exception as exc:  # pragma: no cover
        logger.warning("fusion: notify_alert failed: %s", exc)


def _find_relinkable_case(db, alert: FusedAlert, case_type: str) -> Optional[str]:
    """An already-OPEN case that this alert's incident is a later update to.

    ``_open_case_for_alert``'s primary lookup only matches events belonging
    to *this* cluster (``alert.contributing_event_ids``), so a follow-up
    signal about the same vessel or incident days later -- a fresh
    cluster_id, no overlapping event ids -- always missed it and forked a
    duplicate case. Match by MMSI when the alert has one (dark-fleet /
    sanctions / mobility domains); otherwise by proximity, since most
    humanitarian SAR reports (a dinghy, no AIS) carry no MMSI.
    """
    from core.cases.service import OPEN_STATUSES
    from core.db.models import CaseDB, CaseIntelEventDB, IntelEventDB

    window_start = datetime.fromtimestamp(alert.ts, tz=timezone.utc) - timedelta(
        days=config.FUSION_CASE_RELINK_WINDOW_DAYS
    )
    query = (
        db.query(CaseDB)
        .filter(CaseDB.case_type == case_type)
        .filter(CaseDB.status.in_(OPEN_STATUSES))
        .filter(CaseDB.created_at >= window_start.replace(tzinfo=None))
    )

    if alert.vessel_mmsi:
        match = (
            query.join(CaseIntelEventDB, CaseIntelEventDB.case_id == CaseDB.case_id)
            .join(IntelEventDB, IntelEventDB.id == CaseIntelEventDB.event_id)
            .filter(IntelEventDB.linked_mmsi == alert.vessel_mmsi)
            .order_by(CaseDB.created_at.desc())
            .first()
        )
        return match.case_id if match else None

    if alert.lat is None or alert.lon is None:
        return None
    radius_km = config.FUSION_CASE_RELINK_RADIUS_KM
    best: Optional[CaseDB] = None
    best_km = radius_km
    for case in query.filter(CaseDB.lat.isnot(None), CaseDB.lon.isnot(None)):
        dist = haversine_km(alert.lat, alert.lon, case.lat, case.lon)
        if dist <= best_km:
            best, best_km = case, dist
    return best.case_id if best else None


def _open_case_for_alert(alert: FusedAlert, alert_event_id: str) -> Optional[str]:
    try:
        from core.cases.service import CASE_TYPES, OPEN_STATUSES, link_intel_events, open_case
        from core.db.models import CaseDB, CaseIntelEventDB
        from core.db.session import session_scope

        linked_ids = list(dict.fromkeys([alert_event_id, *alert.contributing_event_ids]))
        case_type = (alert.case_type if alert.case_type in CASE_TYPES
                     and alert.case_type != "unspecified"
                     else _DOMAIN_CASE_TYPE.get(alert.domain, alert.case_type))
        with session_scope() as db:
            existing = (
                db.query(CaseIntelEventDB.case_id)
                .join(CaseDB, CaseDB.case_id == CaseIntelEventDB.case_id)
                .filter(CaseIntelEventDB.event_id.in_(linked_ids))
                .filter(CaseDB.status.in_(OPEN_STATUSES))
                .first()
            )
            if existing is not None:
                logger.info("fusion: open case %s already covers cluster %s", existing[0], alert.cluster_id)
                return existing[0]

            relink_case_id = _find_relinkable_case(db, alert, case_type)
            if relink_case_id is not None:
                link_intel_events(
                    db,
                    case_id=relink_case_id,
                    intel_event_ids=linked_ids,
                    linked_by="fusion-engine",
                    timeline_note=f"Re-linked: new {alert.alert_type} signal matches this case ({alert.summary})",
                    audit_action="case.auto_relinked",
                    audit_data={"alert_type": alert.alert_type, "cluster_id": alert.cluster_id},
                )
                logger.info(
                    "fusion: relinked cluster %s onto existing case %s (no new case opened)",
                    alert.cluster_id, relink_case_id,
                )
                return relink_case_id

            priority = "critical" if alert.confidence >= 0.8 else "high"
            case = open_case(
                db,
                title=f"[{alert.domain}] {alert.alert_type}: {alert.summary}"[:256],
                created_by="fusion-engine",
                case_type=case_type,
                priority=priority,
                sensitivity="restricted",
                summary=alert.summary,
                lat=alert.lat,
                lon=alert.lon,
                intel_event_ids=linked_ids,
                timeline_note=f"Auto-opened by fusion engine ({alert.alert_type})",
                audit_action="case.auto_created",
                audit_data={"alert_type": alert.alert_type, "cluster_id": alert.cluster_id},
                notify=False,  # _emit() sends a richer, cooldown-guarded notify_alert
            )
            return case["case_id"]
    except Exception as exc:
        logger.warning("fusion: auto-case failed: %s", exc)
        return None


# ── entry point ───────────────────────────────────────────────────────────────

def evaluate(event: IntelEvent) -> None:
    """intel_store subscriber — safe to call for every event."""
    if not getattr(config, "FUSION_ENABLED", True):
        return
    signal = normalize(event)
    if signal is None:
        return
    if signal.kind == "ais_anomaly" and _CORRELATION_ENGINE is not None:
        try:
            _CORRELATION_ENGINE.ingest("ais:anomalies", {
                "anomaly_type": signal.anomaly_type,
                "mmsi": signal.mmsi,
                "position": {"lat": signal.lat, "lon": signal.lon, "alt": 0, "source": "ais"},
                "confidence": 0.6,
            })
        except Exception as exc:  # pragma: no cover
            logger.debug("fusion: correlation-engine ingest skipped: %s", exc)
    # Rules are ordered most-specific first; the first that fires for an event
    # wins (a `maritime_strike` should not also raise a generic `vessel_casualty`
    # for the same hull). A rule can still fire later for a *different* event of
    # the same incident.
    for rule in _RULES:
        try:
            alert = rule(signal, event)
        except Exception as exc:  # pragma: no cover - one rule must not break others
            logger.warning("fusion rule %s failed: %s", rule.__name__, exc)
            continue
        if alert is not None:
            _emit(alert)
            break


_REGISTERED = False
_CORRELATION_ENGINE = None


def set_correlation_engine(engine) -> None:
    """Give the fusion engine a handle to the physical-sensor CorrelationEngine
    so AIS anomalies can also feed its channel map (see core.anomaly.correlation).
    """
    global _CORRELATION_ENGINE
    _CORRELATION_ENGINE = engine


def emit_physical_threat(threat: dict) -> None:
    """on_threat callback for CorrelationEngine — surface a sensor-fusion threat
    as a correlated_alert IntelEvent so it reaches the map / feed / notifications.
    """
    classification = str(threat.get("classification") or "physical_threat_candidate")
    domain = "sanctions" if classification == "vessel_spoofing_confirmed" else "safety"
    sensor_data = threat.get("sensor_data") or {}
    position = (sensor_data.get("ais_anomaly") or {}).get("position") or {}
    lat, lon = position.get("lat"), position.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
        logger.info("fusion: physical threat %s has no position, not mapped", classification)
        return
    ts = datetime.now(timezone.utc).timestamp()
    alert = FusedAlert(
        alert_type=classification,
        domain=domain,
        severity="critical" if threat.get("urgent") else "high",
        confidence=round(float(threat.get("confidence") or 0.0), 3),
        lat=float(lat), lon=float(lon), ts=ts,
        contributing_event_ids=[],
        contributing_sources=list(threat.get("sources") or []),
        summary=f"Sensor fusion: {classification} ({', '.join(threat.get('sources') or [])})",
        open_case=threat.get("urgent", False) or classification == "vessel_spoofing_confirmed",
        case_type=_DOMAIN_CASE_TYPE.get(domain, "unspecified"),
    )
    _emit(alert)


def register() -> None:
    """Wire the engine into the intel store. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    intel_store.subscribe(evaluate)
    _REGISTERED = True
    logger.info("OSINT fusion engine registered (%d rules)", len(_RULES))
