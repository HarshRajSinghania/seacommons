from __future__ import annotations

from array import array
import struct


def test_kiwi_raw_big_endian_pcm_is_normalized_to_12khz_s16le():
    from core.radio.pcm import normalize_pcm16le

    values = [0, 1000, -1000, 32767, -32768]
    payload = struct.pack(">5h", *values)
    out = normalize_pcm16le(payload, encoding="kiwi_snd_raw", sample_rate_hz=12_000)
    assert out.sample_rate_hz == 12_000
    assert out.encoding == "pcm_s16le"
    assert out.payload == struct.pack("<5h", *values)


def test_48khz_pcm_is_downsampled_to_12khz_bounded():
    from core.radio.pcm import normalize_pcm16le

    samples = array("h", range(0, 400))
    out = normalize_pcm16le(samples.tobytes(), encoding="openwebrx_pcm_s16le", sample_rate_hz=48_000)
    assert out.sample_rate_hz == 12_000
    assert len(out.payload) == 200


def test_unknown_or_misaligned_audio_fails_closed():
    import pytest
    from core.radio.pcm import normalize_pcm16le

    with pytest.raises(ValueError):
        normalize_pcm16le(b"abc", encoding="openwebrx_audio", sample_rate_hz=12_000)
    with pytest.raises(ValueError):
        normalize_pcm16le(b"abc", encoding="kiwi_snd_raw", sample_rate_hz=12_000)
