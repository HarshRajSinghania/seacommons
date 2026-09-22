# SPDX-License-Identifier: AGPL-3.0-or-later
"""MF/HF DSC tuning contract for generic SSB web receivers."""
from __future__ import annotations

HF_DSC_ASSIGNED_FREQUENCIES_HZ = frozenset({
    2_187_500,
    4_207_500,
    6_312_000,
    8_414_500,
    12_577_000,
    16_804_500,
})

DSC_AUDIO_CENTER_HZ = 1_700
DSC_MARK_HZ = 1_615
DSC_SPACE_HZ = 1_785
DSC_BAUD = 100


def receiver_carrier_hz(assigned_frequency_hz: int, mode: str) -> int:
    """Carrier setting that yields 1615/1785 Hz audio in USB.

    Frame metadata keeps the assigned DSC frequency; this offset is transport
    tuning only and must never rewrite evidence frequency identity.
    """
    assigned = int(assigned_frequency_hz)
    if str(mode or "").strip().lower() == "usb" and assigned in HF_DSC_ASSIGNED_FREQUENCIES_HZ:
        return assigned - DSC_AUDIO_CENTER_HZ
    return assigned
