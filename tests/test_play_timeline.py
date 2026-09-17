from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from core.api.main import app
from core.intel.humanitarian_incident import sync_incident_for_event
from core.intel.store import IntelEvent
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_play_tables():
    from core.db.models import (
        DriftResultDB,
        HumanitarianIncidentDB,
        IncidentTransitionDB,
        SatelliteObservationDB,
    )
    from core.db.session import engine, session_scope

    for table in (HumanitarianIncidentDB, IncidentTransitionDB, DriftResultDB, SatelliteObservationDB):
        table.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.query(HumanitarianIncidentDB).delete()
        db.query(IncidentTransitionDB).delete()
        db.query(DriftResultDB).delete()
        db.query(SatelliteObservationDB).delete()
    yield


def _seed_case(*, lifecycle="resolved", age_hours=30, with_update=True):
    from core.db.models import DriftResultDB, IntelEventDB
    from core.db.session import session_scope

    event_id = f"play-{uuid.uuid4()}"
    reported = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    update_at = reported + timedelta(hours=26)
    thread = []
    if with_update:
        thread = [{
            "tweet_id": f"reply-{event_id}",
            "posted_at": update_at.isoformat(),
            "url": f"https://example.test/{event_id}/reply",
            "kind": "reply",
            "note": "We are still waiting for news about the people.",
        }]
    event = IntelEvent(
        id=event_id, type="distress", severity="high", lat=35.4, lon=14.2,
        title="Public humanitarian report", text="PRIVATE RAW MESSAGE MUST NOT LEAK",
        source="Alarm Phone", timestamp_utc=reported.isoformat(),
        url=f"https://example.test/{event_id}",
        metadata={
            "is_distress": True,
            "maritime_domain": "sar",
            "source_policy": "operator_published",
            "publication_status": "published",
            "thread_reposts": thread,
        },
    )
    with session_scope() as db:
        db.add(IntelEventDB(
            id=event.id, timestamp_utc=event.timestamp_utc, type=event.type,
            severity=event.severity, lat=event.lat, lon=event.lon,
            title=event.title, text=event.text, url=event.url, source=event.source,
            linked_mmsi="", maritime_domain="sar", meta=event.metadata,
        ))
    sync_incident_for_event(event, lifecycle=lifecycle)
    with session_scope() as db:
        db.add(DriftResultDB(
            drift_id=f"drift-{uuid.uuid4()}", event_id=f"intel:{event_id}",
            domain="ocean_sar", lat=event.lat, lon=event.lon, status="completed",
            trajectory={"type": "LineString", "coordinates": [[14.2, 35.4], [14.3, 35.5]]},
            metadata_json={"model": "OpenDrift"}, created_at=reported.replace(tzinfo=None) + timedelta(minutes=10),
        ))
    return event_id


def test_play_incident_index_exposes_real_status_not_archived_label():
    event_id = _seed_case(lifecycle="resolved")
    response = TestClient(app).get("/api/v1/play/incidents?limit=50")
    assert response.status_code == 200
    incident = next(item for item in response.json()["incidents"] if item["incident_id"] == event_id)
    assert incident["incident_status"] == "resolved"
    assert incident["surface"] == "play"
    assert incident["incident_status"] != "archived"


def test_play_timeline_orders_report_drift_and_late_attending_news():
    event_id = _seed_case(lifecycle="needs_review", age_hours=40, with_update=True)
    response = TestClient(app).get(f"/api/v1/play/incidents/{event_id}/timeline")
    assert response.status_code == 200
    payload = response.json()
    assert payload["incident_status"] == "needs_review"
    items = payload["timeline"]
    assert [item["at"] for item in items] == sorted(item["at"] for item in items)
    types = [item["type"] for item in items]
    assert "report" in types
    assert "drift" in types
    assert "attending_news" in types


def test_play_timeline_never_exposes_raw_private_event_text():
    event_id = _seed_case(lifecycle="resolved")
    response = TestClient(app).get(f"/api/v1/play/incidents/{event_id}/timeline")
    assert response.status_code == 200
    body = response.text
    assert "PRIVATE RAW MESSAGE MUST NOT LEAK" not in body


def test_recent_active_incident_is_also_in_general_play_catalog():
    event_id = _seed_case(lifecycle="active", age_hours=2, with_update=False)
    response = TestClient(app).get("/api/v1/play/incidents?limit=50")
    assert response.status_code == 200
    ids = {item["incident_id"] for item in response.json()["incidents"]}
    assert event_id in ids
    assert TestClient(app).get(f"/api/v1/play/incidents/{event_id}/timeline").status_code == 200



def test_play_index_exposes_drift_and_satellite_evidence_counts():
    from core.intel.satellite_observation import SatelliteObservation, persist_observations

    event_id = _seed_case(lifecycle="resolved")
    observation = SatelliteObservation(
        observation_id=f"sat-{uuid.uuid4()}", incident_id=event_id,
        provider="nasa_gibs", mission="VIIRS NOAA-21", product_id="VIIRS_CASE",
        acquisition_time="2026-09-04T09:30:00+00:00",
        discovered_at="2026-09-05T00:00:00+00:00",
        footprint=None, bbox=[14.0, 35.0, 14.2, 35.2], sensor_type="optical",
        temporal_relation="nearest", temporal_delta_s=0,
        asset_ref="https://example.test/{z}/{y}/{x}.jpg",
        source_url="https://example.test/viirs", provenance={},
    )
    assert persist_observations([observation]) == 1

    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    item = next(row for row in response.json()["incidents"] if row["incident_id"] == event_id)
    assert item["evidence_counts"] == {"drift": 1, "satellite": 1}

def test_play_timeline_includes_persisted_satellite_observation():
    from core.intel.satellite_observation import (
        SatelliteObservation,
        persist_observations,
    )

    event_id = _seed_case(lifecycle="resolved")
    observation = SatelliteObservation(
        observation_id=f"sat-{uuid.uuid4()}", incident_id=event_id,
        provider="copernicus_dataspace", mission="Sentinel-1", product_id="S1_CASE",
        acquisition_time="2026-09-04T09:30:00+00:00",
        discovered_at="2026-09-05T00:00:00+00:00",
        footprint={"type": "Polygon", "coordinates": []},
        bbox=[14.0, 35.0, 14.2, 35.2], sensor_type="sar",
        temporal_relation="reverse", temporal_delta_s=-7200,
        asset_ref="https://example.test/preview.jpg",
        source_url="https://example.test/stac/S1_CASE",
        provenance={"stac_collection": "sentinel-1-grd"},
    )
    assert persist_observations([observation]) == 1

    response = TestClient(app).get(f"/api/v1/play/incidents/{event_id}/timeline")
    assert response.status_code == 200
    satellite = next(item for item in response.json()["timeline"] if item["type"] == "satellite")
    assert satellite["properties"]["mission"] == "Sentinel-1"
    assert satellite["properties"]["temporal_relation"] == "reverse"
    assert satellite["properties"]["asset_ref"] == "https://example.test/preview.jpg"
    assert satellite["properties"]["bbox"] == [14.0, 35.0, 14.2, 35.2]


def _seed_maritime_play_event(*, age_hours=36):
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    event_id = f"maritime-{uuid.uuid4()}"
    reported = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with session_scope() as db:
        db.add(IntelEventDB(
            id=event_id, timestamp_utc=reported.isoformat(), type="vessel_incident",
            severity="medium", lat=36.2, lon=15.4,
            title="Public maritime incident", text="PRIVATE MARITIME RAW",
            url=f"https://example.test/{event_id}", source="maritime_osint",
            linked_mmsi="123456789", maritime_domain="maritime_security",
            meta={"publication_status": "published", "source_policy": "operator_published"},
        ))
    return event_id


def test_play_index_includes_public_historical_maritime_points():
    event_id = _seed_maritime_play_event()
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    item = next(row for row in response.json()["incidents"] if row["incident_id"] == event_id)
    assert item["domain"] == "maritime"
    assert item["geometry"] == {"type": "Point", "coordinates": [15.4, 36.2]}
    assert item["incident_status"] == "outcome_unknown"


def test_play_generic_maritime_timeline_is_privacy_safe():
    event_id = _seed_maritime_play_event()
    response = TestClient(app).get(f"/api/v1/play/incidents/{event_id}/timeline")
    assert response.status_code == 200
    payload = response.json()
    assert payload["domain"] == "maritime"
    assert payload["timeline"][0]["type"] == "report"
    assert "PRIVATE MARITIME RAW" not in response.text


def test_play_maritime_rejects_blocked_source_even_if_marked_published():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    event_id = f"blocked-{uuid.uuid4()}"
    reported = datetime.now(timezone.utc) - timedelta(hours=36)
    with session_scope() as db:
        db.add(IntelEventDB(
            id=event_id, timestamp_utc=reported.isoformat(), type="vessel_incident",
            severity="medium", lat=36.0, lon=15.0, title="Must stay private",
            text="PRIVATE", url="https://example.test/blocked", source="blocked",
            linked_mmsi="", maritime_domain="maritime_security",
            meta={"publication_status": "published", "source_policy": "unofficial"},
        ))
    index = TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]
    assert event_id not in {item["incident_id"] for item in index}
    assert TestClient(app).get(f"/api/v1/play/incidents/{event_id}/timeline").status_code == 404


def test_play_index_sorts_humanitarian_and_maritime_before_limit():
    _seed_case(lifecycle="resolved", age_hours=72, with_update=False)
    maritime_id = _seed_maritime_play_event(age_hours=30)
    response = TestClient(app).get("/api/v1/play/incidents?limit=1&offset=0")
    assert response.status_code == 200
    assert response.json()["incidents"][0]["incident_id"] == maritime_id


def test_play_index_paginates_combined_archive():
    _seed_maritime_play_event(age_hours=30)
    _seed_maritime_play_event(age_hours=31)
    _seed_maritime_play_event(age_hours=32)
    first = TestClient(app).get("/api/v1/play/incidents?limit=2&offset=0").json()
    second = TestClient(app).get(f"/api/v1/play/incidents?limit=2&offset={first['next_offset']}").json()
    first_ids = {item["incident_id"] for item in first["incidents"]}
    second_ids = {item["incident_id"] for item in second["incidents"]}
    assert first["next_offset"] == 2
    assert first_ids.isdisjoint(second_ids)

def test_play_index_excludes_raw_historical_security_detector_output():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    event_id = f"security-{uuid.uuid4()}"
    reported = datetime.now(timezone.utc) - timedelta(hours=36)
    with session_scope() as db:
        db.add(IntelEventDB(
            id=event_id, timestamp_utc=reported.isoformat(), type="ais_anomaly",
            severity="high", lat=35.8, lon=14.8,
            title="Historical AIS anomaly", text="internal raw",
            url="", source="SeaCommons", linked_mmsi="123456789",
            maritime_domain="grey_zone",
            meta={"maritime_domain": "grey_zone", "anomaly_type": "ais_gap"},
        ))
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    ids = {row["incident_id"] for row in response.json()["incidents"]}
    assert event_id not in ids


def test_play_counts_exposes_real_archive_total():
    _seed_case(lifecycle="resolved", age_hours=72, with_update=False)
    _seed_maritime_play_event(age_hours=30)
    response = TestClient(app).get("/api/v1/play/counts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == (
        payload["humanitarian_count"] + payload["maritime_count"] + payload.get("investigation_count", 0)
    )
    assert payload["total_count"] >= 2


def test_play_counts_route_is_sync_so_exact_scan_does_not_block_event_loop():
    import inspect

    from core.api.routes.play import play_counts
    assert inspect.iscoroutinefunction(play_counts) is False


def test_play_counts_uses_short_lived_exact_snapshot_cache(monkeypatch):
    from core.api.routes import play as play_routes

    calls = []
    def fake_compute():
        calls.append(1)
        return {
            "total_count": 12, "humanitarian_count": 2, "maritime_count": 10,
            "generated_at": "2026-09-05T14:00:00+00:00",
        }

    monkeypatch.setattr(play_routes, "_compute_play_counts", fake_compute)
    play_routes._play_counts_cache.clear()
    first = play_routes.play_counts()
    second = play_routes.play_counts()
    assert first == second
    assert first["total_count"] == 12
    assert len(calls) == 1
    play_routes._play_counts_cache.clear()


def test_play_db_routes_are_sync_for_threadpool_isolation():
    import inspect

    from core.api.routes.play import play_incident_timeline, play_incidents
    assert inspect.iscoroutinefunction(play_incidents) is False
    assert inspect.iscoroutinefunction(play_incident_timeline) is False


def test_play_catalog_includes_current_humanitarian_case():
    event_id = _seed_case(lifecycle="active", age_hours=2, with_update=False)
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    ids = {item["incident_id"] for item in response.json()["incidents"]}
    assert event_id in ids


def test_play_catalog_includes_public_maritime_record_without_coordinates():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    event_id = f"unpositioned-{uuid.uuid4()}"
    reported = datetime.now(timezone.utc) - timedelta(hours=36)
    with session_scope() as db:
        db.add(IntelEventDB(
            id=event_id, timestamp_utc=reported.isoformat(), type="vessel_incident",
            severity="medium", lat=None, lon=None,
            title="Public unpositioned maritime record", text="internal raw",
            url="", source="maritime_osint", linked_mmsi="",
            maritime_domain="safety",
            meta={"publication_status": "published", "source_policy": "operator_published",
                  "maritime_domain": "safety"},
        ))
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    item = next(row for row in response.json()["incidents"] if row["incident_id"] == event_id)
    assert item["geometry"] is None
    assert item["domain"] == "maritime"


def test_play_catalog_does_not_truncate_public_history_behind_nonpublic_candidates():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    public_id = _seed_maritime_play_event(age_hours=72)
    base = datetime.now(timezone.utc) - timedelta(hours=30)
    with session_scope() as db:
        db.add_all([
            IntelEventDB(
                id=f"blocked-bulk-{index}", timestamp_utc=(base - timedelta(seconds=index)).isoformat(),
                type="vessel_incident", severity="low", lat=36.0, lon=15.0,
                title="blocked", text="", url="", source="blocked", linked_mmsi="",
                maritime_domain="safety",
                meta={"publication_status": "private", "source_policy": "unofficial"},
            )
            for index in range(2100)
        ])
    response = TestClient(app).get("/api/v1/play/incidents?limit=1&offset=0")
    assert response.status_code == 200
    assert response.json()["incidents"][0]["incident_id"] == public_id
    assert response.json()["next_offset"] is None


def test_play_catalog_prefilters_explicit_internal_rows_before_public_projection(monkeypatch):
    from core.api.routes import play as play_routes
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    internal_id = f"internal-prefilter-{uuid.uuid4()}"
    reported = datetime.now(timezone.utc) - timedelta(hours=30)
    with session_scope() as db:
        db.add(IntelEventDB(
            id=internal_id, timestamp_utc=reported.isoformat(), type="correlated_alert",
            severity="low", lat=35.0, lon=15.0, title="internal derived", text="",
            url="", source="SeaCommons", linked_mmsi="", maritime_domain="grey_zone",
            meta={"publication_status": "internal", "maritime_domain": "grey_zone"},
        ))

    original = play_routes._is_public_catalog_maritime
    def checked(event):
        assert (event.meta or {}).get("publication_status") != "internal"
        return original(event)

    monkeypatch.setattr(play_routes, "_is_public_catalog_maritime", checked)
    catalog = play_routes._compute_play_catalog()
    assert internal_id not in {item["incident_id"] for item in catalog}

def test_play_catalog_hides_translated_duplicate_incident() -> None:
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    event_id = _seed_case(lifecycle="active", age_hours=2, with_update=False)
    with session_scope() as db:
        row = db.get(IntelEventDB, event_id)
        meta = dict(row.meta or {})
        meta["translation_of"] = "canonical-case"
        meta["publication_status"] = "internal"
        row.meta = meta

    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    assert response.status_code == 200
    ids = {item["incident_id"] for item in response.json()["incidents"]}
    assert event_id not in ids


def test_play_catalog_excludes_legacy_fusion_and_ais_only_casualty_but_keeps_beacon():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    now = datetime.now(timezone.utc) - timedelta(hours=30)
    suffix = str(uuid.uuid4())
    fusion_id = f"audit-fusion-{suffix}"
    aground_id = f"audit-aground-{suffix}"
    beacon_id = f"audit-beacon-{suffix}"
    with session_scope() as db:
        db.add_all([
            IntelEventDB(
                id=fusion_id, timestamp_utc=now.isoformat(), type="correlated_alert",
                severity="medium", lat=35.0, lon=14.0, title="Vessel unable to manoeuvre — AUDIT",
                text="", url="", source="SeaCommons fusion", linked_mmsi="209888001",
                maritime_domain="safety", meta={
                    "maritime_domain": "safety", "publication_status": "published",
                    "source_policy": "operator_published", "ais_nav_status_kind": "not_under_command",
                },
            ),
            IntelEventDB(
                id=aground_id, timestamp_utc=now.isoformat(), type="vessel_incident",
                severity="medium", lat=35.1, lon=14.1, title="Vessel ran aground — AUDIT",
                text="", url="", source="ais", linked_mmsi="209888002",
                maritime_domain="safety", meta={
                    "maritime_domain": "safety", "publication_status": "published",
                    "source_policy": "official_api", "verification_status": "ais_transponder",
                    "ais_nav_status_kind": "aground",
                },
            ),
            IntelEventDB(
                id=beacon_id, timestamp_utc=now.isoformat(), type="distress",
                severity="high", lat=35.2, lon=14.2, title="Distress beacon activated",
                text="", url="", source="ais_sart", linked_mmsi="",
                maritime_domain="safety", meta={
                    "maritime_domain": "safety", "publication_status": "published",
                    "source_policy": "official_api", "verification_status": "ais_transponder",
                },
            ),
        ])
    ids = {row["incident_id"] for row in TestClient(app).get("/api/v1/play/incidents?limit=500").json()["incidents"]}
    assert fusion_id not in ids
    assert aground_id not in ids
    assert beacon_id in ids


def test_play_catalog_collapses_near_simultaneous_alarm_phone_translation():
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    base = datetime.now(timezone.utc) - timedelta(hours=30)
    suffix = str(uuid.uuid4())
    older = f"audit-ap-en-{suffix}"
    newer = f"audit-ap-fr-{suffix}"
    for event_id, seconds, title in (
        (older, 0, "27 people left Boumerdes and are missing"),
        (newer, 33, "27 personnes parties de Boumerdes sont portees disparues"),
    ):
        event = IntelEvent(
            id=event_id, type="distress", severity="high", lat=36.79492, lon=3.5,
            title=title, text="", url="", source="alarm_phone", linked_mmsi="",
            timestamp_utc=(base + timedelta(seconds=seconds)).isoformat(),
            metadata={"publication_status": "published", "source_policy": "operator_published",
                      "is_distress": True, "maritime_domain": "sar"},
        )
        with session_scope() as db:
            db.add(IntelEventDB(
                id=event.id, timestamp_utc=event.timestamp_utc, type=event.type,
                severity=event.severity, lat=event.lat, lon=event.lon, title=event.title,
                text=event.text, url=event.url, source=event.source, linked_mmsi="",
                maritime_domain="sar", meta=event.metadata,
            ))
        sync_incident_for_event(event, lifecycle="active", case_type="distress")
    response = TestClient(app).get("/api/v1/play/incidents?limit=500")
    ids = [row["incident_id"] for row in response.json()["incidents"]]
    assert older in ids
    assert newer not in ids
