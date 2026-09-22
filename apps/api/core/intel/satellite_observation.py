"""Provider-neutral satellite evidence contract for Live/Play."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

TemporalRelation = Literal["reverse", "nearest", "forward"]


@dataclass(frozen=True)
class SatelliteObservation:
    observation_id: str
    incident_id: str
    provider: str
    mission: str
    product_id: str
    acquisition_time: str
    discovered_at: str
    footprint: dict[str, Any] | None
    bbox: list[float] | None
    sensor_type: str
    temporal_relation: TemporalRelation
    temporal_delta_s: float
    asset_ref: str
    source_url: str
    provenance: dict[str, Any]
    resolution_m: float | None = None
    cloud_cover: float | None = None
    polarisation: list[str] | None = None
    evidence_status: str = "contextual"
    # core.mda.darkship_cue's own vocabulary: "unmatched_candidate" for a SAR
    # detection inside a reachable-area calculation that hasn't been matched
    # to a vessel -- a candidate, never a confirmed identity. None means the
    # association question doesn't apply to this observation.
    association_status: str | None = None
    # Non-null only when this observation is explicitly associated with the
    # canonical MaritimeEpisode rather than merely sharing case context.
    episode_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merged_provenance(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(existing or {})
    incoming = dict(incoming or {})
    for key, value in incoming.items():
        if key != "case_targets":
            merged[key] = value
    targets = []
    seen = set()
    for row in [*(merged.get("case_targets") or []), *(incoming.get("case_targets") or [])]:
        if not isinstance(row, dict):
            continue
        key = (
            row.get("role"), row.get("lat"), row.get("lon"), row.get("at"),
            row.get("search_direction"), row.get("drift_id"), row.get("trajectory_index"),
        )
        if key in seen:
            continue
        seen.add(key)
        targets.append(dict(row))
    if targets:
        merged["case_targets"] = targets
    return merged


def persist_observations(observations: list[SatelliteObservation]) -> int:
    """Persist scene metadata idempotently while merging case-target lineage."""
    from core.db.models import SatelliteObservationDB
    from core.db.session import session_scope
    from core.intel.lifecycle import parse_utc

    created = 0
    with session_scope() as db:
        for observation in observations:
            existing = db.get(SatelliteObservationDB, observation.observation_id)
            if existing is not None:
                existing.provenance = _merged_provenance(existing.provenance, observation.provenance)
                # Association is monotonic: a later resolver may promote a
                # contextual scene/detection, but a replay must not erase it.
                if observation.episode_id:
                    existing.episode_id = observation.episode_id
                if observation.association_status:
                    existing.association_status = observation.association_status
                if observation.evidence_status != "contextual":
                    existing.evidence_status = observation.evidence_status
                continue
            discovered = parse_utc(observation.discovered_at)
            db.add(SatelliteObservationDB(
                observation_id=observation.observation_id,
                incident_id=observation.incident_id,
                provider=observation.provider,
                mission=observation.mission,
                product_id=observation.product_id,
                acquisition_time=observation.acquisition_time,
                discovered_at=(discovered or datetime.now(timezone.utc)).replace(tzinfo=None),
                footprint=observation.footprint,
                bbox=observation.bbox,
                sensor_type=observation.sensor_type,
                temporal_relation=observation.temporal_relation,
                temporal_delta_s=observation.temporal_delta_s,
                asset_ref=observation.asset_ref,
                source_url=observation.source_url,
                provenance=observation.provenance,
                resolution_m=observation.resolution_m,
                cloud_cover=observation.cloud_cover,
                polarisation=observation.polarisation,
                evidence_status=observation.evidence_status,
                association_status=observation.association_status,
                episode_id=observation.episode_id,
            ))
            created += 1
    return created


def list_incident_observations(incident_id: str) -> list[SatelliteObservation]:
    from core.db.models import SatelliteObservationDB
    from core.db.session import session_scope

    with session_scope() as db:
        rows = (
            db.query(SatelliteObservationDB)
            .filter(SatelliteObservationDB.incident_id == incident_id)
            .order_by(SatelliteObservationDB.acquisition_time.asc())
            .all()
        )
        return [SatelliteObservation(
            observation_id=row.observation_id,
            incident_id=row.incident_id,
            provider=row.provider,
            mission=row.mission,
            product_id=row.product_id,
            acquisition_time=row.acquisition_time,
            discovered_at=row.discovered_at.replace(tzinfo=timezone.utc).isoformat()
            if row.discovered_at else "",
            footprint=row.footprint,
            bbox=list(row.bbox) if row.bbox else None,
            sensor_type=row.sensor_type,
            temporal_relation=row.temporal_relation,
            temporal_delta_s=float(row.temporal_delta_s or 0),
            asset_ref=row.asset_ref or "",
            source_url=row.source_url or "",
            provenance=dict(row.provenance or {}),
            resolution_m=row.resolution_m,
            cloud_cover=row.cloud_cover,
            polarisation=list(row.polarisation) if row.polarisation else None,
            evidence_status=row.evidence_status or "contextual",
            association_status=row.association_status,
            episode_id=row.episode_id,
        ) for row in rows]


def _valid_mmsi(value: Any) -> str:
    text = str(value or "").strip()
    return text if len(text) == 9 and text.isdigit() else ""


def materialize_sar_detections(
    *,
    incident_id: str,
    cue: dict[str, Any],
    expected_mmsi: str | None = None,
    episode_id: str | None = None,
) -> list[SatelliteObservation]:
    """Materialize GFW SAR detections with conservative association semantics.

    Exact provider MMSI equality may attach a detection to the current
    canonical episode. An unmatched target, a conflicting MMSI, or a provider
    identity that cannot be compared stays contextual.
    """
    import hashlib

    raw = cue.get("gfw_sar_detections")
    if not isinstance(raw, list):
        raw = cue.get("gfw_unmatched_in_area") or []
    detections = [d for d in raw if isinstance(d, dict)]
    expected = _valid_mmsi(expected_mmsi)
    observations: list[SatelliteObservation] = []
    now = datetime.now(timezone.utc).isoformat()
    for detection in detections:
        lat, lon = detection.get("lat"), detection.get("lon")
        if lat is None or lon is None:
            continue
        timestamp = str(detection.get("timestamp") or now)
        digest = hashlib.blake2s(
            f"{incident_id}:{timestamp}:{lat}:{lon}".encode(), digest_size=16,
        ).hexdigest()
        detected = _valid_mmsi(detection.get("mmsi"))
        provider_matched = bool(detection.get("matched"))
        if provider_matched and expected and detected == expected and episode_id:
            association_status = "strong"
            linked_episode = episode_id
            evidence_status = "associated"
            association_method = "provider_exact_mmsi"
        elif provider_matched and detected and expected and detected != expected:
            association_status = "conflict"
            linked_episode = None
            evidence_status = "contextual"
            association_method = "provider_mmsi_conflict"
        elif provider_matched:
            association_status = "provider_matched_unresolved"
            linked_episode = None
            evidence_status = "contextual"
            association_method = "provider_identity_unresolved"
        else:
            association_status = "unmatched_candidate"
            linked_episode = None
            evidence_status = "contextual"
            association_method = "none"

        observations.append(SatelliteObservation(
            observation_id=f"sat:gfw_sar:{digest}",
            incident_id=incident_id,
            provider="gfw",
            mission="sentinel1",
            product_id=f"gfw_sar:{digest}",
            acquisition_time=timestamp,
            discovered_at=now,
            footprint={"type": "Point", "coordinates": [float(lon), float(lat)]},
            bbox=None,
            sensor_type="sar",
            temporal_relation="nearest",
            temporal_delta_s=0.0,
            asset_ref="",
            source_url="",
            provenance={
                "source": "gfw_sar_detection",
                "darkship_cue": True,
                "provider_matched": provider_matched,
                "association_method": association_method,
            },
            evidence_status=evidence_status,
            association_status=association_status,
            episode_id=linked_episode,
        ))
    return observations


def materialize_unmatched_sar_detections(
    *, incident_id: str, cue: dict[str, Any],
) -> list[SatelliteObservation]:
    """Backward-compatible contextual-only materialization."""
    contextual_cue = dict(cue)
    contextual_cue["gfw_sar_detections"] = [
        d for d in (cue.get("gfw_unmatched_in_area") or ()) if isinstance(d, dict)
    ]
    return materialize_sar_detections(incident_id=incident_id, cue=contextual_cue)
