from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core.live_edge_publisher import (
    LiveEdgePublisher,
    Outbox,
    PublisherSettings,
    public_event_from_row,
    removed_payload,
    signature,
)

_NOW = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)


def distress_row(*, timestamp_utc="2026-08-02T12:00:00+00:00", text="Public source report", **meta_overrides):
    meta = {
        "is_distress": True,
        "confidence": 0.72,
        "location_uncertainty_m": 5000,
        # A real tracked-account distress event carries an explicit publication
        # decision -- the edge now applies the exact VM eligibility rule, which
        # requires one (docs/fixes.md F-06).
        "source_policy": "operator_published",
        "publication_status": "published",
    }
    meta.update(meta_overrides)
    return SimpleNamespace(
        id="evt-1",
        type="distress",
        severity="critical",
        lat=35.1,
        lon=14.2,
        title="Boat in distress",
        text=text,
        url="https://example.test/report",
        source="alarm_phone",
        linked_mmsi="",
        timestamp_utc=timestamp_utc,
        meta=meta,
    )


def test_public_distress_event_mapping_is_versioned() -> None:
    event = public_event_from_row(distress_row(), "oracle-collector-1", now=_NOW, same_source=[])

    assert event is not None
    assert event["id"].startswith("evt-1:")
    assert event["visibility"] == "public"
    assert event["type"] == "distress_observation"
    assert event["geometry"]["coordinates"] == [14.2, 35.1]
    assert event["properties"]["incident_id"] == "evt-1"
    assert event["properties"]["radius_m"] == 5000
    assert event["properties"]["incident_lifecycle"] == "active"
    assert event["properties"]["live_contract_version"] == 2


def test_humanitarian_edge_accepts_any_verified_humanitarian_source() -> None:
    """docs/fixes.md F-06: Humanitarian Live is domain + policy based, not
    Alarm-Phone-only. A published distress report from any tracked NGO reaches
    the edge exactly as it reaches the VM's mode=humanitarian feed."""
    row = distress_row()
    row.source = "sea_watch"
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])
    assert event is not None
    assert event["properties"]["incident_lifecycle"] == "active"


def test_maritime_security_domain_never_reaches_the_humanitarian_edge() -> None:
    row = distress_row(maritime_domain="sanctions")
    assert public_event_from_row(row, "node", now=_NOW, same_source=[]) is None


def test_maritime_safety_domain_never_reaches_the_humanitarian_edge() -> None:
    """docs/fixes.md P0.1: Maritime Safety (NUC/aground/restricted
    manoeuvrability) became its own VM mode (mode=safety), distinct from
    mode=humanitarian -- DEFAULT_PUBLIC_MARITIME_DOMAINS now includes
    "safety" for that VM mode's own domain filter, which would otherwise
    also let it leak into the edge's humanitarian-only feed and break
    VM/edge parity. The edge has no separate safety mode (yet); it must
    stay excluded here, same as security."""
    row = distress_row(
        maritime_domain="safety", ais_nav_status_kind="not_under_command", is_distress=False
    )
    assert public_event_from_row(row, "node", now=_NOW, same_source=[]) is None


def test_thread_reposts_and_repost_count_reach_the_edge_payload() -> None:
    # Without this the public Live host's "Updates" panel is silently empty
    # for every event, since the edge (tried first there, ahead of the VM's
    # own API) never carried this data at all -- a self-reply was correctly
    # threaded on the VM/DB side but had no way to reach the public map.
    row = distress_row(
        repost_count=1,
        thread_reposts=[{
            "tweet_id": "999", "posted_at": "2026-08-02T12:10:00+00:00",
            "url": "https://x.com/i/web/status/999", "kind": "reply",
            "note": "Rescued, all safe.",
            # Fields that must never leak: not part of the public contract.
            "internal_debug": "should not appear",
        }],
    )
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])

    assert event is not None
    assert event["properties"]["repost_count"] == 1
    reposts = event["properties"]["thread_reposts"]
    assert len(reposts) == 1
    assert reposts[0] == {
        "tweet_id": "999", "posted_at": "2026-08-02T12:10:00+00:00",
        "url": "https://x.com/i/web/status/999", "kind": "reply",
        "note": "Rescued, all safe.",
    }


def test_area_geojson_becomes_the_geometry_on_the_edge_too() -> None:
    # Same class of bug as the thread_reposts test above: a report with only
    # an area (no single defensible point) must show as an area on the
    # public Live host too, not silently fall back to a point because this
    # path forgot about area_geojson.
    polygon = {"type": "Polygon", "coordinates": [[[14.0, 35.0], [14.1, 35.0], [14.1, 35.1], [14.0, 35.0]]]}
    row = distress_row(area_geojson=polygon, area_confidence="area_low_confidence")
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])

    assert event is not None
    assert event["geometry"] == polygon
    assert event["properties"]["location_precision"] == "area_low_confidence"


def test_material_update_gets_new_version_id() -> None:
    first = public_event_from_row(distress_row(), "node", now=_NOW, same_source=[])
    second = public_event_from_row(distress_row(confidence=0.9), "node", now=_NOW, same_source=[])

    assert first is not None and second is not None
    assert first["id"] != second["id"]
    assert first["properties"]["incident_id"] == second["properties"]["incident_id"]


def test_concluded_report_is_resolved_and_removed_from_operational_live() -> None:
    # Lifecycle remains explicit for archive consumers while the removal
    # version clears the incident from operational edge state.
    row = distress_row(text="Rescued!! Thank you Ocean Viking for rescuing the 14 people")
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])

    assert event is not None
    assert event["properties"]["incident_lifecycle"] == "resolved"
    assert event["type"] == "incident_removed"
    assert event["properties"]["expired"] is True


def test_saved_arrival_reply_resolves_on_the_edge() -> None:
    row = distress_row(thread_reposts=[{
        "tweet_id": "2085235676618846249",
        "posted_at": "2026-08-02T12:10:00+00:00",
        "kind": "reply",
        "note": "We received news that the people arrived on Sicily!",
    }])
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])
    assert event is not None
    assert event["properties"]["incident_lifecycle"] == "resolved"
    assert event["type"] == "incident_removed"


def test_unsafe_rescue_reply_stays_active_on_the_edge() -> None:
    row = distress_row(thread_reposts=[{
        "tweet_id": "2084583427207057896",
        "posted_at": "2026-08-02T12:10:00+00:00",
        "kind": "reply",
        "note": (
            "A vessel rescued the people but they need to be disembarked in a country "
            "of safety, which Egypt is not!"
        ),
    }])
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])
    assert event is not None
    assert event["properties"]["incident_lifecycle"] == "active"


def test_stale_unresolved_report_leaves_live_after_24_hours() -> None:
    old_timestamp = (_NOW - timedelta(hours=30)).isoformat()
    row = distress_row(timestamp_utc=old_timestamp)
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])

    assert event is not None
    assert event["properties"]["incident_status"] == "outcome_unknown"
    assert event["type"] == "incident_removed"
    assert event["properties"]["expired"] is True


def test_event_past_the_live_window_is_marked_for_removal() -> None:
    old_timestamp = (_NOW - timedelta(days=8)).isoformat()
    row = distress_row(timestamp_utc=old_timestamp)
    event = public_event_from_row(row, "node", now=_NOW, same_source=[])

    assert event is not None
    assert event["type"] == "incident_removed"
    assert event["properties"]["expired"] is True


def test_blocked_source_policy_never_reaches_the_edge_even_if_flagged_distress() -> None:
    # Same guarantee as core/api/routes/live.py's blocklist: a legacy scraper
    # record or an "unofficial" transport must never surface on the public
    # map, even if is_distress/publication_status say otherwise. This is the
    # primary live.seacommons.org path, so it needs the same guarantee the
    # VM standby path already has.
    for overrides in (
        {"source_policy": "unofficial"},
        {"via": "nitter"},
        {"scrape_source": "twscrape-mirror"},
    ):
        row = distress_row(**overrides)
        assert public_event_from_row(row, "node", now=_NOW, same_source=[]) is None


def test_row_without_distress_flag_or_explicit_publication_is_not_exported() -> None:
    # Mirrors the VM path: a bare row with no is_distress flag, a non-distress
    # type, and no explicit publication decision must not leak onto the
    # public map.
    row = distress_row(is_distress=False)
    row.type = "news"
    assert public_event_from_row(row, "node", now=_NOW, same_source=[]) is None


def test_explicit_publication_without_is_distress_flag_is_exported() -> None:
    # The publication_status=="published" branch (fixed from a "publication_state"
    # typo that was never set by any producer and was therefore always False)
    # must actually work: an explicitly published non-distress row still needs
    # a path onto the public map.
    row = distress_row(is_distress=False, publication_status="published")
    row.type = "ngo_activity"
    assert public_event_from_row(row, "node", now=_NOW, same_source=[]) is not None


def test_edge_and_vm_agree_on_the_humanitarian_incident_set() -> None:
    """docs/fixes.md F-06 acceptance proof: for a fixed set of events, the
    humanitarian incident IDs the edge publishes == the ones the VM's
    mode=humanitarian feed returns. Neither path may apply its own source
    policy."""
    from core.intel.store import IntelEvent, intel_store
    from core.live.feed import public_signal_collection

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    events = [
        IntelEvent(
            id="par-ap", type="twitter", severity="critical", lat=35.0, lon=14.0,
            title="Alarm Phone distress", text="Boat in distress, 30 people",
            source="Alarm Phone", timestamp_utc=recent,
            metadata={"is_distress": True, "source_policy": "operator_published",
                      "publication_status": "published", "tracked_account": "alarm_phone"},
        ),
        IntelEvent(
            id="par-sw", type="twitter", severity="high", lat=34.5, lon=13.0,
            title="Sea-Watch distress", text="Sighted an overcrowded boat",
            source="Sea-Watch", timestamp_utc=recent,
            metadata={"is_distress": True, "source_policy": "operator_published",
                      "publication_status": "published", "tracked_account": "seawatch"},
        ),
        IntelEvent(
            id="par-sec", type="ais_anomaly", severity="high", lat=35.2, lon=14.2,
            title="Sanctioned vessel", source="SeaCommons MDA",
            timestamp_utc=recent,
            metadata={"anomaly_type": "sanctioned_vessel", "source_policy": "official_api"},
        ),
        IntelEvent(
            id="par-priv", type="twitter", severity="high", lat=33.0, lon=12.0,
            title="Private caller", text="distress", source="whatsapp",
            timestamp_utc=recent,
            metadata={"is_distress": True, "publication_status": "private"},
        ),
    ]
    for event in events:
        intel_store.add(event)
    try:
        vm = public_signal_collection(mode="humanitarian", days=30)
        vm_ids = {
            ident
            for f in vm["features"]
            if (ident := str(f["properties"]["id"]).removeprefix("intel:")).startswith("par-")
        }

        edge_ids = set()
        for event in events:
            row = SimpleNamespace(
                id=event.id, type=event.type, severity=event.severity,
                lat=event.lat, lon=event.lon, title=event.title, text=event.text,
                url="", source=event.source, linked_mmsi="",
                timestamp_utc=event.timestamp_utc, meta=event.metadata,
            )
            payload = public_event_from_row(row, "node", now=now, same_source=[])
            if payload is not None and not payload["properties"].get("expired"):
                edge_ids.add(payload["properties"]["incident_id"])

        assert vm_ids == edge_ids
        assert "par-ap" in vm_ids and "par-sw" in vm_ids
        assert "par-sec" not in vm_ids and "par-priv" not in vm_ids
    finally:
        with intel_store._lock:
            intel_store._events = type(intel_store._events)(
                (e for e in intel_store._events if not e.id.startswith("par-")),
                maxlen=intel_store._events.maxlen,
            )


def test_non_public_context_event_is_not_exported() -> None:
    row = SimpleNamespace(
        id="evt-2",
        type="news",
        severity="low",
        lat=None,
        lon=None,
        title="Context",
        text="Not explicitly published",
        url="",
        source="news",
        linked_mmsi="",
        timestamp_utc="2026-08-02T12:00:00+00:00",
        meta={},
    )

    assert public_event_from_row(row, "node", now=_NOW, same_source=[]) is None


def test_outbox_survives_reopen_and_remembers_delivery(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    first = Outbox(path)
    assert first.enqueue("evt-3:v1", {"id": "evt-3:v1", "type": "distress_observation"})

    second = Outbox(path)
    assert second.counts()["pending"] == 1
    assert second.ready(10)[0]["event_id"] == "evt-3:v1"

    second.acknowledge("evt-3:v1")
    third = Outbox(path)
    assert third.counts()["pending"] == 0
    assert third.enqueue("evt-3:v1", {"id": "evt-3:v1"}) is False


def test_was_ever_delivered_tracks_by_incident_not_exact_version(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.db")
    assert outbox.was_ever_delivered("evt-9") is False

    outbox.enqueue("evt-9:v1", {"id": "evt-9:v1"})
    assert outbox.was_ever_delivered("evt-9") is False  # pending, not yet delivered

    outbox.acknowledge("evt-9:v1")
    assert outbox.was_ever_delivered("evt-9") is True
    # A later, different version of the SAME incident still counts.
    assert outbox.was_ever_delivered("evt-9") is True
    # An unrelated incident with a similar-looking id must not false-match.
    assert outbox.was_ever_delivered("evt-9-other") is False


def test_removed_payload_is_a_valid_incident_removed_event() -> None:
    payload = removed_payload("evt-10", "node-a", source="alarm_phone")
    assert payload["type"] == "incident_removed"
    assert payload["properties"]["incident_id"] == "evt-10"
    assert payload["geometry"] is None
    assert payload["properties"]["live_contract_version"] == 2
    assert payload["id"].startswith("evt-10:")


def test_signature_is_stable() -> None:
    assert signature("secret", '{"id":"evt"}') == signature("secret", '{"id":"evt"}')
    assert signature("secret", '{"id":"evt"}') != signature("other", '{"id":"evt"}')


def test_publisher_heartbeat_is_separate_from_event_delivery(tmp_path: Path) -> None:
    settings = PublisherSettings(
        edge_url="https://edge.example/v1/live/events",
        ingest_secret="secret",
        node_id="collector-a",
        outbox_path=tmp_path / "outbox.db",
    )
    publisher = LiveEdgePublisher(settings)
    calls = []

    def post(url, *, content, headers):
        calls.append((url, content, headers))
        return SimpleNamespace(status_code=202, text="")

    publisher.client.post = post
    assert publisher.heartbeat(force=True) is True
    assert calls[0][0] == "https://edge.example/v1/live/heartbeat"
    body = calls[0][1]
    assert '"source":"live-edge-publisher"' in body
    assert calls[0][2]["X-SeaCommons-Signature"] == signature("secret", body)


def _publisher(tmp_path: Path) -> LiveEdgePublisher:
    return LiveEdgePublisher(
        PublisherSettings(
            edge_url="https://edge.example/v1/live/events",
            ingest_secret="secret",
            node_id="collector-a",
            outbox_path=tmp_path / "outbox.db",
        )
    )


def test_delivery_and_heartbeat_outcomes_are_exported_as_metrics(tmp_path: Path) -> None:
    from prometheus_client import REGISTRY

    def sample(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    publisher = _publisher(tmp_path)
    publisher.outbox.enqueue("evt-1:v1", {"id": "evt-1:v1", "type": "Feature"})

    before_ok = sample("seacommons_live_publish_events_total", stage="delivered")
    publisher.client.post = lambda url, *, content, headers: SimpleNamespace(status_code=202, text="")
    assert publisher.deliver() == 1
    assert sample("seacommons_live_publish_events_total", stage="delivered") == before_ok + 1
    assert sample("seacommons_live_publish_last_delivery_unixtime") > 0

    publisher.outbox.enqueue("evt-2:v1", {"id": "evt-2:v1", "type": "Feature"})
    before_fail = sample("seacommons_live_publish_events_total", stage="delivery_failed")
    publisher.client.post = lambda url, *, content, headers: SimpleNamespace(status_code=500, text="boom")
    assert publisher.deliver() == 0
    assert sample("seacommons_live_publish_events_total", stage="delivery_failed") == before_fail + 1

    publisher.client.post = lambda url, *, content, headers: SimpleNamespace(status_code=502, text="down")
    assert publisher.heartbeat(force=True) is False
    assert sample("seacommons_live_edge_heartbeat_ok") == 0.0


def test_outbox_depth_gauge_tracks_pending_and_retrying(tmp_path: Path) -> None:
    from core.observability import record_publisher_cycle
    from prometheus_client import REGISTRY

    outbox = _publisher(tmp_path).outbox
    outbox.enqueue("evt-3:v1", {"id": "evt-3:v1"})
    outbox.enqueue("evt-4:v1", {"id": "evt-4:v1"})
    outbox.fail("evt-4:v1", 1, "temporary", 20)

    record_publisher_cycle(outcome="ok", collected=2, outbox_counts=outbox.counts())

    assert REGISTRY.get_sample_value(
        "seacommons_live_outbox_depth", {"state": "pending"}
    ) == 2
    assert REGISTRY.get_sample_value(
        "seacommons_live_outbox_depth", {"state": "retrying"}
    ) == 1
