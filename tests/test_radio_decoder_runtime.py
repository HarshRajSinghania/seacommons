from datetime import datetime, timezone


def _frame(payload=b"\x00\x00\x01\x00"):
    from core.radio.decoder_runtime import EphemeralRadioFrame
    return EphemeralRadioFrame(
        receiver_id="rx1", provider="kiwisdr", physical_lineage="lineage1",
        frequency_hz=2_187_500, mode="usb",
        observed_at=datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc),
        sample_rate_hz=12_000, encoding="kiwi_snd_raw", payload=payload,
        source_terms="allowed",
    )


def test_ephemeral_frame_is_bounded_and_never_serializes_payload_publicly():
    frame = _frame()
    assert frame.payload == b"\x00\x00\x01\x00"
    public = frame.public_metadata()
    assert public["receiver_id"] == "rx1"
    assert "payload" not in public
    assert "lineage1" not in str(public)


def test_oversized_ephemeral_frame_is_rejected():
    import pytest
    with pytest.raises(ValueError, match="payload"):
        _frame(b"x" * 262145)


def test_disabled_decoder_runtime_drops_ephemeral_frames():
    from core.radio.decoder_runtime import RadioDecoderRuntime
    calls = []
    runtime = RadioDecoderRuntime(enabled=False, decoded_handler=lambda msg: calls.append(msg))
    result = runtime.ingest_frame(_frame())
    assert result == {"accepted": False, "reason": "disabled"}
    assert calls == []


def test_decoder_output_routes_only_valid_structured_messages():
    from core.radio.decoder_runtime import RadioDecoderRuntime
    calls = []

    class FakeDecoder:
        def decode(self, frame):
            return ({"kind": "dsc", "payload": {"category": "distress", "mmsi": "123456789"}, "message_id": "m1"},)

    runtime = RadioDecoderRuntime(
        enabled=True, decoders=(FakeDecoder(),), decoded_handler=lambda msg: calls.append(msg) or {"accepted": True}
    )
    result = runtime.ingest_frame(_frame())
    assert result["decoded"] == 1
    assert calls[0].kind == "dsc"
    assert calls[0].provider_message_id == "m1"


def test_invalid_decoder_output_is_fail_closed():
    from core.radio.decoder_runtime import RadioDecoderRuntime
    class BadDecoder:
        def decode(self, frame):
            return ({"kind": "dsc", "payload": "raw audio"},)
    runtime = RadioDecoderRuntime(enabled=True, decoders=(BadDecoder(),), decoded_handler=lambda msg: {"accepted": True})
    result = runtime.ingest_frame(_frame())
    assert result["decoded"] == 0
    assert result["invalid"] == 1


def test_decoder_submit_queue_is_bounded_and_tracks_drops():
    import time

    from core.radio.decoder_runtime import RadioDecoderRuntime

    class SlowDecoder:
        def decode(self, frame):
            time.sleep(0.05)
            return ()

    runtime = RadioDecoderRuntime(
        enabled=True, decoders=(SlowDecoder(),), decoded_handler=lambda msg: {}, queue_size=1
    )
    runtime.start()
    accepted = [runtime.submit_frame(_frame(bytes([i % 255]))) for i in range(10)]
    runtime.stop()
    assert any(value is False for value in accepted)
    assert runtime.status()["dropped"] >= 1


def test_process_decoder_jsonl_protocol_returns_structured_output(tmp_path):
    import sys

    from core.radio.decoder_runtime import JSONLProcessDecoder

    script = tmp_path / "decoder.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " d=json.loads(line); print(json.dumps({'messages':[{'kind':'navtex','payload':'ZCZC KA01\\nTEST\\nNNNN','message_id':'n1'}]}), flush=True)\n"
    )
    decoder = JSONLProcessDecoder((sys.executable, str(script)), timeout_s=1.0)
    outputs = tuple(decoder.decode(_frame()))
    decoder.close()
    assert outputs[0]["kind"] == "navtex"
    assert outputs[0]["message_id"] == "n1"


def test_kiwi_audio_frame_taps_ephemeral_decoder_only_after_audio_init(monkeypatch):
    from core.radio import decoder_runtime
    from core.radio.kiwisdr import KiwiSDRAdapter

    from tests.test_kiwisdr_adapter import FakeKiwiTransport, _descriptor, _snd_frame

    frames = []
    monkeypatch.setattr(decoder_runtime, "submit_ephemeral_frame", lambda frame: frames.append(frame) or True)
    transport = FakeKiwiTransport()
    adapter = KiwiSDRAdapter(_descriptor(), on_observation=lambda obs: None, transport=transport)
    adapter.start(); adapter.tune(2_187_500, "usb")
    transport.emit(_snd_frame(audio=b"before"))
    assert frames == []
    transport.emit(b"MSG audio_init=0 audio_rate=12000")
    transport.emit(_snd_frame(audio=b"after"))
    assert frames[-1].payload == b"after"
    assert frames[-1].sample_rate_hz == 12000
    assert frames[-1].encoding == "kiwi_snd_raw"


def test_openwebrx_audio_binary_taps_ephemeral_decoder(monkeypatch):
    from core.radio import decoder_runtime
    from core.radio.openwebrx import OpenWebRXAdapter

    from tests.test_openwebrx_adapter import (
        FakeTransport,
        descriptor,
    )

    frames = []
    monkeypatch.setattr(decoder_runtime, "submit_ephemeral_frame", lambda frame: frames.append(frame) or True)
    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=lambda obs: None, transport=transport)
    adapter.start(); transport.on_message({"type": "config", "value": {"center_freq": 2_200_000, "samp_rate": 200_000, "audio_compression": "none"}})
    adapter.tune(2_187_500, "usb")
    transport.on_message(b"\x02payload")
    assert frames[-1].payload == b"payload"
    assert frames[-1].sample_rate_hz == 12000
    assert frames[-1].encoding == "openwebrx_pcm_s16le"


def test_openwebrx_compressed_audio_is_not_forwarded_to_pcm_decoder(monkeypatch):
    from core.radio import decoder_runtime
    from core.radio.openwebrx import OpenWebRXAdapter

    from tests.test_openwebrx_adapter import FakeTransport, descriptor

    frames = []
    monkeypatch.setattr(decoder_runtime, "submit_ephemeral_frame", lambda frame: frames.append(frame) or True)
    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=lambda obs: None, transport=transport)
    adapter.start()
    transport.on_message({"type": "config", "value": {"center_freq": 2_200_000, "samp_rate": 200_000, "audio_compression": "adpcm"}})
    adapter.tune(2_187_500, "usb")
    transport.on_message(b"\x02compressed")
    assert frames == []


def test_decoder_worker_survives_downstream_handler_error():
    import time

    from core.radio.decoder_runtime import RadioDecoderRuntime

    class FakeDecoder:
        def decode(self, frame):
            return ({
                "kind": "dsc",
                "payload": {"category": "routine"},
                "message_id": "m1",
            },)

    runtime = RadioDecoderRuntime(
        enabled=True,
        decoders=(FakeDecoder(),),
        decoded_handler=lambda _msg: (_ for _ in ()).throw(RuntimeError("persist failed")),
        queue_size=2,
    )
    runtime.start()
    assert runtime.submit_frame(_frame()) is True
    deadline = time.time() + 1.0
    while runtime.status()["frames"] < 1 and time.time() < deadline:
        time.sleep(0.01)
    status = runtime.status()
    assert status["worker_alive"] is True
    assert status["errors"] >= 1
    assert status["decoded"] == 0
    runtime.stop()
