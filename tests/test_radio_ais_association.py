from datetime import datetime, timezone


def _message(**overrides):
    from core.radio.provider import DecodedRadioMessage

    values = {
        "kind": "dsc",
        "receiver_id": "rx",
        "provider": "kiwisdr",
        "physical_lineage": "lineage",
        "frequency_hz": 2_187_500,
        "mode": "usb",
        "observed_at": datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
        "payload": {
            "category": "distress",
            "mmsi": "123456789",
            "latitude": 35.9,
            "longitude": 14.5,
        },
        "provider_message_id": "dsc-1",
        "source_terms": "allowed",
    }
    values.update(overrides)
    return DecodedRadioMessage(**values)


def test_exact_mmsi_close_recent_ais_is_strong_cross_modal_match(monkeypatch):
    from core.radio.ais_association import associate_dsc_with_ais

    msg = _message()
    monkeypatch.setattr(
        "core.radio.ais_association._recent_ais_points",
        lambda mmsi, observed_at: [
            {"lat": 35.91, "lon": 14.51, "ts": observed_at.isoformat(), "sog": 2.1}
        ],
    )
    result = associate_dsc_with_ais(msg, observation_id="obs:dsc1")
    assert result.mmsi == "123456789"
    assert result.match_status == "strong"
    assert result.confidence >= 0.9
    assert result.distance_km is not None and result.distance_km < 5
    assert result.episode_eligible is True


def test_far_ais_position_is_not_episode_eligible(monkeypatch):
    from core.radio.ais_association import associate_dsc_with_ais

    msg = _message()
    monkeypatch.setattr(
        "core.radio.ais_association._recent_ais_points",
        lambda mmsi, observed_at: [
            {"lat": 45.0, "lon": 9.0, "ts": observed_at.isoformat(), "sog": 8.0}
        ],
    )
    result = associate_dsc_with_ais(msg, observation_id="obs:dsc2")
    assert result.match_status in {"weak", "conflict"}
    assert result.episode_eligible is False


def test_mmsi_without_radio_position_stays_identity_only(monkeypatch):
    from core.radio.ais_association import associate_dsc_with_ais

    msg = _message(payload={"category": "safety", "mmsi": "123456789"})
    monkeypatch.setattr(
        "core.radio.ais_association._recent_ais_points",
        lambda mmsi, observed_at: [
            {"lat": 35.9, "lon": 14.5, "ts": observed_at.isoformat(), "sog": 1.0}
        ],
    )
    result = associate_dsc_with_ais(msg, observation_id="obs:dsc3")
    assert result.match_status == "identity_only"
    assert result.episode_eligible is False


def test_association_persistence_is_idempotent():
    from core.db.models import RadioAISAssociationDB
    from core.db.session import engine, session_scope
    from core.radio.ais_association import (
        RadioAISAssociation,
        persist_association,
    )
    RadioAISAssociationDB.__table__.create(bind=engine(), checkfirst=True)
    assoc = RadioAISAssociation(
        "obs:persist",
        "123456789",
        "strong",
        0.95,
        1.2,
        "2026-09-07T15:00:00+00:00",
        True,
    )
    with session_scope() as db:
        persist_association(db, assoc)
        persist_association(db, assoc)
    with session_scope() as db:
        rows = db.query(RadioAISAssociationDB).filter_by(observation_id="obs:persist").all()
        assert len(rows) == 1 and rows[0].mmsi == "123456789"
        db.query(RadioAISAssociationDB).filter_by(observation_id="obs:persist").delete()


def test_decoded_dsc_bridge_attaches_ais_association(monkeypatch):
    from core.radio import bridge
    from core.radio.ais_association import RadioAISAssociation

    class Runtime:
        def ingest_dsc(self, payload, **context):
            return {"accepted": True, "projected": False, "observation_id": "obs:bridge"}

    monkeypatch.setattr(bridge, "get_structured_radio_runtime", lambda: Runtime())
    monkeypatch.setattr(
        bridge,
        "associate_and_persist_dsc",
        lambda msg, observation_id: RadioAISAssociation(
            observation_id,
            "123456789",
            "strong",
            0.95,
            1.0,
            msg.observed_at.isoformat(),
            True,
            "episode:canonical:radio-test",
        ),
    )
    result = bridge.handle_decoded_radio_message(_message())
    assert result["ais_association"]["mmsi"] == "123456789"
    assert result["ais_association"]["match_status"] == "strong"
    assert result["ais_association"]["case_associated"] is True
    assert result["maritime_episode_id"] == "episode:canonical:radio-test"


def test_strong_radio_match_reuses_existing_episode_and_never_mints_parallel_case():
    from core.db.models import MaritimeEpisodeDB
    from core.db.session import session_scope
    from core.intel.episode_store import save_episode
    from core.radio.ais_association import (
        RadioAISAssociation,
        persist_strong_radio_ais_episode,
    )

    episode_id = "episode:test:radio-existing"
    save_episode({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [14.5, 35.9]},
        "properties": {
            "episode_id": episode_id,
            "episode_family": "gap_episode",
            "subject_ids": ["subj:mmsi:123456789"],
            "related_signal_ids": ["radio-parent-signal"],
            "timestamp_utc": "2026-09-07T15:00:00+00:00",
            "verification_status": "single_source_observed",
        },
    })
    strong = RadioAISAssociation(
        "obs:episode", "123456789", "strong", 0.95, 2.3,
        "2026-09-07T15:00:00+00:00", True, episode_id,
    )
    weak = RadioAISAssociation(
        "obs:weak", "123456789", "weak", 0.65, 55.0,
        "2026-09-07T15:00:00+00:00", False,
    )
    try:
        resolved = persist_strong_radio_ais_episode(_message(), strong)
        assert resolved is not None
        assert resolved.episode_id == episode_id
        assert persist_strong_radio_ais_episode(_message(), weak) is None
        with session_scope() as db:
            assert db.query(MaritimeEpisodeDB).filter(
                MaritimeEpisodeDB.episode_family == "radio_dsc_ais"
            ).count() == 0
    finally:
        with session_scope() as db:
            db.query(MaritimeEpisodeDB).filter_by(episode_id=episode_id).delete()
