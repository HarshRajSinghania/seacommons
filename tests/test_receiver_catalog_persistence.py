from __future__ import annotations

from datetime import datetime, timezone

from core.radio.discovery import DiscoveredReceiver


def _candidate(endpoint: str = "http://malta.example:8073/") -> DiscoveredReceiver:
    return DiscoveredReceiver(
        public_label="Malta Central Med SDR",
        network_family="openwebrx",
        endpoint=endpoint,
        directory_source="receiverbook",
    )


def test_persist_discovered_receivers_is_idempotent_and_fail_closed():
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio.catalog_store import persist_discovered_receivers

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    row = _candidate()
    persist_discovered_receivers((row,))
    persist_discovered_receivers((row,))

    with session_scope() as db:
        rows = db.query(ReceiverCatalogDB).filter_by(discovery_key=row.discovery_key).all()
        assert len(rows) == 1
        saved = rows[0]
        assert saved.terms_status == "review_required"
        assert saved.activation_status == "catalogued"
        assert saved.endpoint == row.endpoint
        db.delete(saved)


def test_receiver_score_combines_geography_health_capability_and_openness():
    from core.radio.catalog_store import ReceiverScoreInput, calculate_receiver_score

    strong = calculate_receiver_score(ReceiverScoreInput(
        distance_km=120.0, reachable=True, supports_target=True,
        uptime_ratio=0.95, failure_rate=0.02, independent_lineage=True,
        license_class="open_source", available_slots=2,
    ))
    weak = calculate_receiver_score(ReceiverScoreInput(
        distance_km=1200.0, reachable=False, supports_target=True,
        uptime_ratio=0.2, failure_rate=0.8, independent_lineage=True,
        license_class="public_access", available_slots=0,
    ))
    assert 0 <= weak < strong <= 100


def test_recompute_receiver_state_promotes_only_terms_allowed_reachable_capable_rows():
    from core.radio.catalog_store import ReceiverComputedState, recompute_receiver_state

    allowed = recompute_receiver_state(
        terms_status="allowed", reachable=True, supports_target=True, score=82.0,
    )
    review = recompute_receiver_state(
        terms_status="review_required", reachable=True, supports_target=True, score=99.0,
    )
    offline = recompute_receiver_state(
        terms_status="allowed", reachable=False, supports_target=True, score=82.0,
    )
    assert allowed == ReceiverComputedState.ELIGIBLE
    assert review == ReceiverComputedState.REVIEW_REQUIRED
    assert offline == ReceiverComputedState.OFFLINE


def test_refresh_discovery_persists_candidates(monkeypatch):
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio import discovery

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    registry = discovery.DiscoveryRegistry()
    html = '''<div class="receiver-details"><h5>Test</h5><ul class="stationreceiverlist"><li>
    <div><a href="http://persist.example:8073/">Persist RX</a></div><div>OpenWebRX 1.2</div>
    </li></ul></div>'''
    discovery.refresh_discovery(
        registry=registry,
        fetch_text=lambda url: html if "receiverbook" in url else (_ for _ in ()).throw(OSError()),
    )
    key = discovery.parse_receiverbook_html(html)[0].discovery_key
    with session_scope() as db:
        row = db.query(ReceiverCatalogDB).filter_by(discovery_key=key).one()
        assert row.public_label == "Persist RX"
        db.delete(row)


def test_probe_catalog_updates_reachability_score_and_state():
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio.catalog_store import probe_and_score_catalog

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    key = "openwebrx:probe.example:8073"
    with session_scope() as db:
        db.merge(ReceiverCatalogDB(
            discovery_key=key, public_label="Probe RX", network_family="openwebrx",
            endpoint="http://probe.example:8073/", directory_source="test",
            terms_status="allowed", activation_status="catalogued",
            capabilities=[{"min_hz": 10000, "max_hz": 30000000}],
            lat=35.9, lon=14.4,
        ))
    summary = probe_and_score_catalog(
        zone="central_med", target_frequency_hz=2_187_500,
        probe=lambda _row: {"reachable": True, "available_slots": 2},
    )
    assert summary["eligible"] >= 1
    with session_scope() as db:
        row = db.query(ReceiverCatalogDB).filter_by(discovery_key=key).one()
        assert row.reachable is True
        assert row.activation_status == "eligible"
        assert row.score >= 50
        db.delete(row)


def test_persist_curated_catalog_preserves_terms_capabilities_and_location():
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio.catalog_store import persist_curated_catalog

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    count = persist_curated_catalog()
    assert count >= 16
    with session_scope() as db:
        row = db.query(ReceiverCatalogDB).filter_by(discovery_key="openwebrx:arascatania.ns0.it:8073").one()
        assert row.terms_status == "allowed"
        assert row.lat is not None and row.lon is not None
        assert row.capabilities
        assert row.directory_source == "curated_seed"
        db.delete(row)


def test_scheduler_receiver_catalog_recompute_runs_seed_and_probe(monkeypatch):
    from core import scheduler
    from core.radio import catalog_store

    calls = []
    monkeypatch.setattr(catalog_store, "persist_curated_catalog", lambda: calls.append("seed") or 16)
    monkeypatch.setattr(catalog_store, "probe_and_score_catalog", lambda: calls.append("probe") or {"eligible": 4})
    scheduler._job_receiver_catalog_recompute()
    assert calls == ["seed", "probe"]

    monkeypatch.setattr(catalog_store, "probe_and_score_catalog", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    scheduler._job_receiver_catalog_recompute()


def test_rank_persistent_catalog_returns_only_runtime_safe_eligible_descriptors():
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio.catalog_store import rank_persistent_catalog

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    key = "kiwisdr:rank.example:8073"
    with session_scope() as db:
        db.merge(ReceiverCatalogDB(
            discovery_key=key, receiver_id="rank_rx", public_label="Rank RX",
            network_family="kiwisdr", physical_lineage="rank_lineage",
            endpoint="http://rank.example:8073/", directory_source="test",
            license_class="public_access", source_terms="operator allowed",
            terms_status="allowed", activation_status="eligible", reachable=True,
            capabilities=[{"min_hz": 10000, "max_hz": 30000000, "modes": ["usb"]}],
            score=99.0,
        ))
    rows = rank_persistent_catalog(target_frequency_hz=2_187_500, mode="usb", limit=1, channel_kind="dsc")
    assert rows[0].receiver_id == "rank_rx"
    assert rows[0].channel_kind == "dsc"
    assert rows[0].physical_lineage == "rank_lineage"
    with session_scope() as db:
        db.query(ReceiverCatalogDB).filter_by(discovery_key=key).delete()


def test_public_catalog_summary_exposes_counts_without_private_endpoint():
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio.catalog_store import public_catalog_summary

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    key = "openwebrx:public-summary.example:8073"
    with session_scope() as db:
        db.merge(ReceiverCatalogDB(
            discovery_key=key, receiver_id="summary_rx", public_label="Summary RX",
            network_family="openwebrx", physical_lineage="summary_lineage",
            endpoint="http://public-summary.example:8073/", directory_source="test",
            license_class="open_source", source_terms="allowed", terms_status="allowed",
            activation_status="eligible", reachable=True, score=88.0,
            capabilities=[{"min_hz": 10000, "max_hz": 30000000, "modes": ["usb"]}],
        ))
    summary = public_catalog_summary(limit=5)
    assert summary["catalogued"] >= 1
    assert summary["reachable"] >= 1
    assert "endpoint" not in str(summary)
    assert "public-summary.example" not in str(summary)
    with session_scope() as db:
        db.query(ReceiverCatalogDB).filter_by(discovery_key=key).delete()


def test_live_receiver_mesh_endpoint_combines_catalog_and_active_runtime(monkeypatch):
    from fastapi.testclient import TestClient
    from core.api.main import app
    from core.radio import catalog_store, runtime

    monkeypatch.setattr(catalog_store, "public_catalog_summary", lambda limit=16: {
        "catalogued": 275, "reachable": 23, "review_required": 250,
        "eligible": 18, "offline": 7,
        "receivers": [{"receiver_id": "rx1", "station_label": "RX1", "network_family": "kiwisdr", "country": "IT", "state": "eligible", "score": 91.0}],
    })
    monkeypatch.setattr(runtime, "get_remote_radio_status", lambda include_receivers=False: {
        "receivers": [{"receiver_id": "rx1", "state": "connected"}],
    })
    response = TestClient(app).get("/api/v1/live/receivers/mesh?limit=8")
    assert response.status_code == 200
    payload = response.json()
    assert payload["catalogued"] == 275
    assert payload["active"] == 1
    assert payload["receivers"][0]["active"] is True
    assert "endpoint" not in str(payload)


def test_live_receiver_mesh_exposes_decoder_observability(monkeypatch):
    """docs section 8: "0 decoded DSC/NAVTEX" was previously indistinguishable
    from "decoder never ran" from the public API -- /receivers/mesh omitted
    every decoder field entirely. Reuses core.radio.bridge.radio_acquisition_
    status() rather than reimplementing it, so the two surfaces can't drift."""
    from fastapi.testclient import TestClient
    from core.api.main import app
    from core.radio import catalog_store, runtime

    monkeypatch.setattr(catalog_store, "public_catalog_summary", lambda limit=16: {
        "catalogued": 1, "reachable": 1, "review_required": 0,
        "eligible": 1, "offline": 0, "receivers": [],
    })
    monkeypatch.setattr(runtime, "get_remote_radio_status", lambda include_receivers=False: {
        "enabled": True, "started": 1, "failed": 0, "configured": 1,
        "receivers": [], "channels": [],
    })
    response = TestClient(app).get("/api/v1/live/receivers/mesh?limit=8")
    assert response.status_code == 200
    payload = response.json()
    assert "decoder" in payload
    for field in ("enabled", "decoders", "frames", "decoded", "invalid", "dropped", "errors", "queued", "worker_alive"):
        assert field in payload["decoder"], field
    assert "structured_enabled" in payload
    assert payload["structured_state"] in {"disabled", "offline", "idle", "degraded", "live"}
