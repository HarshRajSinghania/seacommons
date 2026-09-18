# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistence boundary for derived MaritimeEpisode records."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

_METHOD_VERSION = "maritime-episode-v1"


def episode_fingerprint(
    *,
    subject_ids: tuple[str, ...],
    family: str,
    signal_ids: tuple[str, ...],
    first_observed_at: str,
    last_observed_at: str,
    method_version: str = _METHOD_VERSION,
) -> str:
    payload = {
        "subject_ids": sorted(str(v) for v in subject_ids),
        "family": str(family),
        "signal_ids": sorted(str(v) for v in signal_ids),
        "first_observed_at": str(first_observed_at),
        "last_observed_at": str(last_observed_at),
        "method_version": str(method_version),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def save_episode(feature: dict[str, Any]):
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope

    props = feature.get("properties") or {}
    episode_id = str(props.get("episode_id") or "")
    family = str(props.get("episode_family") or "")
    subject_ids = tuple(str(v) for v in (props.get("subject_ids") or ()) if v)
    signal_ids = tuple(str(v) for v in (props.get("observation_ids") or props.get("related_signal_ids") or ()) if v)
    first_at = str(props.get("first_observed_at") or props.get("timestamp_utc") or "")
    last_at = str(props.get("last_observed_at") or props.get("timestamp_utc") or "")
    if not episode_id or not family or not subject_ids or not first_at or not last_at:
        raise ValueError("episode feature missing deterministic identity fields")

    method_version = str(props.get("episode_method_version") or _METHOD_VERSION)
    fingerprint = episode_fingerprint(
        subject_ids=subject_ids,
        family=family,
        signal_ids=signal_ids,
        first_observed_at=first_at,
        last_observed_at=last_at,
        method_version=method_version,
    )
    behaviour_context = dict(props.get("behaviour_context") or {})
    behaviour_context["analysis"] = {
        "analysis_state": str(props.get("analysis_state") or "observation"),
        "publication_state": str(props.get("publication_state") or "internal"),
        "resolution_state": str(props.get("resolution_state") or "open"),
        "lineage_ids": list(props.get("lineage_ids") or props.get("independence_groups") or ()),
    }
    values = {
        "episode_family": family,
        "subject_ids": list(subject_ids),
        "start_at": _parse_datetime(first_at),
        "end_at": _parse_datetime(last_at),
        "geometry": feature.get("geometry"),
        "observation_ids": list(signal_ids),
        "feature_ids": list(props.get("feature_ids") or ()),
        "independence_groups": list(props.get("independence_groups") or ()),
        "verification_status": str(props.get("verification_status") or "single_source_observed"),
        "behaviour_context": behaviour_context,
        "alternative_explanations": list(props.get("alternative_explanations") or ()),
        "evidence_fingerprint": fingerprint,
        "method_version": method_version,
        "status": str(props.get("episode_status") or "active"),
    }
    with session_scope() as db:
        row = db.query(MaritimeEpisodeDB).filter(MaritimeEpisodeDB.episode_id == episode_id).first()
        if row is None:
            row = MaritimeEpisodeDB(episode_id=episode_id, **values)
            db.add(row)
        else:
            if row.episode_family != family or tuple(row.subject_ids or ()) != subject_ids:
                raise ValueError("episode identity collision")
            for key, value in values.items():
                setattr(row, key, value)
        db.flush()
        db.refresh(row)
        db.expunge(row)
        return row
