from __future__ import annotations

from datetime import datetime

import pytest
from core.radio.provider import ReceiverCapability
from core.radio.registry import ReceiverDescriptor, ReceiverRegistry


class FakeTransport:
    def __init__(self, *, fail_start: bool = False):
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self.websocket_url = ""
        self.texts: list[str] = []
        self.controls: list[dict[str, object]] = []
        self.on_message = None
        self.on_disconnect = None

    def start(self, *, websocket_url, timeout_s, on_message, on_disconnect):
        if self.fail_start:
            raise OSError("offline")
        self.started = True
        self.websocket_url = websocket_url
        self.on_message = on_message
        self.on_disconnect = on_disconnect

    def send_text(self, message):
        self.texts.append(str(message))

    def send_control(self, payload):
        self.controls.append(dict(payload))

    def stop(self):
        self.stopped = True


def descriptor(*, frontend_url="https://rx.example.org/", physical_lineage="med-rx-01"):
    return ReceiverDescriptor(
        receiver_id="openwebrx_med_rx",
        provider="openwebrx",
        frontend_url=frontend_url,
        physical_lineage=physical_lineage,
        enabled=True,
        terms_status="allowed",
        source_terms="operator-permission",
        capabilities=(ReceiverCapability(100_000, 180_000_000, ("am", "usb", "lsb", "nbfm")),),
    )


def _announce_profile(transport, *, center=156_500_000, sample_rate=2_000_000):
    assert transport.on_message is not None
    transport.on_message({"type": "config", "value": {"center_freq": center, "samp_rate": sample_rate}})


def test_start_uses_native_handshake_connectionproperties_and_ws_endpoint():
    from core.radio.openwebrx import OpenWebRXAdapter

    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=lambda _obs: None, transport=transport)
    adapter.start()

    assert transport.websocket_url == "wss://rx.example.org/ws/"
    assert transport.texts == ["SERVER DE CLIENT client=seacommons type=receiver"]
    assert transport.controls == [
        {"type": "connectionproperties", "params": {"output_rate": 12000, "hd_output_rate": 48000}}
    ]
    assert adapter.health().connected is True
    adapter.stop()
    assert transport.stopped is True
    assert adapter.health().connected is False


def test_tune_uses_native_dspcontrol_and_active_profile_bounds():
    from core.radio.openwebrx import OpenWebRXAdapter

    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=lambda _obs: None, transport=transport)
    adapter.start()

    with pytest.raises(ValueError):
        adapter.tune(190_000_000, "nbfm")
    adapter.tune(156_800_000, "nbfm")

    _announce_profile(transport)
    assert transport.controls[-2:] == [
        {"type": "dspcontrol", "action": "start"},
        {"type": "dspcontrol", "params": {"offset_freq": 300_000, "mod": "nfm"}},
    ]

    before = list(transport.controls)
    with pytest.raises(ValueError, match="active OpenWebRX profile"):
        adapter.tune(158_000_000, "nbfm")
    assert transport.controls == before


def test_smeter_message_normalizes_dbfs_and_discards_binary_audio():
    from core.radio.openwebrx import OpenWebRXAdapter

    observations = []
    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=observations.append, transport=transport)
    adapter.start()
    _announce_profile(transport)
    adapter.tune(156_800_000, "nbfm")

    assert transport.on_message is not None
    transport.on_message({"type": "smeter", "value": 1e-8})
    transport.on_message(b"pretend-audio-bytes")

    assert len(observations) == 1
    obs = observations[0]
    assert obs.receiver_id == "openwebrx_med_rx"
    assert obs.provider == "openwebrx"
    assert obs.physical_lineage == "med_rx_01"
    assert obs.frequency_hz == 156_800_000
    assert obs.mode == "nbfm"
    assert obs.signal_dbm is None
    assert obs.signal_dbfs == -80.0
    assert obs.snr_db is None
    assert obs.source_terms == "operator-permission"


def test_disconnect_and_start_failure_fail_closed():
    from core.radio.openwebrx import OpenWebRXAdapter

    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=lambda _obs: None, transport=transport)
    adapter.start()
    assert transport.on_disconnect is not None
    transport.on_disconnect("anything")
    assert adapter.health().connected is False
    assert adapter.health().error == "transport_disconnected"

    failing = OpenWebRXAdapter(
        descriptor(), on_observation=lambda _obs: None, transport=FakeTransport(fail_start=True)
    )
    with pytest.raises(RuntimeError, match="OpenWebRX connection failed"):
        failing.start()
    assert failing.health().connected is False
    assert failing.health().error == "connect_failed"


def test_openwebrx_and_kiwi_frontends_same_physical_receiver_are_one_runnable_lineage():
    kiwi = ReceiverDescriptor(
        receiver_id="kiwi_frontend",
        provider="kiwisdr",
        frontend_url="https://kiwi.example.org",
        physical_lineage="shared-rx-01",
        enabled=True,
        terms_status="allowed",
        source_terms="ok",
        capabilities=(ReceiverCapability(100_000, 30_000_000, ("am", "usb", "lsb")),),
    )
    owrx = descriptor(frontend_url="https://owrx.example.org", physical_lineage="shared-rx-01")

    registry = ReceiverRegistry((kiwi, owrx), max_receivers=10)
    runnable = registry.runnable()

    assert len(runnable) == 1
    assert runnable[0].physical_lineage == "shared_rx_01"


def test_health_counts_only_smeter_observations():
    from core.radio.openwebrx import OpenWebRXAdapter

    observations = []
    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=observations.append, transport=transport)
    adapter.start()
    _announce_profile(transport, center=1_000_000, sample_rate=1_000_000)
    adapter.tune(1_000_000, "am")
    assert transport.on_message is not None
    transport.on_message({"type": "clients", "value": 4})
    transport.on_message({"type": "smeter", "value": 1e-10})

    assert len(observations) == 1
    assert observations[0].observed_at.tzinfo is not None
    assert adapter.health().observations_received == 1
    assert isinstance(adapter.health().last_message_at, datetime)


def test_tune_requested_before_profile_metadata_is_applied_when_config_arrives():
    from core.radio.openwebrx import OpenWebRXAdapter

    transport = FakeTransport()
    adapter = OpenWebRXAdapter(descriptor(), on_observation=lambda observation: None, transport=transport)
    adapter.start()
    adapter.tune(2_187_500, "usb")
    assert not any(payload.get("type") == "dspcontrol" for payload in transport.controls)

    transport.on_message({"type": "config", "value": {"center_freq": 2_200_000, "samp_rate": 200_000}})
    assert any(payload.get("type") == "dspcontrol" and payload.get("action") == "start" for payload in transport.controls)
    # HF DSC USB transport tunes 1700 Hz below the assigned channel so
    # the decoder receives the standard 1615/1785 Hz audio pair.
    assert any(payload.get("params", {}).get("offset_freq") == -14_200 for payload in transport.controls)
