# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json

from core.intel.source_observation import SourceObservation, record_observation
from core.radio.structured import DSCObservation, NAVTEXObservation

_SOURCE_POLICY = "structured_remote_radio_decoder"
_SERVICE = "maritime"
_SAFETY_LANE = "safety"


def _source_name(physical_lineage: str) -> str:
    return f"radio_receiver:{physical_lineage}"


def _dsc_payload(observation: DSCObservation) -> dict[str, object]:
    return {
        "category": observation.category,
        "mmsi": observation.mmsi,
        "latitude": observation.latitude,
        "longitude": observation.longitude,
        "nature_code": observation.nature_code,
        "field_presence": list(observation.field_presence),
        "format": observation.format,
        "from_mmsi": observation.from_mmsi,
        "to_mmsi": observation.to_mmsi,
        "nature_description": observation.nature_description,
        "telecommand_1": observation.telecommand_1,
        "telecommand_2": observation.telecommand_2,
        "end_of_sequence": observation.end_of_sequence,
        "distress_time": observation.distress_time,
        "reported_frequency": observation.reported_frequency,
    }


def _navtex_payload(observation: NAVTEXObservation) -> dict[str, object]:
    return {
        "station_id": observation.station_id,
        "subject_id": observation.subject_id,
        "message_id": observation.message_id,
        "area": observation.area,
        "text": observation.text,
    }


def _persist(
    db,
    *,
    observation_type: str,
    physical_lineage: str,
    receiver_id: str,
    observed_at: str,
    decoder_message_id: str,
    raw_evidence_ref: str,
    source_terms: str | None,
    frequency_hz: int,
    payload: dict[str, object],
    lat: float | None = None,
    lon: float | None = None,
) -> SourceObservation:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return record_observation(
        db,
        service=_SERVICE,
        lane=_SAFETY_LANE,
        observation_type=observation_type,
        source_name=_source_name(physical_lineage),
        source_policy=_SOURCE_POLICY,
        source_id=f"{observation_type}:{decoder_message_id}",
        observed_at=observed_at,
        raw_payload=encoded,
        raw_payload_ref=raw_evidence_ref,
        lat=lat,
        lon=lon,
        subject_refs=[],
        provenance={
            "receiver_id": receiver_id,
            "physical_lineage": physical_lineage,
            "source_terms": source_terms or "",
            "frequency_hz": frequency_hz,
            "decoder_message_id": decoder_message_id,
            "structured_payload": encoded,
        },
    )


def persist_dsc_observation(db, observation: DSCObservation) -> SourceObservation:
    """Persist one decoded DSC message as immutable Maritime Safety evidence."""
    return _persist(
        db,
        observation_type="dsc_message",
        physical_lineage=observation.physical_lineage,
        receiver_id=observation.receiver_id,
        observed_at=observation.observed_at.isoformat(),
        decoder_message_id=observation.decoder_message_id,
        raw_evidence_ref=observation.raw_evidence_ref,
        source_terms=observation.source_terms,
        frequency_hz=observation.frequency_hz,
        payload=_dsc_payload(observation),
        lat=observation.latitude,
        lon=observation.longitude,
    )


def persist_navtex_observation(db, observation: NAVTEXObservation) -> SourceObservation:
    """Persist one decoded NAVTEX block as immutable Maritime contextual evidence."""
    return _persist(
        db,
        observation_type="navtex_message",
        physical_lineage=observation.physical_lineage,
        receiver_id=observation.receiver_id,
        observed_at=observation.observed_at.isoformat(),
        decoder_message_id=observation.decoder_message_id,
        raw_evidence_ref=observation.raw_evidence_ref,
        source_terms=observation.source_terms,
        frequency_hz=observation.frequency_hz,
        payload=_navtex_payload(observation),
    )
