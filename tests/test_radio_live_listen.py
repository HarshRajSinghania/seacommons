from datetime import datetime, timezone


def _frame(payload=b"\x00\x00\x7f\xff"):
    from core.radio.decoder_runtime import EphemeralRadioFrame
    return EphemeralRadioFrame(
        receiver_id="rx-listen", provider="kiwisdr", physical_lineage="lineage-listen",
        frequency_hz=2_187_500, mode="usb", observed_at=datetime.now(timezone.utc),
        sample_rate_hz=12_000, encoding="kiwi_snd_raw", payload=payload,
        source_terms="allowed",
    )


def test_listen_broker_does_no_work_without_subscribers():
    from core.radio.listen import EphemeralAudioBroker
    broker = EphemeralAudioBroker(max_subscribers=2, queue_size=2)
    assert broker.publish(_frame()) == 0
    assert broker.status()["published_frames"] == 0


def test_listen_broker_streams_pcm_only_to_subscribed_receiver():
    from core.radio.listen import EphemeralAudioBroker
    broker = EphemeralAudioBroker(max_subscribers=2, queue_size=2)
    subscription = broker.subscribe("rx-listen")
    assert broker.publish(_frame()) == 1
    assert subscription.queue.get_nowait() == b"\x00\x00\xff\x7f"
    broker.unsubscribe(subscription)


def test_listen_broker_is_bounded_and_counts_drops():
    from core.radio.listen import EphemeralAudioBroker
    broker = EphemeralAudioBroker(max_subscribers=1, queue_size=1)
    subscription = broker.subscribe("rx-listen")
    assert broker.publish(_frame(b"\x01\x00")) == 1
    assert broker.publish(_frame(b"\x02\x00")) == 0
    assert broker.status()["dropped_frames"] == 1
    broker.unsubscribe(subscription)


def test_only_active_terms_allowed_receivers_are_listen_eligible(monkeypatch):
    from core.radio import listen
    rows = [
        type("R", (), {"receiver_id": "allowed", "terms_status": "allowed", "network_family": "kiwisdr"})(),
        type("R", (), {"receiver_id": "review", "terms_status": "review_required", "network_family": "openwebrx"})(),
        type("R", (), {"receiver_id": "other", "terms_status": "allowed", "network_family": "unsupported"})(),
    ]
    monkeypatch.setattr(listen, "_catalog_rows_for_receiver_ids", lambda ids: rows)
    assert listen.listen_eligible_receiver_ids({"allowed", "review", "other"}) == {"allowed"}


def test_submit_ephemeral_frame_fans_out_listen_even_if_decoder_disabled(monkeypatch):
    from core.radio import decoder_runtime
    published = []
    class DisabledRuntime:
        def submit_frame(self, frame):
            return False
    monkeypatch.setattr(decoder_runtime, "get_radio_decoder_runtime", lambda: DisabledRuntime())
    monkeypatch.setattr("core.radio.listen.publish_ephemeral_audio", lambda frame: published.append(frame) or 1)
    assert decoder_runtime.submit_ephemeral_frame(_frame()) is True
    assert len(published) == 1


def test_listen_websocket_is_fail_closed_when_feature_disabled(monkeypatch):
    from core.api.main import app
    from core.config import config
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_ENABLED", False)
    with TestClient(app) as test_client:
        try:
            with test_client.websocket_connect("/api/v1/live/radio/listen/rx-listen"):
                raise AssertionError("disabled listen websocket unexpectedly connected")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_listen_websocket_advertises_ephemeral_pcm_format(monkeypatch):
    from core.api.main import app
    from core.config import config
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_ENABLED", True)
    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_MAX_SECONDS", 1)
    monkeypatch.setattr("core.radio.runtime.get_remote_radio_status", lambda include_receivers=False: {
        "receivers": [{"receiver_id": "rx-listen", "state": "connected"}],
    })
    monkeypatch.setattr("core.radio.listen.listen_eligible_receiver_ids", lambda ids: {"rx-listen"})
    with (
        TestClient(app) as test_client,
        test_client.websocket_connect("/api/v1/live/radio/listen/rx-listen") as websocket,
    ):
        message = websocket.receive_json()

    assert message == {
        "type": "audio_format",
        "encoding": "pcm_s16le",
        "sample_rate_hz": 12_000,
        "channels": 1,
        "persistent": False,
        "max_seconds": 1,
    }


def test_listen_websocket_streams_broker_pcm_bytes(monkeypatch):
    from core.api.main import app
    from core.config import config
    from core.radio.listen import listen_broker
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_ENABLED", True)
    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_MAX_SECONDS", 1)
    monkeypatch.setattr("core.radio.runtime.get_remote_radio_status", lambda include_receivers=False: {
        "receivers": [{"receiver_id": "rx-listen", "state": "connected"}],
    })
    monkeypatch.setattr("core.radio.listen.listen_eligible_receiver_ids", lambda ids: {"rx-listen"})

    with (
        TestClient(app) as test_client,
        test_client.websocket_connect("/api/v1/live/radio/listen/rx-listen") as websocket,
    ):
        websocket.receive_json()
        packet = b"\x01\x00\xff\x7f"
        assert listen_broker.publish(_frame(packet)) == 1
        assert websocket.receive_bytes() == b"\x00\x01\x7f\xff"



def test_listen_http_streams_ephemeral_pcm_without_cloudflare(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from core.api.main import app
    from core.config import config
    from core.radio.listen import listen_broker
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_ENABLED", True)
    monkeypatch.setattr("core.radio.runtime.get_remote_radio_status", lambda include_receivers=False: {
        "receivers": [{"receiver_id": "rx-listen", "state": "connected"}],
    })
    monkeypatch.setattr("core.radio.listen.listen_eligible_receiver_ids", lambda ids: {"rx-listen"})

    with TestClient(app) as test_client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: test_client.get(
            "/api/v1/live/radio/listen/rx-listen?stream_seconds=1"
        ))
        deadline = time.monotonic() + 1.0
        while listen_broker.status()["subscribers"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert listen_broker.status()["subscribers"] == 1
        packet = b"\x01\x00\xff\x7f"
        assert listen_broker.publish(_frame(packet)) == 1
        response = future.result(timeout=10)

    assert response.status_code == 200
    assert response.content == b"\x00\x01\x7f\xff"
    assert response.headers["content-type"].startswith("audio/L16")
    assert response.headers["x-seacommons-audio-encoding"] == "pcm_s16le"
    assert response.headers["x-seacommons-sample-rate"] == "12000"
    assert response.headers["cache-control"] == "no-store"

def test_receiver_mesh_advertises_listen_capability_without_receiver_endpoint(monkeypatch):
    from core.api.main import app
    from core.config import config
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "RADIO_LIVE_LISTEN_ENABLED", True)
    monkeypatch.setattr("core.radio.catalog_store.public_catalog_summary", lambda limit=16: {
        "catalogued": 1, "reachable": 1, "eligible": 1, "offline": 0,
        "receivers": [{
            "receiver_id": "rx-listen", "station_label": "Public RX",
            "network_family": "kiwisdr", "country": "IT", "state": "eligible", "score": 94.0,
        }],
    })
    monkeypatch.setattr("core.radio.runtime.get_remote_radio_status", lambda include_receivers=False: {
        "receivers": [{
            "receiver_id": "rx-listen", "state": "connected",
            "frequency_hz": 2_187_500, "mode": "usb",
        }],
    })
    monkeypatch.setattr("core.radio.listen.listen_eligible_receiver_ids", lambda ids: {"rx-listen"})

    payload = TestClient(app).get("/api/v1/live/receivers/mesh?limit=8").json()
    receiver = payload["receivers"][0]
    assert receiver["listen_available"] is True
    assert receiver["frequency_hz"] == 2_187_500
    assert receiver["mode"] == "usb"
    assert "endpoint" not in str(payload).lower()


def test_listen_eligibility_survives_catalog_session_close():
    from core.db.models import ReceiverCatalogDB
    from core.db.session import engine, session_scope
    from core.radio.listen import listen_eligible_receiver_ids

    ReceiverCatalogDB.__table__.create(bind=engine(), checkfirst=True)
    key = "kiwisdr:listen-session.example:8073"
    receiver_id = "rx-listen-session"
    with session_scope() as db:
        db.merge(ReceiverCatalogDB(
            discovery_key=key, receiver_id=receiver_id, public_label="Listen Session RX",
            network_family="kiwisdr", endpoint="http://listen-session.example:8073/",
            directory_source="test", terms_status="allowed", activation_status="eligible",
            reachable=True,
        ))
    try:
        assert listen_eligible_receiver_ids({receiver_id}) == {receiver_id}
    finally:
        with session_scope() as db:
            db.query(ReceiverCatalogDB).filter_by(discovery_key=key).delete()
