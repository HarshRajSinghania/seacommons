# SPDX-License-Identifier: AGPL-3.0-or-later
"""VesselRegistry: nav_status persistence and GeoJSON projection."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from core.vessels.registry import VesselRegistry


def test_nav_status_round_trips_through_geojson(tmp_path):
    reg = VesselRegistry(db_path=tmp_path / "v.db")
    reg.upsert(
        "111000111", ship_name="Test", lat=35.0, lon=13.0,
        speed=0.2, course=90.0, nav_status=1,
        last_seen=datetime.now(timezone.utc),
    )
    feats = reg.get_geojson()["features"]
    assert feats and feats[0]["properties"]["nav_status"] == 1


def test_nav_status_is_coalesced_not_overwritten_by_a_later_position(tmp_path):
    reg = VesselRegistry(db_path=tmp_path / "v.db")
    now = datetime.now(timezone.utc)
    reg.upsert("111000222", lat=35.0, lon=13.0, nav_status=5, last_seen=now)
    # a later PositionReport with no NavigationalStatus must not wipe the last one
    reg.upsert("111000222", lat=35.1, lon=13.1, speed=0.0, last_seen=now)
    feats = {f["properties"]["mmsi"]: f for f in reg.get_geojson()["features"]}
    assert feats["111000222"]["properties"]["nav_status"] == 5


def test_added_column_migration_on_a_preexisting_db(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE vessels (mmsi TEXT PRIMARY KEY, ship_name TEXT, "
        "last_lat REAL, last_lon REAL, last_speed REAL, last_course REAL, "
        "last_heading REAL, last_seen TEXT, updated_at TEXT);"
    )
    con.commit()
    con.close()
    reg = VesselRegistry(db_path=db)  # _init_db must ALTER in nav_status
    reg.upsert("111000333", lat=35.0, lon=13.0, nav_status=6,
               last_seen=datetime.now(timezone.utc))
    feats = reg.get_geojson()["features"]
    assert feats[0]["properties"]["nav_status"] == 6


def test_geojson_exposes_public_visual_and_report_contract(tmp_path):
    reg = VesselRegistry(db_path=tmp_path / "v.db")
    now = datetime.now(timezone.utc)
    reg.upsert("111000444", ship_name="  Ocean   Test  ", lat=35.0, lon=13.0,
               speed=8.2, course=91.0, heading=92.0, nav_status=0, last_seen=now)
    reg.upsert("111000555", ship_name="Stopped", lat=35.2, lon=13.2,
               speed=0.1, course=10.0, heading=511.0, nav_status=5, last_seen=now)
    feats = {f["properties"]["mmsi"]: f["properties"] for f in reg.get_geojson()["features"]}
    moving = feats["111000444"]
    stopped = feats["111000555"]
    assert moving["display_name"] == "OCEAN TEST"
    assert moving["entity_kind"] == "vessel"
    assert moving["motion_state"] == "moving"
    assert moving["visual_shape"] == "triangle"
    assert moving["heading_deg"] == 92.0
    assert moving["show_heading"] is True
    assert moving["layer_membership"] == ["maritime", "ais", "ais_moving"]
    assert moving["report_type"] == "vessel"
    assert moving["report_id"] == "111000444"
    assert stopped["motion_state"] == "stationary"
    assert stopped["visual_shape"] == "circle"
    assert stopped["show_heading"] is False
    assert stopped["layer_membership"] == ["maritime", "ais", "ais_stationary"]

    reg.upsert("111000666", ship_name="Moored", lat=35.3, lon=13.3,
               speed=0.8, course=80.0, nav_status=5, last_seen=now)
    moored = {f["properties"]["mmsi"]: f["properties"] for f in reg.get_geojson(since=now.isoformat())["features"]}["111000666"]
    assert moored["motion_state"] == "stationary"
    assert moored["visual_shape"] == "circle"

def test_static_update_does_not_refresh_position_freshness(tmp_path):
    from datetime import datetime, timedelta, timezone

    reg = VesselRegistry(db_path=tmp_path / "v.db")
    old = datetime.now(timezone.utc) - timedelta(hours=4)
    reg.upsert("111000888", lat=35.0, lon=13.0, last_seen=old)
    before = reg._cache["111000888"]["last_seen"]

    reg.upsert("111000888", ship_name="RENAMED", destination="PORT")

    assert reg._cache["111000888"]["last_seen"] == before
    assert reg._cache["111000888"]["last_lat"] == 35.0
    assert reg._cache["111000888"]["ship_name"] == "RENAMED"


def test_older_position_cannot_overwrite_newer_position(tmp_path):
    from datetime import datetime, timedelta, timezone

    reg = VesselRegistry(db_path=tmp_path / "v.db")
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(hours=1)
    reg.upsert("111000889", lat=36.0, lon=14.0, speed=9.0, last_seen=newer)
    reg.upsert(
        "111000889",
        lat=30.0,
        lon=5.0,
        speed=1.0,
        ship_name="STATIC INFO MAY UPDATE",
        last_seen=older,
    )

    row = reg._cache["111000889"]
    assert row["last_lat"] == 36.0
    assert row["last_lon"] == 14.0
    assert row["last_speed"] == 9.0
    assert row["last_seen"] == newer.isoformat()
    assert row["ship_name"] == "STATIC INFO MAY UPDATE"
