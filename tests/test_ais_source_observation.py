# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md M1.2: the live AIS feed adapter.

Sampling rule (explicit product decision): a SourceObservation is
recorded only on a navigational-status change or on reappearance after a
reporting gap -- never on every routine position fix. This exercises
AISSourceObservationSampler.on_position() directly (the same method
core.vessels.aisstream calls for every PositionReport via the shared
register_position_hook mechanism), not the live feed itself.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from core.db.models import SourceObservationDB
from core.db.session import session_scope
from core.vessels.ais_source_observation import AISSourceObservationSampler


@pytest.fixture
def sampler():
    return AISSourceObservationSampler()


def _pos(sampler, mmsi, lat=35.0, lon=18.0, nav=0, sog=10.0):
    sampler.on_position(
        mmsi, "TEST VESSEL", lat, lon, sog=sog, nav_status=nav, cog=90.0,
        heading=88.0, received_at=datetime.now(timezone.utc),
    )


def _observation_count(mmsi: str) -> int:
    with session_scope() as db:
        return (
            db.query(SourceObservationDB)
            .filter(SourceObservationDB.source_name == "AISStream")
            .filter(SourceObservationDB.source_id.like(f"{mmsi}:%"))
            .count()
        )


def test_first_position_for_a_vessel_records_an_observation(sampler):
    _pos(sampler, "111000111")
    assert _observation_count("111000111") == 1

    with session_scope() as db:
        row = (
            db.query(SourceObservationDB)
            .filter(SourceObservationDB.source_id.like("111000111:%"))
            .one()
        )
        assert row.service == "maritime"
        assert row.lane == "safety"
        assert row.observation_type == "ais_nav_status"
        assert row.provenance["reason"] == "first_seen"
        assert row.lat == 35.0 and row.lon == 18.0


def test_repeated_fixes_with_unchanged_status_record_nothing_more(sampler):
    _pos(sampler, "111000222", nav=0)
    _pos(sampler, "111000222", lat=35.001, lon=18.001, nav=0)
    _pos(sampler, "111000222", lat=35.002, lon=18.002, nav=0)
    assert _observation_count("111000222") == 1


def test_a_navigational_status_change_records_a_second_observation(sampler):
    _pos(sampler, "111000333", nav=0)
    _pos(sampler, "111000333", nav=5)  # 5 = moored
    assert _observation_count("111000333") == 2

    with session_scope() as db:
        rows = (
            db.query(SourceObservationDB)
            .filter(SourceObservationDB.source_id.like("111000333:%"))
            .order_by(SourceObservationDB.created_at)
            .all()
        )
        assert rows[1].observation_type == "ais_nav_status"
        assert rows[1].provenance["reason"] == "status_change"
        assert rows[1].provenance["nav_status"] == 5


def test_reappearance_after_a_gap_records_an_observation(sampler, monkeypatch):
    _pos(sampler, "111000444", nav=0)
    # Simulate silence exceeding the configured gap without sleeping the
    # test: back-date the sampler's own last-recorded timestamp.
    mmsi = "111000444"
    nav_status, _ts = sampler._last[mmsi]
    sampler._last[mmsi] = (nav_status, sampler._last[mmsi][1] - 3600)

    _pos(sampler, "111000444", nav=0)  # same status, but after the "gap"
    assert _observation_count("111000444") == 2

    with session_scope() as db:
        rows = (
            db.query(SourceObservationDB)
            .filter(SourceObservationDB.source_id.like("111000444:%"))
            .order_by(SourceObservationDB.created_at)
            .all()
        )
        assert rows[1].observation_type == "ais_gap"
        assert rows[1].provenance["reason"] == "gap_reappearance"


def test_a_fix_with_no_lat_lon_is_ignored(sampler):
    sampler.on_position("111000555", "TEST", None, None, nav_status=0)
    assert _observation_count("111000555") == 0


def test_a_broken_session_does_not_raise(sampler, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.db.session.session_scope", _boom)
    _pos(sampler, "111000666")  # must not raise


def test_bounded_memory_evicts_the_oldest_half_past_the_cap(sampler, monkeypatch):
    from core.vessels import ais_source_observation as mod

    monkeypatch.setattr(mod, "_MAX_TRACKED_MMSI", 4)
    for i in range(6):
        _pos(sampler, f"11100{i:04d}")
    assert len(sampler._last) <= 4

def test_continuous_routine_fixes_refresh_last_seen_without_creating_false_gap(
    sampler, monkeypatch
):
    from core.vessels import ais_source_observation as mod

    clock = iter([0.0, 1200.0, 2400.0])
    monkeypatch.setattr(mod.time, "time", lambda: next(clock))

    _pos(sampler, "111000777", nav=0)
    _pos(sampler, "111000777", lat=35.01, nav=0)
    _pos(sampler, "111000777", lat=35.02, nav=0)

    # More than 30 minutes passed since the first persisted observation, but
    # there was never a 30-minute silence between actual received fixes.
    assert _observation_count("111000777") == 1
