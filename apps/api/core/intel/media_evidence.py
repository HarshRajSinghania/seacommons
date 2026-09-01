# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alarm Phone media as a first-class evidence object (docs/prompt.md P1).

X media acquisition + OCR already work (twikit_monitor + x_media_utils). This
adds the durable, normalized evidence layer on top:

  - fetch the image once at ingestion, from an allow-listed host only, with
    redirects disabled, a private-IP guard, and a timeout / size / decoded-
    image guard (never arbitrary-URL fetch / SSRF);
  - keep the durable ORIGINAL as a PRIVATE evidence object, and a bounded
    re-encoded THUMBNAIL as the public derivative;
  - record a normalized ``media_evidence[]`` entry per image;
  - classify the outcome explicitly so a fetch failure, a blocked host, an
    oversize file, an undecodable image, "OCR ran and found nothing" and a
    real printed coordinate are never conflated.

``meta.media_urls`` is left untouched for backward compatibility. The
content-addressed public URL contains the sha256 by design; the raw
``sha256`` field is simply never projected into public Live JSON.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from core.intel.x_media_utils import _ALLOWED_MEDIA_HOSTS, _HEADERS, _MAX_IMAGE_BYTES

_FETCH_TIMEOUT_S = 15.0
_ORIG_PREFIX = "media/orig"          # private durable original
_PUB_PREFIX = "media/pub"            # public re-encoded thumbnail
_THUMB_MAX_PX = 1600                 # longest edge of the public thumbnail
_MAX_DECODED_PIXELS = 40_000_000     # decode-bomb guard (~40 MP)
_SUPPORTED = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}

# fetch_status
FETCH_OK = "ok"
FETCH_BLOCKED_HOST = "blocked_host"
FETCH_REDIRECT_BLOCKED = "redirect_blocked"
FETCH_PRIVATE_IP_BLOCKED = "private_ip_blocked"
FETCH_TOO_LARGE = "too_large"
FETCH_WRONG_TYPE = "wrong_type"
FETCH_FAILED = "fetch_failed"
FETCH_INVALID_IMAGE = "invalid_image"
FETCH_NOT_ATTEMPTED = "not_attempted"

# ocr_status (per-image)
OCR_COORDINATES_FOUND = "coordinates_found"
OCR_NO_COORDINATE = "no_coordinate"   # OCR ran, read no coordinate
OCR_PIN_ONLY = "pin_only"
OCR_EXECUTION_FAILED = "execution_failed"   # OCR could not run at all
OCR_NOT_RUN = "not_run"

# event-level media outcome (docs/prompt.md sec 3)
NO_MEDIA = "no_media"
MEDIA_FETCH_FAILED = "media_fetch_failed"
MEDIA_BLOCKED = "media_blocked"
MEDIA_TOO_LARGE = "media_too_large"
MEDIA_INVALID = "media_invalid"
MEDIA_STORED_NO_LOCATION = "media_stored_no_location"
OCR_FAILED = "ocr_failed"
MEDIA_COORDINATES_FOUND = "media_coordinates_found"
VISUAL_PIN_ONLY = "visual_pin_only"


@dataclass
class MediaEvidence:
    source_url: str | None = None
    original_media_url: str | None = None
    stored_media_url: str | None = None      # /api/v1/media/<sha>.<ext> -- PUBLIC thumbnail
    sha256: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    fetch_status: str = FETCH_NOT_ATTEMPTED
    fetched_at: str | None = None
    ocr_status: str = OCR_NOT_RUN
    ocr_engine: str | None = None
    ocr_text: str | None = None
    coordinate_candidates: list[dict[str, Any]] = field(default_factory=list)
    location_method: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """The subset safe to expose on the public Live feed / edge.

        No ``sha256`` (even though the public URL is content-addressed), no
        raw ``ocr_text``, no ``coordinate_candidates``, no private original.
        """
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # noqa: D401
        return None  # never follow a redirect -- see _fetch()


_OPENER = urllib.request.build_opener(_NoRedirect)


def _host_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme == "https" and p.hostname in _ALLOWED_MEDIA_HOSTS


def _resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _fetch(url: str) -> tuple[bytes | None, str]:
    """Bounded, allow-listed, no-redirect image fetch.

    Returns (bytes|None, fetch_status). A 3xx response is a hard block -- the
    only allowed host does not legitimately redirect its media, and following
    one is an SSRF vector. The resolved host must also be a public IP.
    """
    if not _host_allowed(url):
        return None, FETCH_BLOCKED_HOST
    host = urlparse(url).hostname or ""
    if not _resolves_public(host):
        return None, FETCH_PRIVATE_IP_BLOCKED
    request = urllib.request.Request(
        url, headers={**_HEADERS, "Accept": "image/jpeg,image/png,image/webp,image/gif"}
    )
    try:
        response = _OPENER.open(request, timeout=_FETCH_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return None, FETCH_REDIRECT_BLOCKED
        return None, FETCH_FAILED
    except Exception:
        return None, FETCH_FAILED
    try:
        # Defence in depth: the final URL must still be the allow-listed host.
        if not _host_allowed(response.geturl()):
            return None, FETCH_REDIRECT_BLOCKED
        ctype = str(response.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("image/"):
            return None, FETCH_WRONG_TYPE
        payload = response.read(_MAX_IMAGE_BYTES + 1)
    except Exception:
        return None, FETCH_FAILED
    finally:
        response.close()
    if len(payload) > _MAX_IMAGE_BYTES:
        return None, FETCH_TOO_LARGE
    return payload, FETCH_OK


def _validate_image_dims(w: int, h: int) -> bool:
    return w > 0 and h > 0 and (w * h) <= _MAX_DECODED_PIXELS


def _validate_image(data: bytes) -> tuple[str, str, int, int] | None:
    """Decode-verify with Pillow. Returns (format, mime, w, h) or None.

    Content-Type from the server is never trusted -- only an actually
    decodable, explicitly-supported image passes.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            if fmt not in _SUPPORTED:
                return None
            w, h = im.size
            if not _validate_image_dims(w, h):
                return None
            im.load()
        mime, _ext = _SUPPORTED[fmt]
        return fmt, mime, w, h
    except Exception:
        return None


def _thumbnail(data: bytes, fmt: str) -> tuple[bytes, str, str] | None:
    """A bounded, re-encoded public derivative. Returns (bytes, mime, ext)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im.thumbnail((_THUMB_MAX_PX, _THUMB_MAX_PX))
            out = io.BytesIO()
            if fmt == "PNG":
                im.save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png", ".png"
            im.save(out, format="JPEG", quality=82, optimize=True)
            return out.getvalue(), "image/jpeg", ".jpg"
    except Exception:
        return None


def _store(prefix: str, key: str, data: bytes, mime: str) -> bool:
    try:
        from core.object_store import put

        put(f"{prefix}/{key}", data, mime)
        return True
    except Exception:
        return False


def capture_media_evidence(
    media_urls: list[str],
    *,
    source_url: str | None = None,
    ocr_method: str | None = None,
    ocr_coord: tuple[float, float] | None = None,
    ocr_engine: str | None = None,
    ocr_text: str | None = None,
    ocr_ran: bool = True,
    interengine_distance_m: float | None = None,
    max_images: int = 4,
) -> list[MediaEvidence]:
    """One MediaEvidence per image. Runs inside the bounded media-OCR pool
    worker (never a per-event thread) -- see twikit_monitor._schedule_media_ocr.
    """
    out: list[MediaEvidence] = []
    for url in list(media_urls)[:max_images]:
        ev = MediaEvidence(source_url=source_url, original_media_url=url)
        try:
            data, ev.fetch_status = _fetch(url)
            if data is not None:
                validated = _validate_image(data)
                if validated is None:
                    ev.fetch_status = FETCH_INVALID_IMAGE
                else:
                    fmt, ev.mime_type, ev.width, ev.height = validated
                    ev.sha256 = hashlib.sha256(data).hexdigest()
                    ev.fetched_at = datetime.now(UTC).isoformat()
                    _ext = _SUPPORTED[fmt][1]
                    _store(_ORIG_PREFIX, f"{ev.sha256}{_ext}", data, ev.mime_type)
                    thumb = _thumbnail(data, fmt)
                    if thumb is not None:
                        tbytes, tmime, text = thumb
                        if _store(_PUB_PREFIX, f"{ev.sha256}{text}", tbytes, tmime):
                            ev.stored_media_url = f"/api/v1/media/{ev.sha256}{text}"
        except Exception:  # a single bad image must never sink the whole event
            ev.fetch_status = FETCH_FAILED
        out.append(ev)

    _attach_ocr_outcome(out, ocr_method, ocr_coord, ocr_engine, ocr_text,
                        ocr_ran, interengine_distance_m)
    return out


def _attach_ocr_outcome(
    evidence: list[MediaEvidence],
    method: str | None,
    coord: tuple[float, float] | None,
    engine: str | None,
    text: str | None,
    ocr_ran: bool,
    interengine_distance_m: float | None,
) -> None:
    if not evidence:
        return
    target = next((e for e in evidence if e.fetch_status == FETCH_OK), evidence[0])
    m = (method or "").lower()
    target.ocr_engine = engine
    target.location_method = method or None
    if text:
        target.ocr_text = text[:120]
    if coord is not None and m.endswith("pin_landmark"):
        target.ocr_status = OCR_PIN_ONLY
        target.coordinate_candidates = [{
            "lat": round(coord[0], 5), "lon": round(coord[1], 5),
            "method": method, "approximate": True,
        }]
    elif coord is not None:
        target.ocr_status = OCR_COORDINATES_FOUND
        target.coordinate_candidates = [{
            "lat": round(coord[0], 5), "lon": round(coord[1], 5), "method": method,
            **({"interengine_distance_m": interengine_distance_m}
               if interengine_distance_m is not None else {}),
        }]
    elif ocr_ran:
        target.ocr_status = OCR_NO_COORDINATE
    else:
        target.ocr_status = OCR_EXECUTION_FAILED


_FETCH_STATUS_TO_OUTCOME = {
    FETCH_BLOCKED_HOST: MEDIA_BLOCKED,
    FETCH_REDIRECT_BLOCKED: MEDIA_BLOCKED,
    FETCH_PRIVATE_IP_BLOCKED: MEDIA_BLOCKED,
    FETCH_TOO_LARGE: MEDIA_TOO_LARGE,
    FETCH_WRONG_TYPE: MEDIA_INVALID,
    FETCH_INVALID_IMAGE: MEDIA_INVALID,
    FETCH_FAILED: MEDIA_FETCH_FAILED,
}


def classify_media_outcome(
    evidence: list[MediaEvidence],
    ocr_coord: tuple[float, float] | None,
    method: str | None,
    *,
    ocr_ran: bool = True,
) -> str:
    """docs/prompt.md sec 3 -- one explicit event-level bucket, no collapse."""
    m = (method or "").lower()
    if not evidence:
        return NO_MEDIA
    # "stored" == we hold a durable copy of the original (sha256 set). The
    # public thumbnail is a rendering concern, not evidence completeness.
    stored = [e for e in evidence if e.sha256]
    if not stored:
        # Report the first concrete failure reason, not a generic bucket.
        for e in evidence:
            mapped = _FETCH_STATUS_TO_OUTCOME.get(e.fetch_status)
            if mapped:
                return mapped
        return MEDIA_FETCH_FAILED
    if ocr_coord is not None and m.endswith("pin_landmark"):
        return VISUAL_PIN_ONLY
    if ocr_coord is not None:
        return MEDIA_COORDINATES_FOUND
    if not ocr_ran:
        return OCR_FAILED
    return MEDIA_STORED_NO_LOCATION
