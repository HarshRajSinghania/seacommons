# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///./core/data/test_ngo_response.db")
os.environ.setdefault("RUNTIME_PROFILE", "operational")

from core.api.main import app
from core.intel.ngo_response import (
    _bearing_deg,
    _bearing_delta,
    _haversine_nm,
    analyze_ngo_response,
)
from core.intel.store import IntelEvent, IntelStore
from fastapi.testclient import TestClient

client = TestClient(app)


def _public_event(event_id: str, lat: float, lon: float) -> IntelEvent:
    return IntelEvent(
        id=event_id,
        type="twitter",
        severity="high",
        lat=lat,
        lon=lon,
        title="Reported distress south of Lampedusa",
        source="Alarm Phone",
        metadata={
            "source_policy": "operator_published",
            "publication_status": "published",
            "is_distress": True,
        },
    )


def _fake_registry(rows, now=None):
    """rows: (mmsi, name, lat, lon, speed_kn, course_deg, last_seen)."""
    features = []
    for mmsi, name, lat, lon, speed, course, last_seen in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "mmsi": mmsi,
                "ship_name": name,
                "speed": speed,
                "course": course,
                "last_seen": last_seen,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _blank_spike_store(monkeypatch):
    store = IntelStore()
    monkeypatch.setattr("core.intel.store.intel_store", store)
    return store


def test_haversine_and_bearing_helpers():
    assert abs(_haversine_nm(35.5, 12.6, 35.5, 12.6)) < 1e-6
    assert abs(_haversine_nm(35.5, 12.6, 35.5, 12.61)) - 0.6 < 0.1
    assert abs(_bearing_deg(0.0, 0.0, 1.0, 0.0) - 0.0) < 1.0
    assert abs(_bearing_deg(0.0, 0.0, 0.0, 1.0) - 90.0) < 1.0
    assert _bearing_delta(350, 10) == 20
    assert _bearing_delta(10, 350) == 20


def test_ngo_heading_toward_and_away(monkeypatch):
    _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    episode = _public_event("ep01", 35.5, 12.6)

    # Ocean Viking placed just SW of the episode, course set exactly toward it.
    bearing = _bearing_deg(35.4, 12.5, 35.5, 12.6)
    geo = _fake_registry([
        ("258479000", "Ocean Viking", 35.4, 12.5, 12.0, bearing, (now - timedelta(minutes=10)).isoformat()),
        # Geo Barents also SW of the episode but heading directly away from it.
        ("258826000", "Geo Barents", 35.4, 12.5, 10.0, (bearing + 180) % 360, (now - timedelta(minutes=40)).isoformat()),
        # A non-NGO cargo vessel in range: counted as asset, not an NGO.
        ("245789000", "Cargo Generic", 35.55, 12.65, 9.0, 0.0, (now - timedelta(minutes=5)).isoformat()),
    ])

    result = analyze_ngo_response(episode, now=now, registry_geojson=geo)

    assert result["episode"]["id"] == "ep01"
    assert len(result["ngo_vessels"]) == 2
    by_name = {row["name"]: row for row in result["ngo_vessels"]}

    ov = by_name["Ocean Viking"]
    assert ov["heading_toward"] is True
    assert ov["org"] == "SOS Méditerranée"
    assert ov["role"] == "SAR"
    assert ov["eta_h"] is not None and ov["eta_h"] > 0
    assert ov["fix_age_min"] == 10
    assert ov["track_saved"] == "AIS fix recorded 10 min ago"

    gb = by_name["Geo Barents"]
    assert gb["heading_toward"] is False

    assert result["cross_check"]["total_vessels_within_50nm"] == 3
    assert result["cross_check"]["ngo_vessels_within_50nm"] == 2
    assert result["summary"]["approaching_ngo_vessels"] == 1
    assert result["summary"]["nearest_ngo"]["name"] == "Ocean Viking"

    # GeoJSON: one line + one point per NGO vessel.
    assert len(result["geojson"]["features"]) == 4


def test_ngo_out_of_range_is_excluded(monkeypatch):
    _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    episode = _public_event("ep02", 35.5, 12.6)
    geo = _fake_registry([
        ("258479000", "Ocean Viking", 41.0, 20.0, 12.0, 90.0, now.isoformat()),
    ])
    result = analyze_ngo_response(episode, now=now, registry_geojson=geo)
    assert result["ngo_vessels"] == []
    assert result["summary"]["ngo_vessels_in_range"] == 0


def test_recent_spike_flags_are_reused(monkeypatch):
    store = _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    spike = IntelEvent(
        id="spike01",
        type="ais_spike",
        severity="high",
        lat=35.4,
        lon=12.5,
        title="AIS: Search Pattern — Ocean Viking",
        source="AIS Registry",
        linked_mmsi="258479000",
        metadata={"spike_type": "ngo_search_pattern", "org": "SOS Méditerranée"},
        timestamp_utc=(now - timedelta(minutes=30)).isoformat(),
    )
    store.add(spike, dedup_key="spike01")

    episode = _public_event("ep03", 35.5, 12.6)
    bearing = _bearing_deg(35.4, 12.5, 35.5, 12.6)
    geo = _fake_registry([
        ("258479000", "Ocean Viking", 35.4, 12.5, 4.0, bearing, now.isoformat()),
    ])
    result = analyze_ngo_response(episode, now=now, registry_geojson=geo)
    flags = result["ngo_vessels"][0]["motion_flags"]
    assert "search_pattern" in flags
    assert "speed_spike" not in flags


def test_current_drift_can_link_search_pattern_to_distress_far_from_origin(monkeypatch):
    from core.vessels.ais_coverage import CoverageAssessment, coverage_state

    monkeypatch.setattr(
        coverage_state,
        "assess",
        lambda **_kwargs: CoverageAssessment(
            status="coverage_present",
            active_upstreams=frozenset({"aisstream"}),
            degraded_upstreams=frozenset(),
            confidence=0.95,
            reason_codes=("COVERAGE_PRESENT",),
            gap_eligible=True,
        ),
    )
    store = _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    store.add(IntelEvent(
        id="search-drift-1",
        type="ais_spike",
        severity="high",
        lat=39.0,
        lon=18.0,
        title="AIS: Search Pattern — Ocean Viking",
        source="AIS Registry",
        linked_mmsi="258479000",
        metadata={"spike_type": "ngo_search_pattern"},
        timestamp_utc=(now - timedelta(minutes=10)).isoformat(),
    ), dedup_key="search-drift-1")

    episode = _public_event("ep-drift", 35.5, 12.6)
    geo = _fake_registry([
        ("258479000", "Ocean Viking", 39.0, 18.0, 3.0, 90.0, now.isoformat()),
    ])
    drift = {
        "drift_id": "drift-ep-drift",
        "trajectory": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[17.8, 38.9], [18.0, 39.0]]},
            "properties": {},
        },
        "cone_24h": None,
        "impact_point": None,
    }

    result = analyze_ngo_response(
        episode,
        now=now,
        registry_geojson=geo,
        drift_context=drift,
    )
    vessel = result["ngo_vessels"][0]
    assert vessel["distance_nm"] > 250
    assert vessel["distance_to_drift_nm"] == 0
    assert vessel["operational_target"] == "current_drift"
    assert vessel["mission_state"] == "probable_rescue_activity"
    assert result["summary"]["current_drift_id"] == "drift-ep-drift"


def test_search_pattern_in_srr_without_nearby_distress_stays_candidate(monkeypatch):
    store = _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    store.add(IntelEvent(
        id="search-candidate-1",
        type="ais_spike",
        severity="high",
        lat=35.5,
        lon=12.6,
        title="AIS: Search Pattern — Ocean Viking",
        source="AIS Registry",
        linked_mmsi="258479000",
        metadata={"spike_type": "ngo_search_pattern"},
        timestamp_utc=(now - timedelta(minutes=10)).isoformat(),
    ), dedup_key="search-candidate-1")
    monkeypatch.setattr(
        "core.intel.ngo_response._sar_zone_context",
        lambda lat, lon: [{"id": "malta_srr", "name": "Maltese SRR"}],
    )
    monkeypatch.setattr(
        "core.intel.ngo_response._haversine_nm",
        lambda lat1, lon1, lat2, lon2: 100.0,
    )
    episode = _public_event("ep-far", 37.0, 15.0)
    geo = _fake_registry([
        ("258479000", "Ocean Viking", 35.5, 12.6, 3.0, 180.0, now.isoformat()),
    ])
    result = analyze_ngo_response(episode, now=now, registry_geojson=geo)
    vessel = result["ngo_vessels"][0]
    assert vessel["in_named_srr"] is True
    assert vessel["mission_state"] == "search_candidate"


def test_speed_spike_flag_on_sprint(monkeypatch):
    _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    episode = _public_event("ep04", 35.5, 12.6)
    bearing = _bearing_deg(35.4, 12.5, 35.5, 12.6)
    geo = _fake_registry([
        ("258479000", "Ocean Viking", 35.4, 12.5, 21.0, bearing, now.isoformat()),
    ])
    result = analyze_ngo_response(episode, now=now, registry_geojson=geo)
    assert "speed_spike" in result["ngo_vessels"][0]["motion_flags"]


def test_related_signals_cross_check(monkeypatch):
    _blank_spike_store(monkeypatch)
    now = datetime.now(timezone.utc)
    episode = _public_event("ep05", 35.5, 12.6)
    related = [
        _public_event("ep06", 35.6, 12.7),
        _public_event("ep07", 39.0, 20.0),  # far away → excluded
    ]
    result = analyze_ngo_response(episode, now=now, registry_geojson=_fake_registry([]), related_signals=related)
    assert len(result["cross_check"]["related_signals"]) == 1
    assert result["cross_check"]["related_signals"][0]["id"] == "ep06"
    assert result["summary"]["related_signals"] == 1


def test_route_returns_404_for_unknown_and_unpublished(monkeypatch):
    store = IntelStore()
    store.add(_public_event("priv01", 35.5, 12.6), dedup_key="priv01")
    private = _public_event("priv02", 35.5, 12.6)
    private.metadata["publication_status"] = "private"
    private.metadata["source_policy"] = "unofficial"
    store.add(private, dedup_key="priv02")
    monkeypatch.setattr("core.api.routes.live.intel_store", store)

    assert client.get("/api/v1/live/signals/nope/response").status_code == 404
    assert client.get("/api/v1/live/signals/intel:priv02/response").status_code == 404


def test_route_returns_422_for_unpositioned(monkeypatch):
    store = IntelStore()
    event = _public_event("nopos01", None, None)
    event.metadata["is_distress"] = True
    store.add(event, dedup_key="nopos01")
    monkeypatch.setattr("core.api.routes.live.intel_store", store)
    assert client.get("/api/v1/live/signals/nopos01/response").status_code == 422


def test_route_returns_ngo_cross_check(monkeypatch):
    now = datetime.now(timezone.utc)
    store = IntelStore()
    event = _public_event("live01", 35.5, 12.6)
    store.add(event, dedup_key="live01")
    monkeypatch.setattr("core.api.routes.live.intel_store", store)
    monkeypatch.setattr("core.intel.store.intel_store", store)

    from core.vessels.registry import registry

    bearing = _bearing_deg(35.4, 12.5, 35.5, 12.6)
    monkeypatch.setattr(
        registry,
        "get_geojson",
        lambda: _fake_registry([
            ("258479000", "Ocean Viking", 35.4, 12.5, 12.0, bearing, now.isoformat()),
        ]),
    )

    resp = client.get("/api/v1/live/signals/intel:live01/response")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["episode"]["id"] == "live01"
    assert len(payload["ngo_vessels"]) == 1
    assert payload["ngo_vessels"][0]["name"] == "Ocean Viking"
    assert payload["ngo_vessels"][0]["heading_toward"] is True
    assert len(payload["geojson"]["features"]) == 2
