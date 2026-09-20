# SPDX-License-Identifier: AGPL-3.0-or-later
"""NAVAREA III official navigational warnings (Instituto Hidrográfico de la
Marina, Spain) -- the Western Mediterranean NAVAREA coordinator.

Distinct from core.mda.warfare.poll_navwarnings(), which already covers the
NGA-distributed NAVAREA IV/XII (Atlantic) broadcast-warning JSON API. This
module is the Mediterranean-basin counterpart: a different coordinator, a
different transport (in-force XML, not JSON), integrated into the same
SourceObservation/evidence architecture, not a parallel "news intelligence"
pipeline.

Caveat, stated plainly rather than hidden: this parser targets the general
shape IHM's public in-force NAVAREA III XML uses (a warnings/messages root
with one repeated element per in-force warning, carrying an id, issue date,
area, subject/category and free text with embedded DMM coordinates in the
same "DD-MM.mH DDD-MM.mH" style core.mda.warfare._extract_positions already
parses for NGA text). It has not been validated against a live pull of the
real feed in this environment (no outbound network access here) -- treat
field names as best-effort until checked against a real response, and keep
parse_navarea3_xml() defensive (never raise on an unexpected element/
attribute; skip that one warning, keep the rest).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from xml.etree.ElementTree import Element  # type only -- see ET import below

# defusedxml, not stdlib xml.etree, for actually parsing: this handles XML
# fetched from an external network source (untrusted input) -- stdlib
# ElementTree.fromstring is vulnerable to XXE/billion-laughs by default.
# defusedxml's fromstring() returns the same stdlib Element type, just
# built by a hardened parser, so the Element import above is safe to use
# for annotations only.
from defusedxml import ElementTree as ET

from core.mda.warfare import _extract_positions

logger = logging.getLogger(__name__)

NAVAREA3_SOURCE = "IHM NAVAREA III"
DEFAULT_NAVAREA3_URL = "https://ihm.es/es-es/AreasHidrograficas/NAVAREAIII/Paginas/EnVigor.aspx"

_GNSS_KW = re.compile(r"\b(gps|gnss|jamming|spoofing|interference)\b", re.I)
_FIRING_KW = re.compile(r"\b(firing exercise|gunnery|live fire|missile (test|firing)|naval exercise|ejercicio)\b", re.I)
_STRIKE_KW = re.compile(r"\b(missile|drone|uav|usv|unmanned|attack|explosion|mine)\b", re.I)
_OFFSHORE_KW = re.compile(r"\b(platform|rig|wellhead|survey|cable lay|pipeline|offshore operation)\b", re.I)
_SECURITY_KW = re.compile(r"\b(piracy|armed robbery|security incident|suspicious approach)\b", re.I)
_COMMS_KW = re.compile(r"\b(navtex|dsc|radio silence|communications? (status|outage))\b", re.I)

_WARNING_CATEGORIES = (
    "navigational_warning", "gnss_interference", "firing_exercise",
    "strike_warning", "offshore_operation", "maritime_security_notice",
    "communications_status",
)


@dataclass(frozen=True)
class Navarea3Warning:
    warning_id: str
    issued_at: str
    area: str
    subject: str
    text: str
    category: str
    lat: Optional[float]
    lon: Optional[float]
    coordinates: tuple[tuple[float, float], ...]


def _categorize(subject: str, text: str) -> str:
    combined = f"{subject} {text}"
    if _GNSS_KW.search(combined):
        return "gnss_interference"
    if _FIRING_KW.search(combined):
        return "firing_exercise"
    if _STRIKE_KW.search(combined):
        return "strike_warning"
    if _SECURITY_KW.search(combined):
        return "maritime_security_notice"
    if _COMMS_KW.search(combined):
        return "communications_status"
    if _OFFSHORE_KW.search(combined):
        return "offshore_operation"
    return "navigational_warning"


def _text_of(element: Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _first_present(warning_el: Element, *tags: str) -> Element | None:
    for tag in tags:
        found = warning_el.find(tag)
        if found is not None:
            return found
        # Namespaced feeds: fall back to a local-name match.
        for child in warning_el.iter():
            if child.tag.rsplit("}", 1)[-1].lower() == tag.lower():
                return child
    return None


def parse_navarea3_xml(xml_text: str) -> list[Navarea3Warning]:
    """Parse IHM's in-force NAVAREA III XML into structured warnings.

    Never raises on a malformed individual warning -- that one is skipped
    and parsing continues; the whole document only fails to parse if the
    XML itself is not well-formed (caller's poll function already treats
    any exception here as a graceful, logged no-op).
    """
    root = ET.fromstring(xml_text)
    warning_elements = [
        el for el in root.iter()
        if el.tag.rsplit("}", 1)[-1].lower() in {"warning", "message", "navwarning", "item"}
    ]
    warnings: list[Navarea3Warning] = []
    for el in warning_elements:
        try:
            warning_id = (
                el.get("id")
                or _text_of(_first_present(el, "id", "number", "messageNumber", "warningId"))
            ).strip()
            issued_at = _text_of(_first_present(el, "issueDate", "date", "published", "issued")).strip()
            area = _text_of(_first_present(el, "area", "navarea")).strip() or "NAVAREA III"
            subject = _text_of(_first_present(el, "subject", "title", "category")).strip()
            text = _text_of(_first_present(el, "text", "content", "description", "body")).strip()
            if not warning_id or not text:
                continue
            coordinates = tuple(_extract_positions(text))
            lat, lon = (coordinates[0] if coordinates else (None, None))
            warnings.append(Navarea3Warning(
                warning_id=warning_id,
                issued_at=issued_at,
                area=area,
                subject=subject,
                text=text,
                category=_categorize(subject, text),
                lat=lat,
                lon=lon,
                coordinates=coordinates,
            ))
        except Exception:
            logger.debug("navarea3: skipping one malformed warning element", exc_info=True)
            continue
    return warnings


def _record_source_observation(warning: Navarea3Warning) -> None:
    try:
        from core.db.session import session_scope
        from core.intel.source_observation import record_observation

        with session_scope() as db:
            record_observation(
                db,
                service="maritime", lane="safety", observation_type="official_navigational_warning",
                source_name=NAVAREA3_SOURCE, source_policy="official_site_embed",
                source_id=f"navarea3:{warning.warning_id}",
                observed_at=warning.issued_at or datetime.now(timezone.utc).isoformat(),
                raw_payload=warning.text, lat=warning.lat, lon=warning.lon,
            )
    except Exception as exc:
        logger.debug("navarea3: source_observation record skipped for %s: %s", warning.warning_id, exc)


def poll_navarea3(*, url: str = DEFAULT_NAVAREA3_URL, timeout_s: float = 30.0) -> int:
    """Fetch and ingest in-force NAVAREA III warnings.

    Graceful on outage (docs section 9): any network/parse failure is
    caught, logged at info level, and returns 0 -- never raises, never
    blocks the caller (the scheduler job wrapper). Refresh cadence (every
    30 minutes, faster than the heavy daily MDA batch) is set by the
    scheduler registration, not by this function.
    """
    from core.intel.store import IntelEvent, intel_store

    try:
        import httpx

        response = httpx.get(url, timeout=timeout_s)
        response.raise_for_status()
        warnings = parse_navarea3_xml(response.text)
    except Exception as exc:
        logger.info("navarea3 poll skipped: %s", exc)
        return 0

    ingested = 0
    for warning in warnings:
        eid = f"navarea3:{warning.warning_id}"
        _record_source_observation(warning)
        # Official warnings contextualize a case; they never become a
        # vessel accusation on their own (docs section 9) -- always
        # internal/context, never a public case by themselves.
        added = intel_store.add(IntelEvent(
            id=eid, type="navwarning",
            severity="high" if warning.category in {"strike_warning", "gnss_interference"} else "medium",
            lat=warning.lat, lon=warning.lon,
            title=f"NAVAREA III {warning.warning_id} — {warning.subject or warning.category}"[:255],
            text=warning.text[:600], source=NAVAREA3_SOURCE,
            timestamp_utc=warning.issued_at or datetime.now(timezone.utc).isoformat(),
            metadata={
                "anomaly_type": warning.category,
                "maritime_domain": "safety" if warning.category == "navigational_warning" else "grey_zone",
                "is_distress": False,
                "publication_status": "internal",
                "source_policy": "official_site_embed",
                "coordinate_source": "navarea3_text",
                "nav_area": warning.area,
                "warning_category": warning.category,
                "coordinates": [list(c) for c in warning.coordinates],
            },
        ), dedup_key=eid)
        ingested += 1 if added else 0
    if ingested:
        logger.info("navarea3: %d in-force warning(s) ingested", ingested)
    return ingested
