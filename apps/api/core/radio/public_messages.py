# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from typing import Any, Mapping


def _structured_payload(row) -> dict[str, Any]:
    provenance = row.provenance if isinstance(row.provenance, Mapping) else {}
    encoded = provenance.get("structured_payload")
    if not isinstance(encoded, str):
        return {}
    try:
        parsed = json.loads(encoded)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def project_public_radio_message(row) -> dict[str, object]:
    payload = _structured_payload(row)
    provenance = row.provenance if isinstance(row.provenance, Mapping) else {}
    kind = "dsc" if row.observation_type == "dsc_message" else "navtex"
    base: dict[str, object] = {
        "observation_id": row.observation_id,
        "kind": kind,
        "observed_at": row.observed_at,
        "frequency_hz": int(provenance.get("frequency_hz") or 0),
        "latitude": row.lat,
        "longitude": row.lon,
    }
    if kind == "dsc":
        base.update({
            "category": payload.get("category"),
            "mmsi": payload.get("mmsi"),
            "from_mmsi": payload.get("from_mmsi"),
            "to_mmsi": payload.get("to_mmsi"),
            "nature_code": payload.get("nature_code"),
            "nature_description": payload.get("nature_description"),
            "format": payload.get("format"),
            "telecommand_1": payload.get("telecommand_1"),
            "telecommand_2": payload.get("telecommand_2"),
            "end_of_sequence": payload.get("end_of_sequence"),
            "distress_time": payload.get("distress_time"),
            "reported_frequency": payload.get("reported_frequency"),
        })
        return base

    text = str(payload.get("text") or "")[:1200]
    base.update({
        "station_id": payload.get("station_id"),
        "subject_id": payload.get("subject_id"),
        "message_id": payload.get("message_id"),
        "area": payload.get("area"),
        "text": text,
    })
    return base
