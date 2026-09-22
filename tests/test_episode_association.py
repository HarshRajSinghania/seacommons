from datetime import datetime, timezone

from core.intel.episode_store import save_episode


def _episode(episode_id: str, *, mmsi: str, at: str):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.1, 35.5]},
        "properties": {
            "episode_id": episode_id,
            "episode_family": "gap_episode",
            "subject_ids": [f"subj:mmsi:{mmsi}"],
            "related_signal_ids": [f"signal:{episode_id}"],
            "timestamp_utc": at,
            "verification_status": "single_source_observed",
        },
    }


def test_unique_episode_for_mmsi_resolves_single_parent():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.intel.episode_association import unique_episode_for_mmsi

    episode_id = "episode:test:association:single"
    at = "2026-09-22T09:00:00+00:00"
    save_episode(_episode(episode_id, mmsi="211879870", at=at))
    try:
        resolved = unique_episode_for_mmsi(
            "211879870",
            datetime(2026, 9, 22, 9, 30, tzinfo=timezone.utc),
        )
        assert resolved == episode_id
    finally:
        with session_scope() as db:
            db.query(MaritimeEpisodeDB).filter_by(episode_id=episode_id).delete()


def test_unique_episode_for_mmsi_fails_closed_on_ambiguity():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.intel.episode_association import unique_episode_for_mmsi

    ids = ["episode:test:association:a", "episode:test:association:b"]
    at = "2026-09-22T09:00:00+00:00"
    for episode_id in ids:
        save_episode(_episode(episode_id, mmsi="211879871", at=at))
    try:
        resolved = unique_episode_for_mmsi(
            "211879871",
            datetime(2026, 9, 22, 9, 30, tzinfo=timezone.utc),
        )
        assert resolved is None
    finally:
        with session_scope() as db:
            db.query(MaritimeEpisodeDB).filter(
                MaritimeEpisodeDB.episode_id.in_(ids)
            ).delete(synchronize_session=False)
