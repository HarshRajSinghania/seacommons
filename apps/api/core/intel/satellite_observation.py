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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def persist_observations(observations: list[SatelliteObservation]) -> int:
    """Persist metadata idempotently by deterministic observation_id."""
    from core.db.models import SatelliteObservationDB
    from core.db.session import session_scope
    from core.intel.lifecycle import parse_utc

    created = 0
    with session_scope() as db:
        for observation in observations:
            if db.get(SatelliteObservationDB, observation.observation_id) is not None:
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
        ) for row in rows]


def materialize_unmatched_sar_detections(
    *, incident_id: str, cue: dict[str, Any],
) -> list[SatelliteObservation]:
    """Turn core.mda.darkship_cue's gfw_unmatched_in_area detections into
    proper, persisted satellite evidence -- not decorative images.

    Each unmatched detection is a candidate, never a vessel match: stored
    with association_status="unmatched_candidate", the exact vocabulary
    darkship_cue.build() already uses for its own top-level field.
    """
    import hashlib

    unmatched = [d for d in (cue.get("gfw_unmatched_in_area") or ()) if isinstance(d, dict)]
    observations: list[SatelliteObservation] = []
    now = datetime.now(timezone.utc).isoformat()
    for detection in unmatched:
        lat = detection.get("lat")
        lon = detection.get("lon")
        if lat is None or lon is None:
            continue
        timestamp = str(detection.get("timestamp") or now)
        digest = hashlib.blake2s(
            f"{incident_id}:{timestamp}:{lat}:{lon}".encode(), digest_size=16,
        ).hexdigest()
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
            provenance={"source": "gfw_sar_unmatched", "darkship_cue": True},
            evidence_status="contextual",
            association_status="unmatched_candidate",
        ))
    return observations
