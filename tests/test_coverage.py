# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md M4.2: AIS reception-quality CoverageBaseline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.mda.coverage import compute_coverage_baseline


def test_dense_traffic_area_is_healthy_with_high_congestion(monkeypatch):
    import core.vessels.track_store as track_store_module

    own_rows = [{"mmsi": "111000111"}] * 6
    neighbour_rows = []
    for i in range(20):
        neighbour_mmsi = f"20000000{i}"
        neighbour_rows += [{"mmsi": neighbour_mmsi}] * 5
    monkeypatch.setattr(track_store_module.track_store, "track", lambda *a, **k: own_rows)
    monkeypatch.setattr(
        track_store_module.track_store, "positions_between",
        lambda *a, **k: own_rows + neighbour_rows,
    )
    monkeypatch.setattr("core.mda.jamming.jamming.in_jamming_zone", lambda *a, **k: 0.0)
    monkeypatch.setattr("core.intel.landmask.distance_to_coast_km", lambda *a, **k: 12.5)

    result = compute_coverage_baseline("111000111", 35.5, 14.1)
    assert result.source_health == "healthy"
    assert result.congestion == "high"
    assert result.local_receiver_density == 20
    assert result.preceding_track_density == 6
    assert result.coast_distance_km == 12.5
    assert result.jamming_context == 0.0


def test_no_nearby_traffic_at_all_is_unknown_not_a_guess(monkeypatch):
    import core.vessels.track_store as track_store_module

    monkeypatch.setattr(track_store_module.track_store, "track", lambda *a, **k: [])
    monkeypatch.setattr(track_store_module.track_store, "positions_between", lambda *a, **k: [])
    monkeypatch.setattr("core.mda.jamming.jamming.in_jamming_zone", lambda *a, **k: 0.0)
    monkeypatch.setattr("core.intel.landmask.distance_to_coast_km", lambda *a, **k: None)

    result = compute_coverage_baseline("111000222", 35.5, 14.1)
    assert result.source_health == "unknown"
    assert result.congestion == "unknown"
    assert result.local_receiver_density == 0
    assert result.neighbour_message_ratio is None


def test_neighbour_message_ratio_compares_against_the_local_median(monkeypatch):
    import core.vessels.track_store as track_store_module

    own_rows = [{"mmsi": "111000333"}] * 2  # this vessel reported half as often
    neighbour_rows = (
        [{"mmsi": "200000001"}] * 4
        + [{"mmsi": "200000002"}] * 4
        + [{"mmsi": "200000003"}] * 4
    )
    monkeypatch.setattr(track_store_module.track_store, "track", lambda *a, **k: own_rows)
    monkeypatch.setattr(
        track_store_module.track_store, "positions_between",
        lambda *a, **k: own_rows + neighbour_rows,
    )
    monkeypatch.setattr("core.mda.jamming.jamming.in_jamming_zone", lambda *a, **k: 0.1)
    monkeypatch.setattr("core.intel.landmask.distance_to_coast_km", lambda *a, **k: 5.0)

    result = compute_coverage_baseline("111000333", 35.5, 14.1)
    assert result.preceding_track_density == 2
    assert result.local_receiver_density == 3
    assert result.neighbour_message_ratio == 0.5  # 2 / median(4,4,4)


def test_never_raises_when_every_dependency_is_broken(monkeypatch):
    import core.vessels.track_store as track_store_module

    def _boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(track_store_module.track_store, "track", _boom)
    monkeypatch.setattr(track_store_module.track_store, "positions_between", _boom)
    monkeypatch.setattr("core.mda.jamming.jamming.in_jamming_zone", _boom)
    monkeypatch.setattr("core.intel.landmask.distance_to_coast_km", _boom)

    result = compute_coverage_baseline("111000444", 35.5, 14.1)
    assert result.source_health == "unknown"
    assert result.jamming_context is None
    assert result.coast_distance_km is None
    assert result.local_receiver_density == 0


def test_track_corridor_coverage_requires_temporal_spatial_witnesses():
    from datetime import timedelta

    from core.mda.coverage import compute_track_corridor_coverage
    from core.mda.sar_association import propagate_ais_state

    start = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=4)
    rows = []
    for hour in (2, 4):
        at = start + timedelta(hours=hour)
        projected = propagate_ais_state(
            last_lat=35.8,
            last_lon=14.2,
            last_observed_at=start,
            target_time=at,
            course_deg=90.0,
            speed_kn=10.0,
        )
        for index in range(3):
            rows.append({
                "mmsi": f"20000000{hour}{index}",
                "ts": at.isoformat(),
                "lat": projected.lat + index * 0.001,
                "lon": projected.lon + index * 0.001,
                "source": "aisstream",
            })

    class Store:
        def positions_between(self, *args, **kwargs):
            return rows

    result = compute_track_corridor_coverage(
        Store(),
        mmsi="111000777",
        last_lat=35.8,
        last_lon=14.2,
        gap_start=start,
        now=end,
        course_deg=90.0,
        speed_kn=10.0,
    )

    assert result.continuous is True
    assert result.covered_checkpoints == result.checkpoint_count == 2
    assert result.median_nearby_vessels == 3.0
    assert result.observed_sources == ("aisstream",)


def test_track_corridor_coverage_stays_false_without_route_observations():
    from datetime import timedelta

    from core.mda.coverage import compute_track_corridor_coverage

    class Store:
        def positions_between(self, *args, **kwargs):
            return []

    start = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    result = compute_track_corridor_coverage(
        Store(),
        mmsi="111000778",
        last_lat=35.8,
        last_lon=14.2,
        gap_start=start,
        now=start + timedelta(hours=7),
        course_deg=180.0,
        speed_kn=12.0,
    )

    assert result.continuous is False
    assert result.covered_checkpoints == 0
    assert result.median_nearby_vessels == 0.0


def test_expected_reporting_interval_uses_observed_vessel_cadence(monkeypatch):
    import core.vessels.track_store as track_store_module

    base = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    own_rows = [
        {"mmsi": "111000880", "ts": (base + timedelta(seconds=60 * i)).isoformat()}
        for i in range(15)
    ]
    neighbours = [
        {"mmsi": f"20000000{k}", "ts": base.isoformat()}
        for k in range(6)
    ]
    monkeypatch.setattr(track_store_module.track_store, "track", lambda *a, **k: own_rows)
    monkeypatch.setattr(
        track_store_module.track_store,
        "positions_between",
        lambda *a, **k: own_rows + neighbours,
    )
    monkeypatch.setattr("core.mda.jamming.jamming.in_jamming_zone", lambda *a, **k: 0.0)
    monkeypatch.setattr("core.intel.landmask.distance_to_coast_km", lambda *a, **k: 120.0)

    result = compute_coverage_baseline("111000880", 35.5, 14.1, at=base + timedelta(minutes=20))
    assert result.expected_reporting_interval_s == 60.0
    assert result.preceding_track_density == 15


def test_reception_expectation_requires_dense_history_neighbours_corridor_and_no_jamming():
    from core.mda.coverage import (
        CoverageBaseline,
        TrackCoverageContinuity,
        build_reception_expectation,
    )

    baseline = CoverageBaseline(
        mmsi="111000881",
        at=datetime.now(timezone.utc),
        source_health="healthy",
        expected_reporting_interval_s=60.0,
        local_receiver_density=8,
        neighbour_message_ratio=1.0,
        coast_distance_km=100.0,
        congestion="medium",
        jamming_context=0.0,
        preceding_track_density=18,
    )
    corridor = TrackCoverageContinuity(
        method_version="track-corridor-coverage/v1",
        checkpoint_count=3,
        covered_checkpoints=3,
        covered_fraction=1.0,
        min_nearby_vessels=5,
        median_nearby_vessels=7.0,
        radius_nm=30.0,
        time_window_min=50.0,
        observed_sources=("aisstream",),
    )
    strong = build_reception_expectation(
        gap_duration_s=4 * 3600,
        coverage=baseline,
        nearby_vessels_reporting_before=8,
        nearby_vessels_reporting_after=9,
        corridor=corridor,
        jamming_score=0.0,
    )
    assert strong.strong is True
    assert strong.expected_messages_during_gap == 240
    assert "DENSE_PRE_GAP_REPORTING_HISTORY" in strong.reason_codes

    weak = build_reception_expectation(
        gap_duration_s=4 * 3600,
        coverage=baseline,
        nearby_vessels_reporting_before=8,
        nearby_vessels_reporting_after=1,
        corridor=corridor,
        jamming_score=0.0,
    )
    assert weak.strong is False


def test_at_defaults_to_now_and_is_echoed_back():
    before = datetime.now(timezone.utc)
    result = compute_coverage_baseline("111000555", 35.5, 14.1)
    after = datetime.now(timezone.utc)
    assert before <= result.at <= after
