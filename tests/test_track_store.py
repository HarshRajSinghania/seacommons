# SPDX-License-Identifier: AGPL-3.0-or-later
"""AIS track history store."""
from __future__ import annotations

import os

os.environ["SEACOMMONS_TRACK_STORE_SYNC"] = "1"

import time
from datetime import datetime, timedelta, timezone

import pytest

from core.vessels.track_store import TrackStore


@pytest.fixture
def store():
    s = TrackStore()
    yield s


def _pos(store, mmsi, lat, lon, sog=10.0, nav=0, recv=None):
    store.on_position(mmsi, "TEST", lat, lon, sog=sog, cog=90.0, heading=88.0,
                      nav_status=nav, received_at=recv or datetime.now(timezone.utc))


def test_position_is_stored(store):
    _pos(store, "111000111", 35.0, 18.0)
    rows = store.track("111000111")
    assert len(rows) == 1
    assert rows[0]["lat"] == 35.0 and rows[0]["sog"] == 10.0


def test_throttled_within_interval(store):
    _pos(store, "111000222", 35.0, 18.0)
    _pos(store, "111000222", 35.0001, 18.0001)   # < 60 s later, tiny move
    assert len(store.track("111000222")) == 1


def test_nav_status_change_breaks_throttle(store):
    _pos(store, "111000333", 35.0, 18.0, nav=0)
    _pos(store, "111000333", 35.0001, 18.0001, nav=1)   # status change → kept
    assert len(store.track("111000333")) == 2


def test_long_jump_breaks_throttle(store):
    _pos(store, "111000444", 35.0, 18.0)
    _pos(store, "111000444", 35.2, 18.3)   # ~15 nm jump → kept
    assert len(store.track("111000444")) == 2


def test_silent_since(store):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _pos(store, "111000555", 35.0, 18.0, sog=12.0, recv=old)
    # force the in-memory last-seen clock back
    store._last["111000555"].ts = time.time() - 7200
    quiet = dict(store.silent_since(min_silent_s=3600))
    assert "111000555" in quiet
    assert dict(store.silent_since(min_silent_s=3 * 3600)) == {}


def test_recent_last_seen_hydrates_gap_state_after_restart(store):
    mmsi = "111000556"
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _pos(store, mmsi, 35.2, 18.2, sog=11.0, recv=old)

    restarted = TrackStore()
    assert restarted._last == {}
    assert restarted.hydrate_recent_last_seen(max_age_hours=12) >= 1

    quiet = dict(restarted.silent_since(min_silent_s=3600))
    assert mmsi in quiet
    assert quiet[mmsi].lat == 35.2
    assert quiet[mmsi].sog == 11.0


def test_positions_between_bbox(store):
    now = datetime.now(timezone.utc)
    _pos(store, "111000666", 35.0, 18.0, recv=now)
    _pos(store, "111000777", 10.0, 5.0, recv=now)
    hits = store.positions_between(now - timedelta(minutes=5), now + timedelta(minutes=5),
                                   bbox=(17.0, 34.0, 19.0, 36.0))
    assert {h["mmsi"] for h in hits} == {"111000666"}


def test_recent_tracks_batches_and_limits_each_mmsi(store):
    now = datetime.now(timezone.utc)
    for index in range(4):
        _pos(store, "111001111", 35.0 + index / 100, 18.0, nav=index % 2, recv=now + timedelta(minutes=index))
        _pos(store, "222002222", 36.0 + index / 100, 19.0, nav=index % 2, recv=now + timedelta(minutes=index))

    tracks = store.recent_tracks(
        {"111001111", "222002222"},
        since=now - timedelta(minutes=1),
        limit_per_mmsi=2,
    )

    assert set(tracks) == {"111001111", "222002222"}
    assert all(len(points) == 2 for points in tracks.values())
    assert tracks["111001111"][-1]["lat"] == 35.03


def test_prune(store):
    old = datetime.now(timezone.utc) - timedelta(days=90)
    _pos(store, "111000888", 35.0, 18.0, recv=old)
    assert store.prune(older_than_days=60) == 1
    assert store.track("111000888") == []
