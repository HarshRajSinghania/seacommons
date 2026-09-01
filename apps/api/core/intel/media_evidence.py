# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alarm Phone media as a first-class evidence object (docs/prompt.md P1).

X media acquisition + OCR already work (twikit_monitor + x_media_utils). This
adds the durable, normalized evidence layer on top:

  - download the image once at ingestion, from an allow-listed host only,
    with a timeout / size / MIME guard (never arbitrary-URL fetch / SSRF);
  - keep a durable copy in the object store so a historical event does not
    depend forever on a pbs.twimg.com URL;
  - record a normalized ``media_evidence[]`` entry per image;
  - classify the outcome explicitly so OCR failure, a pin-only map and a
    real printed coordinate are never conflated.

``meta.media_urls`` is left untouched for backward compatibility.
"""
from __future__ import annotations

import hashlib
import io
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from core.intel.x_media_utils import _ALLOWED_MEDIA_HOSTS, _HEADERS, _MAX_IMAGE_BYTES

_FETCH_TIMEOUT_S = 15.0
_MEDIA_KEY_PREFIX = "media"

# fetch_status
FETCH_OK = "ok"
FETCH_BLOCKED_HOST = "blocked_host"
FETCH_TOO_LARGE = "too_large"
FETCH_WRONG_TYPE = "wrong_type"
FETCH_FAILED = "fetch_failed"
FETCH_NOT_ATTEMPTED = "not_attempted"

# ocr_status (per-image)
OCR_COORDINATES_FOUND = "coordinates_found"
OCR_NO_COORDINATE = "no_coordinate"
OCR_PIN_ONLY = "pin_only"
OCR_NOT_RUN = "not_run"
OCR_ERROR = "error"

# event-level media outcome (docs/prompt.md sec 6)
MEDIA_COORDINATES_FOUND = "media_coordinates_found"
VISUAL_PIN_ONLY = "visual_pin_only"
OCR_FAILED = "ocr_failed"
MEDIA_NO_LOCATION = "media_no_location"


@dataclass
class MediaEvidence:
    source_url: str | None = None            # the X post the image came from
    original_media_url: str | None = None    # the pbs.twimg.com URL
    stored_media_url: str | None = None      # /api/v1/media/<sha256> (durable copy)
    sha256: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    fetch_status: str = FETCH_NOT_ATTEMPTED
    fetched_at: str | None = None
    ocr_status: str = OCR_NOT_RUN
    ocr_engine: str | None = None
    ocr_text: str | None = None              # bounded raw coordinate span only
    coordinate_candidates: list[dict[str, Any]] = field(default_factory=list)
    location_method: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """The subset safe to expose on the public Live feed / edge."""
        return {
            "stored_media_url": self.stored_media_url,
            "source_url": self.source_url,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "fetch_status": self.fetch_status,
            "ocr_status": self.ocr_status,
            "ocr_engine": self.ocr_engine,
            "location_method": self.location_method,
        }


def _host_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme == "https" and p.hostname in _ALLOWED_MEDIA_HOSTS


def _image_dims_and_mime(data: bytes) -> tuple[int | None, int | None, str | None]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").lower()
            mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                    "gif": "image/gif"}.get(fmt)
            return im.width, im.height, mime
    except Exception:
        return None, None, None


def _fetch(url: str) -> tuple[bytes | None, str]:
    """Bounded, allow-listed image fetch. Returns (bytes|None, fetch_status)."""
    if not _host_allowed(url):
        return None, FETCH_BLOCKED_HOST
    request = urllib.request.Request(
        url, headers={**_HEADERS, "Accept": "image/jpeg,image/png,image/webp"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
            ctype = str(response.headers.get("Content-Type") or "").lower()
            if not ctype.startswith("image/"):
                return None, FETCH_WRONG_TYPE
            payload = response.read(_MAX_IMAGE_BYTES + 1)
    except Exception:
        return None, FETCH_FAILED
    if len(payload) > _MAX_IMAGE_BYTES:
        return None, FETCH_TOO_LARGE
    return payload, FETCH_OK


def _store_durable(data: bytes, mime: str | None) -> str | None:
    sha = hashlib.sha256(data).hexdigest()
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/gif": ".gif"}.get(mime or "", "")
    key = f"{_MEDIA_KEY_PREFIX}/{sha}{ext}"
    try:
        from core.object_store import put

        put(key, data, mime or "application/octet-stream")
    except Exception:
        return None
    return f"/api/v1/media/{sha}{ext}"


def capture_media_evidence(
    media_urls: list[str],
    *,
    source_url: str | None = None,
    ocr_method: str | None = None,
    ocr_coord: tuple[float, float] | None = None,
    ocr_engine: str | None = None,
    ocr_text: str | None = None,
    interengine_distance_m: float | None = None,
    max_images: int = 4,
) -> list[MediaEvidence]:
    """One MediaEvidence per image: download, durably store, and record the
    OCR outcome that ``_apply_media_ocr`` already computed.
    """
    out: list[MediaEvidence] = []
    for url in list(media_urls)[:max_images]:
        ev = MediaEvidence(source_url=source_url, original_media_url=url)
        data, ev.fetch_status = _fetch(url)
        if data is not None:
            ev.sha256 = hashlib.sha256(data).hexdigest()
            ev.width, ev.height, ev.mime_type = _image_dims_and_mime(data)
            ev.fetched_at = datetime.now(UTC).isoformat()
            ev.stored_media_url = _store_durable(data, ev.mime_type)
        out.append(ev)

    _attach_ocr_outcome(out, ocr_method, ocr_coord, ocr_engine, ocr_text, interengine_distance_m)
    return out


def _attach_ocr_outcome(
    evidence: list[MediaEvidence],
    method: str | None,
    coord: tuple[float, float] | None,
    engine: str | None,
    text: str | None,
    interengine_distance_m: float | None,
) -> None:
    if not evidence:
        return
    # The OCR ran over the whole image set and produced (at most) one result;
    # attribute it to the first successfully fetched image.
    target = next((e for e in evidence if e.fetch_status == FETCH_OK), evidence[0])
    m = (method or "").lower()
    target.ocr_engine = engine
    target.location_method = method or None
    if text:
        target.ocr_text = text[:120]
    if coord is not None and not m.endswith("pin_landmark"):
        target.ocr_status = OCR_COORDINATES_FOUND
        target.coordinate_candidates = [{
            "lat": round(coord[0], 5), "lon": round(coord[1], 5), "method": method,
            **({"interengine_distance_m": interengine_distance_m}
               if interengine_distance_m is not None else {}),
        }]
    elif coord is not None and m.endswith("pin_landmark"):
        target.ocr_status = OCR_PIN_ONLY
        target.coordinate_candidates = [{
            "lat": round(coord[0], 5), "lon": round(coord[1], 5),
            "method": method, "approximate": True,
        }]
    elif method in (None, "", "none"):
        target.ocr_status = OCR_NO_COORDINATE
    else:
        target.ocr_status = OCR_NO_COORDINATE


def classify_media_outcome(
    evidence: list[MediaEvidence],
    ocr_coord: tuple[float, float] | None,
    method: str | None,
) -> str:
    """docs/prompt.md sec 6 -- a single explicit event-level bucket."""
    m = (method or "").lower()
    if not evidence:
        return MEDIA_NO_LOCATION
    fetched = [e for e in evidence if e.fetch_status == FETCH_OK]
    if not fetched:
        return MEDIA_NO_LOCATION
    if ocr_coord is not None and m.endswith("pin_landmark"):
        return VISUAL_PIN_ONLY
    if ocr_coord is not None:
        return MEDIA_COORDINATES_FOUND
    return OCR_FAILED
