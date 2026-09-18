# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift-cued dark-ship search geometry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.mda.darkship_cue import (
    _gfw_sar_in_area,
    _recent_s1_scenes,
    build,
    reachable_polygon,
)


def test_s1_stac_query_uses_the_current_copernicus_endpoint(monkeypatch):
    """docs/fixes.md M0.5: the old catalogue.dataspace.copernicus.eu/stac
    endpoint is stale; must use the current Data Space STAC API."""
    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": []}

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr("httpx.post", fake_post)
    _recent_s1_scenes((13.0, 34.0, 14.0, 35.0), datetime.now(timezone.utc))

    assert calls == ["https://stac.dataspace.copernicus.eu/v1/search"]


def test_gfw_sar_query_uses_4wings_and_classifies_unmatched_locally(monkeypatch):
    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "entries": [{
                    "public-global-sar-presence:v4.0": [
                        {
                            "lat": 35.77,
                            "lon": 13.47,
                            "entryTimestamp": "2026-09-11T17:03:35Z",
                            "mmsi": "",
                            "vesselId": "",
                        },
                        {
                            "lat": 35.46,
                            "lon": 13.31,
                            "entryTimestamp": "2026-09-11T05:12:50Z",
                            "mmsi": "224165270",
                            "vesselId": "vessel-1",
                        },
                    ],
                }],
            }

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        calls.append({
            "url": url, "params": params, "json": json,
            "headers": headers, "timeout": timeout,
        })
        return _FakeResponse()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr("core.config.config.GFW_API_TOKEN", "test-token")

    since = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
    rows = _gfw_sar_in_area((13.0, 35.0, 14.0, 36.0), since, until=until)

    assert calls[0]["url"] == "https://gateway.api.globalfishingwatch.org/v3/4wings/report"
    assert calls[0]["params"]["datasets[0]"] == "public-global-sar-presence:latest"
    assert calls[0]["params"]["date-range"] == "2026-09-11,2026-09-12"
    assert calls[0]["json"]["geojson"]["type"] == "FeatureCollection"
    assert rows[0]["matched"] is False
    assert rows[0]["timestamp"] == "2026-09-11T17:03:35Z"
    assert rows[1]["matched"] is True
    assert rows[1]["mmsi"] == "224165270"


def test_reachable_polygon_grows_with_time():
    small = reachable_polygon(35.0, 18.0, 90.0, 12.0, hours=1.0)
    big = reachable_polygon(35.0, 18.0, 90.0, 12.0, hours=6.0)
    assert big["_radius_km"] > small["_radius_km"] * 3
    assert small["coordinates"][0][0] == small["coordinates"][0][-1]   # closed ring


def test_reachable_polygon_biased_forward():
    poly = reachable_polygon(35.0, 18.0, 90.0, 15.0, hours=4.0)  # heading east
    ring = poly["coordinates"][0]
    east = max(c[0] for c in ring) - 18.0
    west = 18.0 - min(c[0] for c in ring)
    assert east > west   # extends further east (forward) than west (aft)


def test_build_offline_still_returns_area_and_estimate():
    cue = build(lat=34.5, lon=13.0, course_deg=270.0, speed_kn=10.0,
                gap_start=datetime.now(timezone.utc) - timedelta(hours=3))
    assert cue["dark_for_hours"] == 3.0
    assert cue["search_area"]["type"] == "Polygon"
    assert cue["next_s1_pass_estimate_hours"] >= 0
    assert "Sentinel-1" in cue["recommendation"] or "SAR" in cue["recommendation"]
    assert cue["association_status"] == "no_detection"


def test_unmatched_sar_detection_is_worded_as_a_candidate_not_a_confirmation(monkeypatch):
    """docs/fixes.md M0.5 exit gate: an unmatched detection cannot produce
    text equivalent to "likely/confirmed dark vessel" without a stronger
    association stage (acquisition-time AIS propagation, distance/
    uncertainty scoring -- docs/fixes.md M7.2, not built yet)."""
    monkeypatch.setattr(
        "core.mda.darkship_cue._gfw_sar_in_area",
        lambda bbox, since, **kwargs: [
            {"lat": 34.5, "lon": 13.0, "matched": False, "timestamp": "x"}
        ],
    )
    monkeypatch.setattr(
        "core.mda.darkship_cue._recent_s1_scenes",
        lambda bbox, since, **kwargs: [],
    )

    cue = build(lat=34.5, lon=13.0, course_deg=270.0, speed_kn=10.0,
                gap_start=datetime.now(timezone.utc) - timedelta(hours=3))

    assert cue["association_status"] == "unmatched_candidate"
    assert cue["gfw_unmatched_in_area"]
    recommendation = cue["recommendation"].lower()
    assert "candidate" in recommendation
    assert "likely the dark vessel" not in recommendation
    # Honest wording says "not a confirmed match" -- the word "confirmed" is
    # fine negated; what must never appear is an affirmative claim of one.
    assert "not a confirmed" in recommendation
    assert "is confirmed" not in recommendation
    assert "confirmed dark vessel" not in recommendation
