# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

from core.radio.provider import ObservationCallback, RadioObservation, RemoteReceiverHealth
from core.radio.registry import ReceiverDescriptor

_KIWI_MIN_FREQUENCY_HZ = 10_000
_KIWI_MAX_FREQUENCY_HZ = 30_000_000
_KIWI_PASSBANDS: dict[str, tuple[int, int]] = {
    "am": (-4900, 4900),
    "usb": (300, 2700),
    "lsb": (-2700, -300),
    "nbfm": (-6000, 6000),
}


class KiwiTransport(Protocol):
    def start(
        self,
        *,
        websocket_url: str,
        timeout_s: float,
        on_frame: Callable[[bytes], None],
        on_disconnect: Callable[[str], None],
    ) -> None: ...

    def send(self, message: str) -> None: ...

    def stop(self) -> None: ...


class WebSocketKiwiTransport:
    """Small Kiwi WebSocket transport; frame interpretation stays in the adapter."""

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
        on_frame: Callable[[bytes], None],
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
            args=(on_frame,),
            daemon=True,
            name="kiwisdr-reader",
        )
        self._reader.start()

    def _read_loop(self, on_frame: Callable[[bytes], None]) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    frame = connection.recv(timeout=1.0, decode=False)
                except TimeoutError:
                    continue
                if isinstance(frame, bytes):
                    on_frame(frame)
        except Exception:
            if not self._stop.is_set() and self._on_disconnect is not None:
                self._on_disconnect("transport_disconnected")

    def send(self, message: str) -> None:
        if self._connection is None:
            raise RuntimeError("KiwiSDR transport is not connected")
        self._connection.send(message)

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


def kiwi_websocket_url(frontend_url: str, stream_id: int) -> str:
    parsed = urlsplit(frontend_url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(
        parsed.scheme.lower()
    )
    if scheme is None or not parsed.netloc:
        raise ValueError("KiwiSDR frontend URL must be http(s) or ws(s)")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/{int(stream_id)}/SND" if base_path else f"/{int(stream_id)}/SND"
    return urlunsplit((scheme, parsed.netloc.lower(), path, "", ""))


class KiwiSDRAdapter:
    def __init__(
        self,
        descriptor: ReceiverDescriptor,
        *,
        on_observation: ObservationCallback,
        transport: KiwiTransport | None = None,
        connect_timeout_s: float = 10.0,
        stream_id_factory: Callable[[], int] | None = None,
    ) -> None:
        if descriptor.provider != "kiwisdr":
            raise ValueError("KiwiSDRAdapter requires provider=kiwisdr")
        if connect_timeout_s <= 0:
            raise ValueError("connect_timeout_s must be positive")
        self._descriptor = descriptor
        self._on_observation = on_observation
        self._transport = transport or WebSocketKiwiTransport()
        self._connect_timeout_s = connect_timeout_s
        self._stream_id_factory = stream_id_factory or (lambda: time.time_ns() // 1_000_000)
        self._connected = False
        self._last_message_at: datetime | None = None
        self._observations_received = 0
        self._error: str | None = None
        self._frequency_hz: int | None = None
        self._mode: str | None = None
        self._session_id = ""
        self._audio_rate_hz: int | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._error = None
            self._session_id = uuid.uuid4().hex
            self._audio_rate_hz = None
        websocket_url = kiwi_websocket_url(
            self._descriptor.frontend_url,
            self._stream_id_factory(),
        )
        try:
            self._transport.start(
                websocket_url=websocket_url,
                timeout_s=self._connect_timeout_s,
                on_frame=self._on_frame,
                on_disconnect=self._on_disconnect,
            )
            self._transport.send("SET auth t=kiwi p=")
        except Exception as exc:
            try:
                self._transport.stop()
            except Exception:
                pass
            with self._lock:
                self._connected = False
                self._error = "connect_failed"
            raise RuntimeError("KiwiSDR connection failed") from exc
        with self._lock:
            self._connected = True

    def stop(self) -> None:
        try:
            self._transport.stop()
        finally:
            with self._lock:
                self._connected = False
                self._error = None

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
        if normalized_mode not in _KIWI_PASSBANDS:
            raise ValueError(f"unsupported KiwiSDR mode: {normalized_mode or 'empty'}")
        if not (_KIWI_MIN_FREQUENCY_HZ <= frequency_hz <= _KIWI_MAX_FREQUENCY_HZ):
            raise ValueError("frequency is outside KiwiSDR capability")
        if not any(
            capability.frequency_min_hz <= frequency_hz <= capability.frequency_max_hz
            and normalized_mode in capability.modes
            for capability in self._descriptor.capabilities
        ):
            raise ValueError("frequency or mode is outside configured receiver capability")
        with self._lock:
            if not self._connected:
                raise RuntimeError("KiwiSDR adapter is not connected")
        with self._lock:
            self._frequency_hz = frequency_hz
            self._mode = normalized_mode
        self._send_tune_message(frequency_hz, normalized_mode)

    def _send_tune_message(self, frequency_hz: int, normalized_mode: str) -> None:
        from core.radio.dsc_tuning import receiver_carrier_hz

        low_cut, high_cut = _KIWI_PASSBANDS[normalized_mode]
        wire_frequency_hz = receiver_carrier_hz(frequency_hz, normalized_mode)
        frequency_khz = wire_frequency_hz / 1000
        self._transport.send(
            f"SET mod={normalized_mode} low_cut={low_cut} high_cut={high_cut} freq={frequency_khz:g}"
        )

    def _complete_audio_handshake(self, frame: bytes) -> None:
        text = frame.decode("utf-8", errors="ignore")
        match = re.search(r"audio_rate=(\d+)", text)
        if match is None:
            return
        audio_rate = int(match.group(1))
        with self._lock:
            self._audio_rate_hz = audio_rate
        self._transport.send(f"SET AR OK in={audio_rate} out={audio_rate}")
        self._transport.send("SERVER DE CLIENT SeaCommons SND")
        self._transport.send("SET agc=1 hang=0 thresh=-130 slope=6 decay=1000 manGain=50")
        self._transport.send("SET compression=0")
        with self._lock:
            frequency_hz = self._frequency_hz
            mode = self._mode
        if frequency_hz is not None and mode is not None:
            self._send_tune_message(frequency_hz, mode)

    def _on_disconnect(self, _detail: str) -> None:
        with self._lock:
            self._connected = False
            self._error = "transport_disconnected"

    def _on_frame(self, frame: bytes) -> None:
        if frame.startswith(b"MSG"):
            if b"audio_init=" in frame:
                self._complete_audio_handshake(frame)
            return
        if len(frame) < 10 or frame[:3] != b"SND":
            return
        sequence = int.from_bytes(frame[4:8], "little")
        smeter = int.from_bytes(frame[8:10], "big")
        signal_dbm = round(0.1 * smeter - 127.0, 1)
        observed_at = datetime.now(timezone.utc)
        with self._lock:
            frequency_hz = self._frequency_hz
            mode = self._mode
            session_id = self._session_id
            audio_rate_hz = self._audio_rate_hz
            if frequency_hz is None or mode is None:
                return
            self._last_message_at = observed_at
            self._observations_received += 1
        audio_payload = frame[10:]
        if audio_payload and audio_rate_hz:
            try:
                from core.radio.decoder_runtime import EphemeralRadioFrame, submit_ephemeral_frame
                submit_ephemeral_frame(EphemeralRadioFrame(
                    receiver_id=self._descriptor.receiver_id, provider=self._descriptor.provider,
                    physical_lineage=self._descriptor.physical_lineage, frequency_hz=frequency_hz,
                    mode=mode, observed_at=observed_at, sample_rate_hz=audio_rate_hz,
                    encoding="kiwi_snd_raw", payload=audio_payload, source_terms=self._descriptor.source_terms,
                ))
            except Exception:
                pass
        self._on_observation(
            RadioObservation(
                receiver_id=self._descriptor.receiver_id,
                provider=self._descriptor.provider,
                physical_lineage=self._descriptor.physical_lineage,
                frequency_hz=frequency_hz,
                mode=mode,
                observed_at=observed_at,
                signal_dbm=signal_dbm,
                source_terms=self._descriptor.source_terms,
                provider_message_id=str(sequence),
                session_id=session_id,
            )
        )
