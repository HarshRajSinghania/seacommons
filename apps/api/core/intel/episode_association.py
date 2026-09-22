# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conservative evidence -> canonical MaritimeEpisode association."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _distance_to_interval_s(at: datetime, start: datetime, end: datetime) -> float:
    if start <= at <= end:
        return 0.0
    return min(abs((at - start).total_seconds()), abs((at - end).total_seconds()))


def unique_episode_for_mmsi(
    mmsi: str,
    observed_at: datetime,
    *,
    window_hours: float = 6.0,
) -> str | None:
    """Return one unambiguous canonical episode for this vessel/time.

    Vessel identity alone is not enough to select among overlapping behaviour
    episodes. If more than one episode is temporally plausible, fail closed.
    """
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.mda.vessel_subject import subject_id_for

    subject_id = subject_id_for(mmsi=mmsi)
    if not subject_id:
        return None
    at = _utc_naive(observed_at)
    margin = timedelta(hours=max(0.0, float(window_hours)))
    with session_scope() as db:
        rows = (
            db.query(MaritimeEpisodeDB)
            .filter(
                MaritimeEpisodeDB.start_at <= at + margin,
                MaritimeEpisodeDB.end_at >= at - margin,
            )
            .order_by(MaritimeEpisodeDB.updated_at.desc())
            .limit(100)
            .all()
        )
        candidates = [
            (
                str(row.episode_id),
                _utc_naive(row.start_at),
                _utc_naive(row.end_at),
            )
            for row in rows
            if subject_id in {str(value) for value in (row.subject_ids or [])}
        ]

    if len(candidates) != 1:
        return None
    episode_id, start, end = candidates[0]
    if _distance_to_interval_s(at, start, end) > margin.total_seconds():
        return None
    return episode_id
