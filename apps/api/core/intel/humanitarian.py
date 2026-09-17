# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical, people-centred metadata for public humanitarian incidents."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from core.domain.live_contracts import HumanitarianCaseType
from core.intel.geoextract import is_ongoing_incident, is_resolved_distress

_PEOPLE = re.compile(
    r"(?:(?P<approx>~|≈|about|around|approximately|approx\.?|ca\.?|environ|circa|env\.?)\s*)?"
    r"(?P<count>\d{1,4})\s*"
    # English, then French / Italian / Spanish / German -- Alarm Phone posts
    # the same alert in several languages and the head-count must resolve the
    # same way in each (docs/prompt.md: "Support at least English, Italian,
    # French").
    r"(?:people|persons|passengers|migrants|survivors"
    r"|personnes?|passagers?|rescapée?s?"
    r"|persone|passeggeri"
    r"|personas?|pasajeros?"
    r"|menschen|personen)\b",
    re.I,
)


def _case_type(text: str, *, distress: bool, resolved: bool) -> str:
    """Classify into the canonical HumanitarianCaseType vocabulary.

    docs/fixes.md Phase 2.1 / sec 3.3: a finite, explicit set -- no ad-hoc
    strings. Unclassifiable non-distress reports go to the review lane.
    """
    value = re.sub(r"\s+", " ", text or "").lower()
    if re.search(r"\b(pushback|pushed back|forced back|forced return)\b", value):
        return HumanitarianCaseType.PUSHBACK.value
    if re.search(r"\b(intercept(?:ed|ion)|pulled back|libyan coast ?guard)\b", value):
        return HumanitarianCaseType.INTERCEPTION.value
    if re.search(
        r"\b(missing|where are they|loss of contact|lost contact|overdue|no contact)\b",
        value,
    ):
        return HumanitarianCaseType.MISSING.value
    if re.search(r"\b(evros|nea ?vyssa|neo ?cheimonio|border|reception cent(?:re|er)|reception camp|forest|land border)\b", value):
        return HumanitarianCaseType.LAND_HUMANITARIAN.value
    hopeful_outcome = bool(re.search(
        r"\b(?:we\s+)?(?:hope|pray|wish)(?:fully)?\b|\bhopefully\b", value
    ))
    if resolved or (not hopeful_outcome and re.search(
        r"\b(rescued|rescue completed|arrived safely|all safe|disembark(?:ed|ation)"
        r"|port of safety|safe port|brought to safety)\b",
        value,
    )):
        return HumanitarianCaseType.RESOLUTION.value
    if re.search(
        r"\b(rescue under ?way|rescue operation|proceeding (?:to|toward)|visual contact"
        r"|rhib launched|on scene)\b",
        value,
    ):
        return HumanitarianCaseType.RESCUE_UPDATE.value
    if distress:
        return HumanitarianCaseType.DISTRESS.value
    if re.search(
        r"\b(remember(?:ing)?|memorial|anniversary|commemorat|we demand|outrageous|shame"
        r"|one year on|victims of)\b",
        value,
    ):
        return HumanitarianCaseType.ADVOCACY.value
    if re.search(
        r"\b(shipwreck|capsiz(?:e|ed|ing)|sank|sunk|sinking|in distress|taking on water"
        r"|medical (?:evacuation|emergency)|medevac)\b",
        value,
    ):
        return HumanitarianCaseType.DISTRESS.value
    return HumanitarianCaseType.UNKNOWN_HUMANITARIAN.value


def canonical_classification(event: Any, *, same_source: Any = ()) -> dict[str, Any]:
    """Recompute the canonical IntelEventDB classification columns for a
    stored event, using the SAME helpers live ingestion / the public feed
    use -- no second taxonomy (docs/prompt.md sec 3 / Phase 2).

    Returns the seven canonical fields: maritime_domain, operational_tier,
    humanitarian_case_type, incident_lifecycle, location_status,
    coordinate_review_status, location_uncertainty_m. Never touches lat/lon.
    """
    from core.intel import lifecycle
    from core.intel.geoextract import is_direct_distress_call, is_resolved_distress
    from core.intel.location_evidence import (
        canonical_review_status,
        location_status_for,
    )

    meta = getattr(event, "metadata", {}) or {}
    text = str(getattr(event, "text", "") or getattr(event, "title", "") or "")
    is_distress = bool(meta.get("is_distress")) or is_direct_distress_call(text)
    resolved = is_resolved_distress(text) or str(meta.get("report_kind") or "") == "resolved"

    case_type = _case_type(text, distress=is_distress, resolved=resolved)
    incident_lifecycle = lifecycle.distress_lifecycle(
        event, now=datetime.now(UTC), same_source=list(same_source)
    )
    review = canonical_review_status(
        meta.get("coordinate_source"), meta.get("coordinate_review_status")
    )
    uncertainty = meta.get("location_uncertainty_m")
    try:
        uncertainty = float(uncertainty) if uncertainty is not None else None
    except (TypeError, ValueError):
        uncertainty = None

    location_status = location_status_for(
        lat=getattr(event, "lat", None),
        lon=getattr(event, "lon", None),
        coordinate_source=meta.get("coordinate_source"),
        coordinate_review_status=review,
        has_area_geometry=bool(meta.get("area_geojson")),
        is_land=case_type == HumanitarianCaseType.LAND_HUMANITARIAN.value,
    )
    return {
        "maritime_domain": event.maritime_domain(),
        "operational_tier": event.tier(),
        "humanitarian_case_type": case_type,
        "incident_lifecycle": incident_lifecycle,
        "location_status": location_status,
        "coordinate_review_status": review,
        "location_uncertainty_m": uncertainty,
    }


def humanitarian_case_metadata(
    text: str,
    *,
    incident_id: str,
    source: str,
    distress: bool,
    resolved: bool | None = None,
) -> dict[str, Any]:
    """Build the stable case projection without inventing unavailable facts."""
    resolved = is_resolved_distress(text) if resolved is None else resolved
    people = _PEOPLE.search(text or "")
    ongoing = distress or is_ongoing_incident(text)
    if resolved:
        status = "resolved"
    elif ongoing:
        status = "ongoing"
    else:
        status = "reported"
    direct_source = str(source).lower().lstrip("@") == "alarm_phone"
    return {
        "humanitarian_case_id": f"HUM-X-{incident_id}",
        "humanitarian_case_type": _case_type(text, distress=distress, resolved=resolved),
        "humanitarian_status": status,
        "people_reported": int(people.group("count")) if people else None,
        "people_precision": "approximate" if people and people.group("approx") else "exact" if people else "unknown",
        "verification_level": "direct_humanitarian_source" if direct_source else "single_public_source",
        "source_count": 1,
    }
