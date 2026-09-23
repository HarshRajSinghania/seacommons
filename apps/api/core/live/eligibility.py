# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared, side-effect-free public Live eligibility primitives.

Keep narrow evidence gates here when both projection and durable retention
must make the exact same decision.  This avoids the two surfaces drifting
apart while leaving event-type/publication policy in ``projection.py``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.domain.incident_taxonomy import is_independently_corroborated

_AIS_GAP_TYPES = frozenset({"gap", "long_gap", "ais_gap"})


def is_ais_gap(anomaly_type: object) -> bool:
    return str(anomaly_type or "").strip().lower() in _AIS_GAP_TYPES


def ais_gap_has_public_support(metadata: Mapping[str, Any] | None) -> bool:
    """Whether an AIS-gap observation has enough support for public Live.

    Independent corroboration is sufficient.  Otherwise SeaCommons requires
    an explainable strong same-lineage reception expectation.  The latter is
    evidence that reception should have continued, not corroboration of intent.
    """
    meta = metadata or {}
    if is_independently_corroborated(meta):
        return True
    reception = meta.get("reception_expectation") or {}
    return bool(
        isinstance(reception, Mapping)
        and str(reception.get("support_level") or "").lower() == "strong"
    )
