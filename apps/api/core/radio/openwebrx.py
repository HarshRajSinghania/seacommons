# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from core.radio.provider import ObservationCallback, RadioObservation, RemoteReceiverHealth
from core.radio.registry import ReceiverDescriptor

_OPENWEBRX_MODE_MAP = {"nbfm": "nfm"}


class OpenWebRXTransport(Protocol):
    def start(
        self,
        *,
        websocket_url: str,
        timeout_s: float,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[str], None],
    ) -> None: ...

    def send_text(self, message: str) -> None: ...

    def send_control(self, payload: Mapping[str, object]) -> None: ...

    def stop(self) -> None: ...


class WebSocketOpenWebRXTransport:
    """OpenWebRX WebSocket transport; binary audio/FFT frames are discarded in v1."""

    def __init__(self) -> None:
        self._connection = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_disconnect: Callable[[str], None] | None = None

    def start(
        self,
        *,
        websocket_url: str,
        timeout_s: float,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[str], None],
    ) -> None:
        from websockets.sync.client import connect

        self._stop.clear()
        self._on_disconnect = on_disconnect
        self._connection = connect(
            websocket_url,
            open_timeout=timeout_s,
            compression=None,
            max_size=1_048_576,
        )
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(on_message,),
            daemon=True,
            name="openwebrx-reader",
        )
        self._reader.start()

    def _read_loop(self, on_message: Callable[[object], None]) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    message = connection.recv(timeout=1.0, decode=False)
                except TimeoutError:
                    continue
                if isinstance(message, bytes):
                    # Binary streams remain ephemeral; the adapter decides whether an audio frame
                    # is routed to the bounded decoder tap. Nothing is persisted here.
                    on_message(message)
                    continue
                if isinstance(message, str):
                    try:
                        on_message(json.loads(message))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            if not self._stop.is_set() and self._on_disconnect is not None:
                self._on_disconnect("transport_disconnected")

    def send_text(self, message: str) -> None:
        if self._connection is None:
            raise RuntimeError("OpenWebRX transport is not connected")
        self._connection.send(message)

    def send_control(self, payload: Mapping[str, object]) -> None:
        self.send_text(json.dumps(dict(payload), separators=(",", ":")))

    def stop(self) -> None:
        self._stop.set()
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)


def openwebrx_websocket_url(frontend_url: str) -> str:
    parsed = urlsplit(frontend_url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(
        parsed.scheme.lower()
    )
    if scheme is None or not parsed.netloc:
        raise ValueError("OpenWebRX frontend URL must be http(s) or ws(s)")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/ws/" if base_path else "/ws/"
    return urlunsplit((scheme, parsed.netloc.lower(), path, "", ""))


class OpenWebRXAdapter:
    def __init__(
        self,
        descriptor: ReceiverDescriptor,
        *,
        on_observation: ObservationCallback,
        transport: OpenWebRXTransport | None = None,
        connect_timeout_s: float = 10.0,
    ) -> None:
        if descriptor.provider != "openwebrx":
            raise ValueError("OpenWebRXAdapter requires provider=openwebrx")
        if connect_timeout_s <= 0:
            raise ValueError("connect_timeout_s must be positive")
        self._descriptor = descriptor
        self._on_observation = on_observation
        self._transport = transport or WebSocketOpenWebRXTransport()
        self._connect_timeout_s = connect_timeout_s
        self._connected = False
        self._last_message_at: datetime | None = None
        self._observations_received = 0
        self._error: str | None = None
        self._frequency_hz: int | None = None
        self._mode: str | None = None
        self._session_id = ""
        self._center_freq_hz: int | None = None
        self._sample_rate_hz: int | None = None
        self._audio_compression: str | None = None
        self._dsp_started = False
        self._pending_tune: tuple[int, str] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._error = None
            self._session_id = uuid.uuid4().hex
            self._center_freq_hz = None
            self._sample_rate_hz = None
            self._audio_compression = None
            self._dsp_started = False
            self._pending_tune = None
        try:
            self._transport.start(
                websocket_url=openwebrx_websocket_url(self._descriptor.frontend_url),
                timeout_s=self._connect_timeout_s,
                on_message=self._on_message,
                on_disconnect=self._on_disconnect,
            )
            self._transport.send_text("SERVER DE CLIENT client=seacommons type=receiver")
            self._transport.send_control(
                {
                    "type": "connectionproperties",
                    "params": {"output_rate": 12000, "hd_output_rate": 48000},
                }
            )
        except Exception as exc:
            try:
                self._transport.stop()
            except Exception:
                pass
            with self._lock:
                self._connected = False
                self._error = "connect_failed"
            raise RuntimeError("OpenWebRX connection failed") from exc
        with self._lock:
            self._connected = True

    def stop(self) -> None:
        try:
            self._transport.stop()
        finally:
            with self._lock:
                self._connected = False
                self._error = None
                self._dsp_started = False

    def health(self) -> RemoteReceiverHealth:
        with self._lock:
            return RemoteReceiverHealth(
                receiver_id=self._descriptor.receiver_id,
                provider=self._descriptor.provider,
                connected=self._connected,
                last_message_at=self._last_message_at,
                observations_received=self._observations_received,
                error=self._error,
            )

    def capabilities(self):
        return self._descriptor.capabilities

    def tune(self, frequency_hz: int, mode: str) -> None:
        normalized_mode = str(mode or "").strip().lower()
        if not normalized_mode:
            raise ValueError("mode must not be empty")
        if not any(
            capability.frequency_min_hz <= frequency_hz <= capability.frequency_max_hz
            and normalized_mode in capability.modes
            for capability in self._descriptor.capabilities
        ):
            raise ValueError("frequency or mode is outside configured receiver capability")
        with self._lock:
            if not self._connected:
                raise RuntimeError("OpenWebRX adapter is not connected")
            center = self._center_freq_hz
            sample_rate = self._sample_rate_hz
            dsp_started = self._dsp_started
        if center is None or sample_rate is None:
            with self._lock:
                self._pending_tune = (int(frequency_hz), normalized_mode)
            return
        from core.radio.dsc_tuning import receiver_carrier_hz

        wire_frequency_hz = receiver_carrier_hz(frequency_hz, normalized_mode)
        if not center - sample_rate // 2 <= wire_frequency_hz <= center + sample_rate // 2:
            raise ValueError("frequency is outside active OpenWebRX profile")
        wire_mode = _OPENWEBRX_MODE_MAP.get(normalized_mode, normalized_mode)
        if not dsp_started:
            self._transport.send_control({"type": "dspcontrol", "action": "start"})
        self._transport.send_control(
            {
                "type": "dspcontrol",
                "params": {"offset_freq": int(wire_frequency_hz - center), "mod": wire_mode},
            }
        )
        with self._lock:
            self._frequency_hz = int(frequency_hz)
            self._mode = normalized_mode
            self._dsp_started = True
            self._pending_tune = None

    def _on_disconnect(self, _detail: str) -> None:
        with self._lock:
            self._connected = False
            self._error = "transport_disconnected"
            self._dsp_started = False

    def _on_message(self, message: object) -> None:
        if isinstance(message, bytes):
            if not message or message[0] not in {0x02, 0x04}:
                return
            with self._lock:
                frequency_hz = self._frequency_hz
                mode = self._mode
                audio_compression = self._audio_compression
            if frequency_hz is None or mode is None or len(message) <= 1:
                return
            if str(audio_compression or '').strip().lower() not in {'none', 'off', 'false', '0'}:
                return
            try:
                from core.radio.decoder_runtime import EphemeralRadioFrame, submit_ephemeral_frame
                submit_ephemeral_frame(EphemeralRadioFrame(
                    receiver_id=self._descriptor.receiver_id, provider=self._descriptor.provider,
                    physical_lineage=self._descriptor.physical_lineage, frequency_hz=frequency_hz,
                    mode=mode, observed_at=datetime.now(timezone.utc),
                    sample_rate_hz=48_000 if message[0] == 0x04 else 12_000,
                    encoding="openwebrx_pcm_s16le", payload=message[1:],
                    source_terms=self._descriptor.source_terms,
                ))
            except Exception:
                pass
            return
        if not isinstance(message, Mapping):
            return
        message_type = str(message.get("type") or "").strip().lower()
        if message_type == "config":
            value = message.get("value")
            if isinstance(value, Mapping):
                center = value.get("center_freq")
                sample_rate = value.get("samp_rate")
                audio_compression = value.get("audio_compression")
                with self._lock:
                    if center is not None:
                        self._center_freq_hz = int(center)
                    if sample_rate is not None:
                        self._sample_rate_hz = int(sample_rate)
                    if audio_compression is not None:
                        self._audio_compression = str(audio_compression)
                    pending_tune = self._pending_tune
                if pending_tune is not None:
                    try:
                        self.tune(*pending_tune)
                    except (RuntimeError, ValueError):
                        pass
            return
        if message_type != "smeter":
            return
        raw_value = message.get("value")
        try:
            linear = float(raw_value)
        except (TypeError, ValueError):
            return
        if linear <= 0 or not math.isfinite(linear):
            return
        with self._lock:
            frequency_hz = self._frequency_hz
            mode = self._mode
            session_id = self._session_id
            if frequency_hz is None or mode is None:
                return
        observed_at = datetime.now(timezone.utc)
        observation = RadioObservation(
            receiver_id=self._descriptor.receiver_id,
            provider=self._descriptor.provider,
            physical_lineage=self._descriptor.physical_lineage,
            frequency_hz=frequency_hz,
            mode=mode,
            observed_at=observed_at,
            signal_dbfs=round(10.0 * math.log10(linear), 1),
            source_terms=self._descriptor.source_terms,
            session_id=session_id,
        )
        with self._lock:
            self._last_message_at = observed_at
            self._observations_received += 1
        self._on_observation(observation)
