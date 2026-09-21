from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.intel.store import IntelEvent

_NOW = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)


def _event(**overrides):
    values = {
        "id": "sat-event-1", "type": "twitter", "severity": "high",
        "lat": 35.5, "lon": 14.1, "source": "Alarm Phone",
        "title": "Distress report", "text": "", "timestamp_utc": _NOW.isoformat(),
        "metadata": {"is_distress": True, "maritime_domain": "sar"},
    }
    values.update(overrides)
    return IntelEvent(**values)


def test_satellite_enrichment_eligibility_is_signal_gated():
    from core.intel.satellite_enrichment import is_satellite_enrichment_candidate

    assert is_satellite_enrichment_candidate(_event(), now=_NOW) is True
    assert is_satellite_enrichment_candidate(_event(lat=None), now=_NOW) is False
    raw_ais = _event(type="ais_anomaly", metadata={"maritime_domain": "grey_zone"})
    assert is_satellite_enrichment_candidate(raw_ais, now=_NOW) is False


def test_published_maritime_event_is_candidate_but_old_event_is_not():
    from core.intel.satellite_enrichment import is_satellite_enrichment_candidate

    maritime = _event(
        type="correlated_alert",
        metadata={"publication_status": "published", "maritime_domain": "grey_zone"},
    )
    assert is_satellite_enrichment_candidate(maritime, now=_NOW) is True

    old = _event(timestamp_utc=(_NOW - timedelta(days=8)).isoformat())
    assert is_satellite_enrichment_candidate(old, now=_NOW) is False


def test_trajectory_targets_sample_case_drift_over_time():
    from core.intel.satellite_enrichment import _trajectory_targets

    trajectory = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [14.0, 35.0],
                [14.1, 35.1],
                [14.2, 35.2],
                [14.3, 35.3],
                [14.4, 35.4],
            ],
        },
        "properties": {
            "timestamps_utc": [
                "2026-09-05T00:00:00Z",
                "2026-09-05T01:00:00Z",
                "2026-09-05T02:00:00Z",
                "2026-09-05T03:00:00Z",
                "2026-09-05T04:00:00Z",
            ]
        },
    }
    targets = _trajectory_targets(trajectory, drift_id="drift-1", limit=3)
    assert len(targets) == 3
    assert all(row["role"] == "drift_trajectory" for row in targets)
    assert all(row["drift_id"] == "drift-1" for row in targets)
    assert targets[-1]["lat"] == 35.4
    assert targets[-1]["lon"] == 14.4
    assert targets[-1]["at"].hour == 4


def test_enrich_event_queries_reverse_nearest_forward_and_persists(monkeypatch):
    from core.intel import satellite_enrichment
    from core.intel.satellite_observation import SatelliteObservation

    calls = []
    persisted = []

    def fake_resolve_for_incident(**kwargs):
        calls.append(kwargs["direction"])
        return [SatelliteObservation(
            observation_id=f"sat-{kwargs['direction']}", incident_id=kwargs["incident_id"],
            provider="test", mission="Sentinel-1", product_id=kwargs["direction"],
            acquisition_time=_NOW.isoformat(), discovered_at=_NOW.isoformat(),
            footprint=None, bbox=None, sensor_type="sar",
            temporal_relation=kwargs["direction"], temporal_delta_s=0,
            asset_ref="", source_url="", provenance={},
        )]

    monkeypatch.setattr(satellite_enrichment, "resolve_for_incident", fake_resolve_for_incident)
    monkeypatch.setattr(satellite_enrichment, "persist_observations", lambda rows: persisted.extend(rows) or len(rows))

    report = satellite_enrichment.enrich_event(_event(), now=_NOW, include_viirs=False)
    assert calls == ["reverse", "nearest", "forward"]
    assert len(persisted) == 3
    assert report["persisted"] == 3
    assert report["errors"] == 0


def test_enrich_event_adds_nearest_searches_for_drift_targets(monkeypatch):
    from core.intel import satellite_enrichment
    from core.intel.satellite_observation import SatelliteObservation

    calls = []
    persisted = []

    monkeypatch.setattr(
        satellite_enrichment,
        "_case_satellite_targets",
        lambda _event: [
            {"role": "reported_origin", "lat": 35.5, "lon": 14.1, "at": _NOW},
            {
                "role": "drift_trajectory", "lat": 35.6, "lon": 14.2,
                "at": _NOW + timedelta(hours=2), "drift_id": "drift-1",
                "trajectory_index": 2,
            },
        ],
    )

    def fake_resolve_for_incident(**kwargs):
        calls.append((kwargs["lat"], kwargs["lon"], kwargs["event_time"], kwargs["direction"]))
        return [SatelliteObservation(
            observation_id=f"sat-{len(calls)}", incident_id=kwargs["incident_id"],
            provider="test", mission="Sentinel-1", product_id=str(len(calls)),
            acquisition_time=kwargs["event_time"].isoformat(),
            discovered_at=_NOW.isoformat(), footprint=None, bbox=None,
            sensor_type="sar", temporal_relation=kwargs["direction"],
            temporal_delta_s=0, asset_ref="", source_url="", provenance={},
        )]

    monkeypatch.setattr(satellite_enrichment, "resolve_for_incident", fake_resolve_for_incident)
    monkeypatch.setattr(
        satellite_enrichment,
        "persist_observations",
        lambda rows: persisted.extend(rows) or len(rows),
    )

    report = satellite_enrichment.enrich_event(_event(), now=_NOW, include_viirs=False)
    assert [row[3] for row in calls] == ["reverse", "nearest", "forward", "nearest"]
    assert calls[-1][:2] == (35.6, 14.2)
    assert calls[-1][2] == _NOW + timedelta(hours=2)
    assert persisted[-1].provenance["case_targets"][0]["role"] == "drift_trajectory"
    assert persisted[-1].provenance["case_targets"][0]["drift_id"] == "drift-1"
    assert report == {"persisted": 4, "errors": 0}


def test_enrich_event_degrades_safely_when_provider_fails(monkeypatch):
    from core.intel import satellite_enrichment

    def fail(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(satellite_enrichment, "resolve_for_incident", fail)
    report = satellite_enrichment.enrich_event(_event(), now=_NOW, include_viirs=False)
    assert report == {"persisted": 0, "errors": 3}


def test_scheduler_satellite_job_calls_bounded_enrichment(monkeypatch):
    from core import scheduler

    calls = []
    monkeypatch.setattr(
        "core.intel.satellite_enrichment.enrich_recent_events",
        lambda **kwargs: calls.append(kwargs) or {"scanned": 4, "enriched": 2, "persisted": 5, "errors": 0},
    )
    scheduler._job_satellite_enrichment()
    assert calls == [{"limit": 6}]
