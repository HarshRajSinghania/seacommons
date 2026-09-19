from __future__ import annotations

import base64
import json
import queue
import select
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping, Protocol

from core.radio.provider import DecodedRadioMessage

_MAX_EPHEMERAL_FRAME_BYTES = 262_144


@dataclass(frozen=True)
class EphemeralRadioFrame:
    receiver_id: str
    provider: str
    physical_lineage: str
    frequency_hz: int
    mode: str
    observed_at: datetime
    sample_rate_hz: int
    encoding: str
    payload: bytes
    source_terms: str | None = None

    def __post_init__(self) -> None:
        if not self.receiver_id.strip() or not self.provider.strip() or not self.physical_lineage.strip():
            raise ValueError("receiver/provider/lineage required")
        if self.frequency_hz <= 0 or self.sample_rate_hz <= 0:
            raise ValueError("frequency and sample rate must be positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if not isinstance(self.payload, bytes) or len(self.payload) > _MAX_EPHEMERAL_FRAME_BYTES:
            raise ValueError("payload exceeds ephemeral frame bound")

    def public_metadata(self) -> dict[str, object]:
        return {
            "receiver_id": self.receiver_id,
            "provider": self.provider,
            "frequency_hz": self.frequency_hz,
            "mode": self.mode,
            "observed_at": self.observed_at.isoformat(),
            "sample_rate_hz": self.sample_rate_hz,
            "encoding": self.encoding,
            "bytes": len(self.payload),
        }


class RadioDecoder(Protocol):
    def decode(self, frame: EphemeralRadioFrame) -> Iterable[Mapping[str, object]]: ...


class RadioDecoderRuntime:
    def __init__(
        self,
        *,
        enabled: bool,
        decoders: Iterable[RadioDecoder] = (),
        decoded_handler: Callable[[DecodedRadioMessage], dict[str, object]],
        queue_size: int = 32,
    ) -> None:
        self.enabled = bool(enabled)
        self._decoders = tuple(decoders)
        self._decoded_handler = decoded_handler
        self._frames = 0
        self._decoded = 0
        self._invalid = 0
        self._dropped = 0
        self._errors = 0
        self._queue: queue.Queue[EphemeralRadioFrame] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "decoders": len(self._decoders),
            "frames": self._frames,
            "decoded": self._decoded,
            "invalid": self._invalid,
            "dropped": self._dropped,
            "errors": self._errors,
            "queued": self._queue.qsize(),
            "worker_alive": bool(self._worker and self._worker.is_alive()),
        }

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True, name="radio-decoder")
        self._worker.start()

    def submit_frame(self, frame: EphemeralRadioFrame) -> bool:
        if not self.enabled:
            return False
        self.start()
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.ingest_frame(frame)
            except Exception:
                # A malformed decoder response or downstream persistence error
                # must never kill the one bounded decoder worker silently.
                self._errors += 1
            finally:
                self._queue.task_done()

    def stop(self) -> None:
        self._stop.set()
        worker, self._worker = self._worker, None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        for decoder in self._decoders:
            close = getattr(decoder, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def ingest_frame(self, frame: EphemeralRadioFrame) -> dict[str, object]:
        if not self.enabled:
            return {"accepted": False, "reason": "disabled"}
        self._frames += 1
        decoded = 0
        invalid = 0
        for decoder in self._decoders:
            try:
                outputs = tuple(decoder.decode(frame))
            except Exception:
                invalid += 1
                continue
            for output in outputs[:16]:
                try:
                    kind = str(output.get("kind") or "").strip().lower()
                    payload = output.get("payload")
                    message = DecodedRadioMessage(
                        kind=kind,
                        receiver_id=frame.receiver_id,
                        provider=frame.provider,
                        physical_lineage=frame.physical_lineage,
                        frequency_hz=frame.frequency_hz,
                        mode=frame.mode,
                        observed_at=frame.observed_at,
                        payload=payload,  # type: ignore[arg-type]
                        provider_message_id=str(output.get("message_id") or "").strip() or None,
                        source_terms=frame.source_terms,
                    )
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                try:
                    self._decoded_handler(message)
                except Exception:
                    self._errors += 1
                    continue
                decoded += 1
        self._decoded += decoded
        self._invalid += invalid
        return {"accepted": True, "decoded": decoded, "invalid": invalid}

class JSONLProcessDecoder:
    """Persistent bounded JSONL decoder subprocess. No shell, no file persistence."""

    def __init__(self, command: tuple[str, ...], *, timeout_s: float = 0.5, max_output_chars: int = 65536) -> None:
        if not command or not command[0]:
            raise ValueError("decoder command required")
        if timeout_s <= 0:
            raise ValueError("decoder timeout must be positive")
        self._command = tuple(command)
        self._timeout_s = float(timeout_s)
        self._max_output_chars = max(1024, int(max_output_chars))
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def _ensure_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self.close()
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            close_fds=True,
        )
        return self._process

    def decode(self, frame: EphemeralRadioFrame) -> Iterable[Mapping[str, object]]:
        from core.radio.pcm import normalize_pcm16le

        pcm = normalize_pcm16le(
            frame.payload, encoding=frame.encoding, sample_rate_hz=frame.sample_rate_hz
        )
        envelope = {
            "receiver_id": frame.receiver_id,
            "provider": frame.provider,
            "physical_lineage": frame.physical_lineage,
            "frequency_hz": frame.frequency_hz,
            "mode": frame.mode,
            "observed_at": frame.observed_at.isoformat(),
            "sample_rate_hz": pcm.sample_rate_hz,
            "encoding": pcm.encoding,
            "payload_b64": base64.b64encode(pcm.payload).decode("ascii"),
        }
        with self._lock:
            process = self._ensure_process()
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(envelope, separators=(",", ":")) + "\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], self._timeout_s)
            if not ready:
                raise TimeoutError("decoder response timeout")
            line = process.stdout.readline(self._max_output_chars + 1)
            if len(line) > self._max_output_chars:
                raise ValueError("decoder output exceeds bound")
        payload = json.loads(line)
        messages = payload.get("messages", []) if isinstance(payload, Mapping) else []
        if not isinstance(messages, list):
            raise ValueError("decoder messages must be a list")
        return tuple(item for item in messages[:16] if isinstance(item, Mapping))

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

_decoder_runtime: RadioDecoderRuntime | None = None
_decoder_runtime_lock = threading.Lock()


def runtime_from_config() -> RadioDecoderRuntime:
    from core.config import config
    from core.radio.bridge import handle_decoded_radio_message

    decoders: list[RadioDecoder] = []
    if config.RADIO_DECODER_ENABLED and str(config.RADIO_DECODER_COMMANDS_JSON or "").strip():
        try:
            commands = json.loads(config.RADIO_DECODER_COMMANDS_JSON)
        except json.JSONDecodeError:
            commands = []
        if isinstance(commands, list):
            for raw in commands[:4]:
                if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v for v in raw):
                    continue
                decoders.append(JSONLProcessDecoder(tuple(raw), timeout_s=config.RADIO_DECODER_TIMEOUT_S))
    return RadioDecoderRuntime(
        enabled=bool(config.RADIO_DECODER_ENABLED and decoders),
        decoders=decoders,
        decoded_handler=handle_decoded_radio_message,
        queue_size=config.RADIO_DECODER_QUEUE_SIZE,
    )


def get_radio_decoder_runtime() -> RadioDecoderRuntime:
    global _decoder_runtime
    if _decoder_runtime is None:
        with _decoder_runtime_lock:
            if _decoder_runtime is None:
                _decoder_runtime = runtime_from_config()
    return _decoder_runtime


def submit_ephemeral_frame(frame: EphemeralRadioFrame) -> bool:
    from core.radio.listen import publish_ephemeral_audio

    listen_deliveries = publish_ephemeral_audio(frame)
    decoder_accepted = get_radio_decoder_runtime().submit_frame(frame)
    return bool(listen_deliveries or decoder_accepted)


def radio_decoder_status() -> dict[str, object]:
    return get_radio_decoder_runtime().status()


def reset_radio_decoder_runtime() -> None:
    global _decoder_runtime
    with _decoder_runtime_lock:
        runtime, _decoder_runtime = _decoder_runtime, None
    if runtime is not None:
        runtime.stop()
