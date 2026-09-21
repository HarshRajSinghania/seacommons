# SPDX-License-Identifier: AGPL-3.0-or-later
"""Official NAVAREA III in-force navigational warnings from IHM Spain.

The operational feed is IHM's public ``navareas_crudo.xml`` document. Its
actual schema is a ``dataroot`` containing repeated ``NAVAREASVIGOR`` rows
with Access-style field names (nnunaf/nfemi/nlocai/nasuni/ntein). Warnings
are stored as immutable SourceObservation revisions and one stable,
updateable IntelEvent per official warning id. They provide context/evidence
only and never create a vessel allegation by themselves.
"""
from __future__ import annotations

import hashlib
import json
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
DEFAULT_NAVAREA3_URL = "https://armada.defensa.gob.es/ihm/XML/navareas_crudo.xml"
NAVAREA3_REFERENCE_URL = "https://armada.defensa.gob.es/ihm/Aplicaciones/Navareas/Index_Navareas_xml_en.html"

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
    source_record_id: str
    issued_at: str
    area: str
    subject: str
    text: str
    category: str
    lat: Optional[float]
    lon: Optional[float]
    coordinates: tuple[tuple[float, float], ...]
    snapshot_generated: str = ""


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
    """Parse the real IHM ``NAVAREASVIGOR`` schema defensively.

    A small generic fallback remains for archived fixtures/tools, but the
    operational mapping is based on the live official feed verified in 2026.
    Malformed individual records are skipped without discarding the document.
    """
    root = ET.fromstring(xml_text)
    snapshot_generated = str(root.get("generated") or "").strip()
    warning_elements = [
        el for el in root.iter()
        if el.tag.rsplit("}", 1)[-1].lower() == "navareasvigor"
    ]
    real_schema = bool(warning_elements)
    if not warning_elements:
        warning_elements = [
            el for el in root.iter()
            if el.tag.rsplit("}", 1)[-1].lower()
            in {"warning", "message", "navwarning", "item"}
        ]

    warnings: list[Navarea3Warning] = []
    for el in warning_elements:
        try:
            if real_schema:
                source_record_id = _text_of(_first_present(el, "nnuna")).strip()
                warning_id = (
                    _text_of(_first_present(el, "nnunaf")).strip()
                    or source_record_id
                )
                issued_at = _text_of(_first_present(el, "nfemi")).strip()
                area = (
                    _text_of(_first_present(el, "nlocai")).strip()
                    or _text_of(_first_present(el, "nlocae")).strip()
                    or "NAVAREA III"
                )
                subject = (
                    _text_of(_first_present(el, "nasuni")).strip()
                    or _text_of(_first_present(el, "nasun")).strip()
                )
                text = (
                    _text_of(_first_present(el, "ntein")).strip()
                    or _text_of(_first_present(el, "ntees")).strip()
                )
            else:
                warning_id = (
                    el.get("id")
                    or _text_of(_first_present(el, "id", "number", "messageNumber", "warningId"))
                ).strip()
                source_record_id = warning_id
                issued_at = _text_of(_first_present(el, "issueDate", "date", "published", "issued")).strip()
                area = _text_of(_first_present(el, "area", "navarea")).strip() or "NAVAREA III"
                subject = _text_of(_first_present(el, "subject", "title", "category")).strip()
                text = _text_of(_first_present(el, "text", "content", "description", "body")).strip()
            if not warning_id or not text:
                continue
            coordinates = tuple(_extract_positions(text))
            lat, lon = coordinates[0] if coordinates else (None, None)
            warnings.append(Navarea3Warning(
                warning_id=warning_id,
                source_record_id=source_record_id or warning_id,
                issued_at=issued_at,
                area=area,
                subject=subject,
                text=text,
                category=_categorize(subject, text),
                lat=lat,
                lon=lon,
                coordinates=coordinates,
                snapshot_generated=snapshot_generated,
            ))
        except Exception:
            logger.debug("navarea3: skipping one malformed warning element", exc_info=True)
            continue
    return warnings


def _record_source_observation(
    warning: Navarea3Warning, *, source_url: str = DEFAULT_NAVAREA3_URL
) -> None:
    try:
        from core.db.session import session_scope
        from core.intel.source_observation import record_observation

        payload = {
            "warning_id": warning.warning_id,
            "source_record_id": warning.source_record_id,
            "issued_at": warning.issued_at,
            "area": warning.area,
            "subject": warning.subject,
            "text": warning.text,
            "category": warning.category,
            "coordinates": [list(value) for value in warning.coordinates],
            "snapshot_generated": warning.snapshot_generated,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        revision = hashlib.blake2s(encoded.encode("utf-8"), digest_size=10).hexdigest()
        with session_scope() as db:
            record_observation(
                db,
                service="maritime", lane="safety", observation_type="official_navigational_warning",
                source_name=NAVAREA3_SOURCE, source_policy="official_xml_feed",
                source_id=f"navarea3:{warning.warning_id}:{revision}",
                source_url=source_url,
                raw_payload_ref=NAVAREA3_REFERENCE_URL,
                observed_at=warning.issued_at or datetime.now(timezone.utc).isoformat(),
                raw_payload=encoded, lat=warning.lat, lon=warning.lon,
                provenance={
                    "official_warning_id": warning.warning_id,
                    "source_record_id": warning.source_record_id,
                    "snapshot_generated": warning.snapshot_generated,
                    "feed_url": source_url,
                },
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
        from core.net.outbound import FixedOriginClient
        from core.net.policy import JSON_TEXT

        client = FixedOriginClient(("https://armada.defensa.gob.es",), timeout=timeout_s)
        response = client.request(url, contract=JSON_TEXT)
        response.raise_for_status()
        warnings = parse_navarea3_xml(response.body.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.info("navarea3 poll skipped: %s", exc)
        return 0

    ingested = 0
    for warning in warnings:
        eid = f"navarea3:{warning.warning_id}"
        _record_source_observation(warning, source_url=url)
        # Official warnings contextualize a case; they never become a
        # vessel accusation on their own (docs section 9) -- always
        # internal/context, never a public case by themselves.
        added = intel_store.add(IntelEvent(
            id=eid, type="navwarning",
            severity="high" if warning.category in {"strike_warning", "gnss_interference"} else "medium",
            lat=warning.lat, lon=warning.lon,
            title=f"NAVAREA III {warning.warning_id} — {warning.subject or warning.category}"[:255],
            text=warning.text[:600], source=NAVAREA3_SOURCE,
            url=NAVAREA3_REFERENCE_URL,
            timestamp_utc=warning.issued_at or datetime.now(timezone.utc).isoformat(),
            metadata={
                "anomaly_type": warning.category,
                "maritime_domain": "safety" if warning.category == "navigational_warning" else "grey_zone",
                "is_distress": False,
                "publication_status": "internal",
                "source_policy": "official_xml_feed",
                "coordinate_source": "navarea3_text",
                "nav_area": warning.area,
                "warning_category": warning.category,
                "official_warning_id": warning.warning_id,
                "source_record_id": warning.source_record_id,
                "snapshot_generated": warning.snapshot_generated,
                "feed_url": url,
                "coordinates": [list(c) for c in warning.coordinates],
            },
        ), dedup_key=eid)
        ingested += 1 if added else 0
    if ingested:
        logger.info("navarea3: %d in-force warning(s) ingested", ingested)
    return ingested
