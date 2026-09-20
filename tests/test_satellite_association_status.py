# SPDX-License-Identifier: AGPL-3.0-or-later
"""Satellite evidence as analytical evidence, not decorative images (spec
section 6): an unmatched SAR detection inside a reachable-area calculation
is stored with association_status="unmatched_candidate" -- a candidate,
never a claimed vessel match.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_satellite_observations():
    from core.db.models import SatelliteObservationDB
    from core.db.session import session_scope

    with session_scope() as db:
        db.query(SatelliteObservationDB).filter(
            SatelliteObservationDB.incident_id.like("sat-assoc-test%")
        ).delete(synchronize_session=False)
    yield
    with session_scope() as db:
        db.query(SatelliteObservationDB).filter(
            SatelliteObservationDB.incident_id.like("sat-assoc-test%")
        ).delete(synchronize_session=False)


def test_materialize_unmatched_sar_detections_builds_candidate_observations():
    from core.intel.satellite_observation import materialize_unmatched_sar_detections

    cue = {
        "association_status": "unmatched_candidate",
        "gfw_unmatched_in_area": [
            {"lat": 35.53, "lon": 14.15, "timestamp": "2026-09-17T10:15:00+00:00"},
            {"lat": 35.60, "lon": 14.20, "timestamp": "2026-09-17T11:00:00+00:00"},
        ],
    }
    observations = materialize_unmatched_sar_detections(incident_id="sat-assoc-test-1", cue=cue)
    assert len(observations) == 2
    for obs in observations:
        assert obs.association_status == "unmatched_candidate"
        assert obs.incident_id == "sat-assoc-test-1"
        assert obs.provider == "gfw"
        assert obs.sensor_type == "sar"
        assert obs.observation_id.startswith("sat:gfw_sar:")


def test_materialize_skips_detections_without_position():
    from core.intel.satellite_observation import materialize_unmatched_sar_detections

    cue = {"gfw_unmatched_in_area": [{"timestamp": "2026-09-17T10:15:00+00:00"}]}
    assert materialize_unmatched_sar_detections(incident_id="sat-assoc-test-2", cue=cue) == []


def test_materialize_is_empty_when_no_unmatched_detections():
    from core.intel.satellite_observation import materialize_unmatched_sar_detections

    assert materialize_unmatched_sar_detections(incident_id="sat-assoc-test-3", cue={}) == []


def test_association_status_persists_and_round_trips():
    from core.intel.satellite_observation import (
        list_incident_observations,
        materialize_unmatched_sar_detections,
        persist_observations,
    )

    cue = {"gfw_unmatched_in_area": [{"lat": 36.0, "lon": 13.0, "timestamp": "2026-09-17T09:00:00+00:00"}]}
    observations = materialize_unmatched_sar_detections(incident_id="sat-assoc-test-4", cue=cue)
    created = persist_observations(observations)
    assert created == 1

    # Idempotent by deterministic observation_id -- re-persisting the exact
    # same detection must not double-count.
    created_again = persist_observations(observations)
    assert created_again == 0

    reloaded = list_incident_observations("sat-assoc-test-4")
    assert len(reloaded) == 1
    assert reloaded[0].association_status == "unmatched_candidate"


def test_regular_satellite_observation_has_no_association_status_by_default():
    """A routine quicklook (no dark-ship cue) never gets association_status
    invented -- it only applies where a real candidate-match question
    exists."""
    from datetime import datetime, timezone

    from core.intel.satellite_observation import (
        SatelliteObservation,
        list_incident_observations,
        persist_observations,
    )

    obs = SatelliteObservation(
        observation_id="sat-assoc-test-5:quicklook",
        incident_id="sat-assoc-test-5",
        provider="copernicus",
        mission="sentinel2",
        product_id="S2A_MSIL1C_TEST",
        acquisition_time=datetime.now(timezone.utc).isoformat(),
        discovered_at=datetime.now(timezone.utc).isoformat(),
        footprint=None, bbox=None, sensor_type="optical",
        temporal_relation="nearest", temporal_delta_s=0.0,
        asset_ref="", source_url="", provenance={},
    )
    persist_observations([obs])
    reloaded = list_incident_observations("sat-assoc-test-5")
    assert len(reloaded) == 1
    assert reloaded[0].association_status is None
