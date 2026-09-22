from __future__ import annotations

from array import array
from dataclasses import dataclass
import sys

_ALLOWED = {"kiwi_snd_raw", "openwebrx_pcm_s16le", "pcm_s16le"}


@dataclass(frozen=True)
class NormalizedPCM:
    payload: bytes
    sample_rate_hz: int
    encoding: str = "pcm_s16le"


def _decode_s16(payload: bytes, *, byteorder: str) -> array:
    if not payload or len(payload) % 2:
        raise ValueError("PCM16 payload must be non-empty and 16-bit aligned")
    values = array("h")
    values.frombytes(payload)
    if values.itemsize != 2:
        raise RuntimeError("unexpected host short size")
    if byteorder not in {"little", "big"}:
        raise ValueError("unsupported PCM16 byte order")
    if byteorder != sys.byteorder:
        values.byteswap()
    return values


def _encode_s16le(values: array) -> bytes:
    encoded = array("h", values)
    if sys.byteorder != "little":
        encoded.byteswap()
    return encoded.tobytes()


def _resample_linear(samples: array, in_rate: int, out_rate: int) -> array:
    if in_rate == out_rate:
        return samples
    if in_rate <= 0 or out_rate <= 0:
        raise ValueError("sample rates must be positive")
    out_len = max(1, round(len(samples) * out_rate / in_rate))
    result = array("h")
    scale = in_rate / out_rate
    for index in range(out_len):
        pos = index * scale
        left = min(int(pos), len(samples) - 1)
        right = min(left + 1, len(samples) - 1)
        frac = pos - left
        value = round(samples[left] + (samples[right] - samples[left]) * frac)
        result.append(max(-32768, min(32767, value)))
    return result


def normalize_pcm16le(payload: bytes, *, encoding: str, sample_rate_hz: int, target_rate_hz: int = 12_000) -> NormalizedPCM:
    normalized_encoding = str(encoding).strip().lower()
    if normalized_encoding not in _ALLOWED:
        raise ValueError("unsupported ephemeral audio encoding")
    # KiwiSDR's ordinary non-camping mono SND stream is network-order
    # signed 16-bit PCM. OpenWebRX and our internal normalized form are LE.
    byteorder = "big" if normalized_encoding == "kiwi_snd_raw" else "little"
    values = _decode_s16(payload, byteorder=byteorder)
    normalized = _resample_linear(values, int(sample_rate_hz), int(target_rate_hz))
    return NormalizedPCM(payload=_encode_s16le(normalized), sample_rate_hz=int(target_rate_hz))
