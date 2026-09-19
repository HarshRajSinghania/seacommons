# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical semantic visual taxonomy for public Live.

SeaCommons classifies signals by CATEGORY; it does not assign a synthetic
LOW/MEDIUM/HIGH/CRITICAL risk rating. Colour and visual identity are a pure
function of the semantic category — never of ``severity``, OCR confidence, or
lifecycle.

This module is the single source of truth shared by:

* ``core.live.projection`` (VM REST/WS feed)
* ``core.live_edge_publisher`` (Cloudflare edge push)

and mirrored — value for value — by the frontend
``apps/web/src/features/intel/categories.js``. It has no FastAPI / SQLAlchemy
dependency; keep it that way so the low-memory edge publisher can import it.

Invariants:

* Alarm Phone is ``humanitarian_alarm_phone`` (red) because of its
  category/source role — for a maritime point, a land point, a region area,
  a drift origin and a drift trajectory/cone alike. Lifecycle never changes
  that category or its colour.
* ``classify_visual_category`` never inspects ``severity``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Canonical category -> colour. Mirror of EVENT_VISUAL_CATEGORIES /
# SIGNAL_CATEGORIES colours in apps/web/src/features/intel/categories.js.
CATEGORY_COLORS: dict[str, str] = {
    "humanitarian_alarm_phone": "#ff3b3b",
    "civil_sar": "#4ade80",
    "state_sar": "#38bdf8",
    "navigation_casualty": "#ff4d5e",
    "spoofing": "#c084fc",
    "ais_gap": "#fb923c",
    "loitering": "#facc15",
    "rendezvous": "#f97316",
    "sanctions": "#f472b6",
    "infrastructure": "#22d3ee",
    "identity": "#60a5fa",
    "piracy": "#ef4444",
    "environmental": "#34d399",
    "news": "#94a3b8",
    "social": "#818cf8",
    "ngo_activity": "#4ade80",
    "hazard": "#f59e0b",
    "iom": "#b91c1c",
    "distress": "#ff3b3b",
    "context": "#8bf0c5",
}

CATEGORY_LABELS: dict[str, str] = {
    "humanitarian_alarm_phone": "Alarm Phone (humanitarian)",
    "civil_sar": "Civil SAR / NGO",
    "state_sar": "State SAR / Coast Guard",
    "navigation_casualty": "Navigation casualty",
    "spoofing": "AIS spoofing / impossible movement",
    "ais_gap": "AIS gap / dark activity",
    "loitering": "Loitering / abnormal dwell",
    "rendezvous": "Rendezvous / ship-to-ship",
    "sanctions": "Sanctions match",
    "infrastructure": "Infrastructure proximity",
    "identity": "Identity / flag anomaly",
    "piracy": "Piracy / security incident",
    "environmental": "Environmental hazard",
    "news": "News / RSS",
    "social": "Social post",
    "ngo_activity": "NGO activity",
    "hazard": "Natural hazard (GDACS)",
    "iom": "IOM missing migrants",
    "distress": "Maritime distress",
    "context": "Maritime context",
}

DEFAULT_CATEGORY = "context"

_ALARM_PHONE_RE = re.compile(r"alarm.?phone", re.IGNORECASE)


def is_alarm_phone(source: Any, metadata: Mapping[str, Any] | None = None) -> bool:
    """True for any Alarm Phone report, by source name or tracked account."""
    if _ALARM_PHONE_RE.search(str(source or "")):
        return True
    meta = metadata or {}
    return bool(_ALARM_PHONE_RE.search(str(meta.get("tracked_account") or "")))


def _tokens(event_type: str, metadata: Mapping[str, Any]) -> str:
    parts = [
        event_type,
        metadata.get("anomaly_type"),
        metadata.get("alert_type"),
        metadata.get("ais_nav_status_kind"),
        metadata.get("detection_reason"),
    ]
    anomaly_types = metadata.get("anomaly_types")
    if isinstance(anomaly_types, (list, tuple)):
        parts.extend(anomaly_types)
    return re.sub(r"[\s-]+", "_", " ".join(str(p) for p in parts if p).lower())


def classify_visual_category(
    *,
    source: Any = "",
    event_type: str = "",
    maritime_domain: str | None = None,
    humanitarian_case_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Return the canonical visual category for a signal.

    Never reads ``severity`` / ``intel_severity`` / OCR confidence / lifecycle.
    """
    meta = metadata or {}
    event_type = str(event_type or "")
    domain = str(maritime_domain or "").lower()

    # 1. Alarm Phone is red because of its source/category role, full stop.
    if is_alarm_phone(source, meta):
        return "humanitarian_alarm_phone"

    tokens = _tokens(event_type, meta)

    # 2. Explicit anomaly / security semantics.
    if re.search(r"spoof|teleport|impossible_speed|impossible_movement|gnss_manip", tokens):
        return "spoofing"
    if re.search(r"ais_gap|dark_vessel|dark_activity|signal_gap|transponder_off|(^|_)gap($|_)", tokens):
        return "ais_gap"
    if re.search(r"loiter|abnormal_dwell|stationary_anomaly", tokens):
        return "loitering"
    if re.search(r"rendezvous|ship_to_ship|(^|_)sts(_|$)|proximity_pair", tokens):
        return "rendezvous"
    if meta.get("sanctions_matched") or domain == "sanctions" or "sanction" in tokens:
        return "sanctions"
    if meta.get("infrastructure") or re.search(r"pipeline|cable|infrastructure|platform_proximity", tokens):
        return "infrastructure"
    if re.search(r"identity|flag_hopping|mmsi_mismatch|imo_mismatch|false_flag", tokens):
        return "identity"
    if re.search(
        r"not_under_command|unable_to_man|restricted_man|aground|engine_failure|"
        r"mechanical_failure|disabled_vessel",
        tokens,
    ):
        return "navigation_casualty"
    if domain == "piracy" or re.search(r"piracy|hijack|armed_robbery", tokens):
        return "piracy"
    if domain == "environmental" or re.search(r"pollution|oil_spill|environmental", tokens):
        return "environmental"

    # 3. Humanitarian / SAR semantics.
    hct = str(humanitarian_case_type or "").lower()
    if event_type == "iom_incident":
        return "iom"
    if event_type == "gdacs":
        return "hazard"
    if event_type == "ngo_activity":
        operator_type = str(meta.get("operator_type") or "").lower()
        if operator_type == "state_authority":
            return "state_sar"
        if operator_type == "civil_ngo":
            return "civil_sar"
        return "ngo_activity"
    if hct and hct not in {"advocacy", "unknown_humanitarian"}:
        return "distress"
    # `distress` is already the canonical semantic event type. AIS SART/EPIRB
    # observations intentionally live in the Maritime Safety domain, so requiring
    # domain=sar here incorrectly downgraded genuine distress beacons to context.
    if event_type == "distress" or (domain == "sar" and meta.get("is_distress")):
        return "distress"

    # 4. Source-class fallbacks (still never severity).
    if event_type in {"twitter", "mastodon", "bluesky"}:
        return "social"
    if event_type == "news":
        return "news"
    return DEFAULT_CATEGORY


def visual_category_fields(
    *,
    source: Any = "",
    event_type: str = "",
    maritime_domain: str | None = None,
    humanitarian_case_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """``{visual_category, visual_color, category_label}`` for a signal or its drift."""
    category = classify_visual_category(
        source=source,
        event_type=event_type,
        maritime_domain=maritime_domain,
        humanitarian_case_type=humanitarian_case_type,
        metadata=metadata,
    )
    return {
        "visual_category": category,
        "visual_color": CATEGORY_COLORS.get(category, CATEGORY_COLORS[DEFAULT_CATEGORY]),
        "category_label": CATEGORY_LABELS.get(category, CATEGORY_LABELS[DEFAULT_CATEGORY]),
    }
