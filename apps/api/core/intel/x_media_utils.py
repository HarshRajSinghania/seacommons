# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared X/Twitter media utilities: Snowflake ID decoding and image OCR.

Split out of the old alarm_phone_monitor.py (the first-party alarmphone.org
site scraper, removed once twikit became the sole, stable X source) because
twikit_monitor.py's own image-based coordinate extraction depends on the OCR
pipeline here — these functions are source-agnostic, not specific to any one
collector.
"""
from __future__ import annotations

import io
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from core.intel.geoextract import extract_numeric_coords
from core.net.outbound import FixedOriginClient
from core.net.policy import IMAGE, JSON_TEXT, OutboundError

_X_EPOCH_MS = 1_288_834_974_657
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_ALLOWED_MEDIA_HOSTS = frozenset({"pbs.twimg.com"})
_MEDIA_ORIGINS = tuple(f"https://{host}" for host in sorted(_ALLOWED_MEDIA_HOSTS))
_SYNDICATION_ORIGINS = ("https://cdn.syndication.twimg.com",)

# docs/fixes.md F-03: OCR agreement is a physical distance, not a degree
# delta. 0.03 deg of latitude is ~3.3 km and a degree of longitude shrinks
# toward the poles, so the old test both overstated agreement and varied with
# latitude. Compare candidates geodesically in metres instead, and persist the
# measured distance + the threshold used. Conservative starting values, env-
# tunable, to be calibrated against the regression corpus.
OCR_CONSENSUS_MAX_DISTANCE_M = float(os.getenv("OCR_CONSENSUS_MAX_DISTANCE_M", "500"))
OCR_CROSS_ENGINE_MAX_DISTANCE_M = float(
    os.getenv("OCR_CROSS_ENGINE_MAX_DISTANCE_M", "500")
)


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, in metres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    d_lat, d_lon = lat2 - lat1, lon2 - lon1
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))
_HEADERS = {
    "User-Agent": "SeaCommonsIntel/2.0 (+https://seacommons.org)",
    "Accept": "text/html,application/xhtml+xml",
}
_EASYOCR_READER: Any = None
_EASYOCR_LOCK = threading.Lock()
# Each media OCR job runs in its own background thread (twikit_monitor's
# _schedule_media_ocr) and tesseract runs as external subprocesses -- with no
# cap, several images landing at once each spawn their own 6-variant sweep in
# parallel. Verified live: on the pilot VM's 2 vCPUs this pushed
# /api/v1/live/signals response time from ~4s to 9-22s (CPU contention, not
# the DB -- the query itself is sub-millisecond). Same one-at-a-time
# discipline _EASYOCR_LOCK already applies to the neural OCR pass.
_TESSERACT_LOCK = threading.Lock()


def _easyocr_image(payload: bytes) -> tuple[Optional[tuple[float, float]], list[dict], bool]:
    """Canonical neural OCR pass; returns coordinate + positioned text boxes.

    EasyOCR is primary because it detects small text regions before reading
    them. Tesseract remains the explicit legacy fallback for hosts where the
    model package/weights are unavailable.
    """
    global _EASYOCR_READER
    try:
        import easyocr  # type: ignore[import-untyped]
        import numpy as np
        from PIL import Image, ImageOps
    except Exception:
        return None, [], False
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    except Exception:
        return None, [], True
    try:
        # Reader inference is not thread-safe and Twikit can enqueue several
        # images during its startup catch-up poll.  Serialising both model
        # creation and readtext prevents transient failures/large parallel
        # Torch allocations from disabling the primary OCR path.
        with _EASYOCR_LOCK:
            if _EASYOCR_READER is None:
                _EASYOCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
            results = _EASYOCR_READER.readtext(
                image,
                detail=1,
                paragraph=False,
                min_size=8,
                text_threshold=0.45,
                low_text=0.25,
                canvas_size=3200,
                mag_ratio=1.5,
            )
    except Exception:
        # A corrupt/unsupported image or transient inference error must not
        # permanently disable EasyOCR for every later incident in the process.
        # Import/model construction failures are retried on the next image;
        # Tesseract remains the compatibility fallback for this one.
        return None, [], True

    texts: list[str] = []
    boxes: list[dict] = []
    for index, item in enumerate(results):
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        polygon, raw_text, confidence = item[0], str(item[1] or "").strip(), float(item[2] or 0)
        if not raw_text or confidence < 0.20:
            continue
        texts.append(raw_text)
        try:
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
        except (TypeError, ValueError, IndexError):
            continue
        boxes.append({
            "text": raw_text,
            "confidence": round(confidence, 3),
            "left": round(min(xs)),
            "top": round(min(ys)),
            "width": max(1, round(max(xs) - min(xs))),
            "height": max(1, round(max(ys) - min(ys))),
            "block": "easyocr",
            "par": "1",
            "line": str(index + 1),
            "word": 1,
        })
    combined = "\n".join(texts)
    return extract_numeric_coords(combined), boxes, True


def x_id_timestamp(tweet_id: str) -> str:
    """Recover the exact UTC creation time encoded in an X Snowflake ID."""
    milliseconds = (int(tweet_id) >> 22) + _X_EPOCH_MS
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def _x_photo_urls(payload: dict[str, Any]) -> list[str]:
    """Every pbs.twimg.com photo URL in a syndication tweet-result payload,
    including its quoted tweet (Alarm Phone often puts the map in the quote)."""
    urls: list[str] = []

    def _harvest(node: dict[str, Any]) -> None:
        for item in (node.get("mediaDetails") or []):
            candidate = str(item.get("media_url_https") or item.get("media_url") or "")
            if candidate:
                urls.append(candidate)
        for photo in (node.get("photos") or []):
            candidate = str(photo.get("url") or "")
            if candidate:
                urls.append(candidate)

    _harvest(payload)
    quoted = payload.get("quoted_tweet")
    if isinstance(quoted, dict):
        _harvest(quoted)

    seen: set[str] = set()
    clean: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname in _ALLOWED_MEDIA_HOSTS
            and url not in seen
        ):
            seen.add(url)
            clean.append(url)
    return clean[:6]


def _syndication_token(tweet_id: str) -> str:
    """The token the public tweet-result CDN expects:
    ((id / 1e15) * pi).toString(36), with zero-runs and the dot removed."""
    value = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole = int(value)
    frac = value - whole
    out = ""
    while whole > 0:
        out = digits[whole % 36] + out
        whole //= 36
    out = out or "0"
    if frac > 0:
        out += "."
        for _ in range(24):
            frac *= 36
            digit = int(frac)
            out += digits[digit]
            frac -= digit
    return re.sub(r"(0+|\.)", "", out)


def fetch_tweet_photos(tweet_id: str, *, timeout: float = 12.0) -> list[str]:
    """Public pbs.twimg.com photo URLs for a tweet, via the syndication CDN --
    no account, no API key. Used to re-process historical events whose image
    URLs were never stored. Returns [] on any failure."""
    tweet_id = str(tweet_id).strip()
    if not tweet_id.isdigit():
        return []
    url = (
        "https://cdn.syndication.twimg.com/tweet-result"
        f"?id={tweet_id}&token={_syndication_token(tweet_id)}&lang=en"
    )
    try:
        response = FixedOriginClient(_SYNDICATION_ORIGINS, timeout=timeout).request(
            url,
            contract=JSON_TEXT,
            headers={"User-Agent": _HEADERS["User-Agent"], "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (OutboundError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    return _x_photo_urls(payload)


def consensus_ocr_coordinate(texts: list[str]) -> Optional[tuple[float, float]]:
    """Accept an OCR location only when two independent layout passes agree.

    Clusters the per-pass candidates by geodesic proximity (F-03: metres, not
    a degree delta) rather than testing exact pairs, so one bad digit in a
    third pass no longer blocks a clear two-pass agreement; returns the median
    of the largest cluster once it holds at least two candidates.
    """
    candidates = [
        candidate
        for text in texts
        if (candidate := extract_numeric_coords(text)) is not None
    ]
    cluster = _largest_agreeing_cluster(candidates, tol_m=OCR_CONSENSUS_MAX_DISTANCE_M)
    if len(cluster) >= 2:
        lats = sorted(c[0] for c in cluster)
        lons = sorted(c[1] for c in cluster)
        mid = len(cluster) // 2
        return round(lats[mid], 5), round(lons[mid], 5)
    return None


def _largest_agreeing_cluster(
    candidates: list[tuple[float, float]], *, tol_m: float
) -> list[tuple[float, float]]:
    best: list[tuple[float, float]] = []
    for anchor in candidates:
        group = [other for other in candidates if haversine_m(other, anchor) <= tol_m]
        if len(group) > len(best):
            best = group
    return best


# Characters a coordinate readout can contain — a whitelist makes Tesseract
# far more accurate on the small text of a map label.
_COORD_WHITELIST = "0123456789.,'\"NSEWnsew:/-()[] "


def ocr_png_coordinate(
    processed_png: bytes,
    *,
    executable: Optional[str] = None,
) -> tuple[Optional[tuple[float, float]], bool]:
    """Run independent Tesseract layouts over an already-normalised PNG."""
    command = executable or shutil.which("tesseract")
    if not command:
        return None, False
    texts: list[str] = []
    # (psm, restrict-to-coordinate-characters). The whitelisted passes read
    # digits off a map label far more reliably; the open passes still catch
    # the "Position:" prefix and any degree glyph.
    variants = (
        ("3", False), ("6", False), ("11", False),
        ("6", True), ("7", True), ("11", True),
    )
    with _TESSERACT_LOCK:
        for page_mode, restrict in variants:
            cmd = [command, "stdin", "stdout", "--psm", page_mode, "--oem", "1", "-l", "eng"]
            if restrict:
                cmd += ["-c", f"tessedit_char_whitelist={_COORD_WHITELIST}"]
            try:
                result = subprocess.run(
                    cmd, input=processed_png, capture_output=True, check=False, timeout=20
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                texts.append(result.stdout.decode("utf-8", errors="replace")[:20_000])
    consensus = consensus_ocr_coordinate(texts)
    if consensus is not None:
        return consensus, True
    # A tightly cropped popup may be legible in only one Tesseract layout.
    # Accept it as explicitly unverified only when exactly one valid,
    # hemisphere-labelled coordinate survives the strict range parser.
    candidates = {
        candidate for text in texts
        if (candidate := extract_numeric_coords(text)) is not None
    }
    return (next(iter(candidates)) if len(candidates) == 1 else None), True


def _tesseract_cross_check(payload: bytes, executable: str) -> Optional[tuple[float, float]]:
    """One cheap Tesseract read used only to validate an EasyOCR coordinate.

    Not a blind search — the caller already has a candidate. A single scaled/
    sharpened full-image pass through the same multi-PSM consensus
    (ocr_png_coordinate) is enough to confirm or dispute it, far cheaper than
    the popup-detection + multi-crop sweep _ocr_photo runs only when EasyOCR
    found nothing at all.
    """
    from PIL import Image, ImageFilter, ImageOps

    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            scale = min(4, max(1, math.ceil(2400 / max(1, image.width))))
            if scale > 1:
                image = image.resize(
                    (image.width * scale, image.height * scale), Image.Resampling.LANCZOS
                )
            image = ImageOps.autocontrast(image).filter(ImageFilter.SHARPEN)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
    except Exception:
        return None
    coordinate, _attempted = ocr_png_coordinate(output.getvalue(), executable=executable)
    return coordinate


def _download_bounded_image(url: str) -> Optional[bytes]:
    """Fetch one allow-listed X image through the canonical bounded client."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_MEDIA_HOSTS:
        return None
    try:
        response = FixedOriginClient(_MEDIA_ORIGINS, timeout=15.0).request(
            url,
            contract=IMAGE,
            headers={**_HEADERS, "Accept": "image/jpeg,image/png,image/webp"},
        )
        response.raise_for_status()
        return response.body
    except OutboundError:
        return None


def _extract_coordinate_from_bytes(
    payload: bytes, *, executable: Optional[str] = None, sea_snap: bool = True
) -> tuple[Optional[tuple[float, float]], bool, str, dict[str, Any]]:
    """The OCR core: EasyOCR read + Tesseract cross-check, else the Tesseract
    multi-band sweep, else pin+landmark geolocation. See ``_ocr_photo``.

    ``sea_snap`` is forwarded to the pin-landmark path: a land humanitarian
    case (Evros, a reception centre) must not have its pin dragged into the
    water (docs/fixes.md F-09 parity, audit LM-6)."""
    executable = executable if executable is not None else shutil.which("tesseract")
    easy_coordinate, easy_boxes, easy_attempted = _easyocr_image(payload)
    if easy_coordinate is not None:
        if executable:
            try:
                cross_check = _tesseract_cross_check(payload, executable)
            except Exception:
                cross_check = None
            if cross_check is not None:
                distance_m = haversine_m(cross_check, easy_coordinate)
                agree = distance_m <= OCR_CROSS_ENGINE_MAX_DISTANCE_M
                return (
                    easy_coordinate, True,
                    "easyocr_tesseract_consensus" if agree else "easyocr_text_disputed",
                    {
                        "interengine_distance_m": round(distance_m, 1),
                        "consensus_threshold_m": OCR_CROSS_ENGINE_MAX_DISTANCE_M,
                    },
                )
        return easy_coordinate, True, "easyocr_text", {}

    from PIL import Image, ImageFilter, ImageOps

    Image.MAX_IMAGE_PIXELS = 25_000_000
    attempted = False
    with Image.open(io.BytesIO(payload)) as source:
        image = ImageOps.exif_transpose(source).convert("L")
        width, height = image.size
        popup_boxes: list[tuple[int, int, int, int]] = []
        try:
            import numpy as np

            pixels = np.asarray(image)
            bright = pixels >= 235
            active_rows = np.flatnonzero(
                bright.sum(axis=1) >= max(80, int(width * 0.22))
            )

            def runs(values):
                if not len(values):
                    return []
                output = []
                start = previous = int(values[0])
                for raw in values[1:]:
                    value = int(raw)
                    if value != previous + 1:
                        output.append((start, previous + 1))
                        start = value
                    previous = value
                output.append((start, previous + 1))
                return output

            row_runs = runs(active_rows)
            merged_rows: list[tuple[int, int]] = []
            for top, bottom in row_runs:
                if merged_rows and top - merged_rows[-1][1] <= 32:
                    merged_rows[-1] = (merged_rows[-1][0], bottom)
                else:
                    merged_rows.append((top, bottom))

            for top, bottom in merged_rows:
                if bottom - top < 30:
                    continue
                active_columns = np.flatnonzero(
                    bright[top:bottom].sum(axis=0)
                    >= max(15, int((bottom - top) * 0.42))
                )
                for left, right in runs(active_columns):
                    if right - left < 140:
                        continue
                    padding = 12
                    popup_boxes.append((
                        max(0, left - padding),
                        max(0, top - padding),
                        min(width, right + padding),
                        min(height, bottom + padding),
                    ))
        except Exception:
            popup_boxes = []
        # Alarm Phone map popups can appear at the top, centre or bottom. A
        # full-map OCR pass often misses their small coordinate row, so scan
        # three overlapping horizontal bands as well as the complete image.
        boxes = popup_boxes + [
            (0, 0, width, height),
            (0, 0, width, max(1, int(height * 0.55))),
            (0, int(height * 0.22), width, max(1, int(height * 0.78))),
            (0, int(height * 0.45), width, height),
        ]
        for box in boxes:
            candidate_image = image.crop(box)
            scale = min(4, max(1, math.ceil(2400 / max(1, candidate_image.width))))
            if scale > 1:
                candidate_image = candidate_image.resize(
                    (candidate_image.width * scale, candidate_image.height * scale),
                    Image.Resampling.LANCZOS,
                )
            candidate_image = ImageOps.autocontrast(candidate_image).filter(ImageFilter.SHARPEN)
            output = io.BytesIO()
            candidate_image.save(output, format="PNG", optimize=True)
            coordinate, did_attempt = ocr_png_coordinate(
                output.getvalue(), executable=executable
            )
            attempted = attempted or did_attempt
            if coordinate is not None:
                return coordinate, attempted, "text", {}

    # No printed coordinate readout anywhere in the image — the screenshot
    # may still carry a plain drop-pin with no text at all (see module
    # docstring on map_pin_geolocate). Try recovering that from the pin's
    # pixel position plus visible place-name labels before giving up.
    try:
        from core.intel.map_pin_geolocate import (
            geolocate_pin_detailed,
            geolocate_pin_from_image,
        )

        pin_boxes = easy_boxes or None
        pin_coord = geolocate_pin_from_image(
            payload, executable=executable, word_boxes=pin_boxes, sea_snap=sea_snap
        )
        pin_source = "easyocr" if pin_boxes else "tesseract"
        # EasyOCR may detect map text yet still miss enough landmarks to fit
        # the pin. In that case retry the landmark pass with Tesseract instead
        # of letting a non-empty but incomplete box list suppress the fallback.
        if pin_coord is None and pin_boxes and executable:
            pin_coord = geolocate_pin_from_image(
                payload, executable=executable, word_boxes=None, sea_snap=sea_snap
            )
            pin_source = "tesseract"
            pin_boxes = None
        pin_solution = (
            geolocate_pin_detailed(payload, executable=executable, word_boxes=pin_boxes)
            if pin_coord is not None
            else None
        )
    except Exception:
        pin_coord = None
        pin_solution = None
    if pin_coord is not None:
        diagnostics: dict[str, Any] = {}
        if pin_solution is not None:
            diagnostics = {
                "estimated_position_error_m": float(pin_solution.estimated_position_error_m),
                "pin_solver_confidence": float(pin_solution.confidence),
                "pin_fit_residual_px": float(pin_solution.fit_residual_px),
                "pin_extrapolation_px": float(pin_solution.max_extrapolation_px),
                "pin_landmarks_used": list(pin_solution.landmarks_used),
            }
        return (
            pin_coord,
            True,
            "easyocr_pin_landmark" if pin_source == "easyocr" else "tesseract_pin_landmark",
            diagnostics,
        )
    return None, attempted or easy_attempted, "none", {}


def _ocr_photo(
    url: str,
) -> tuple[Optional[tuple[float, float]], bool, str, dict[str, Any]]:
    """Download one bounded public image and extract a coordinate from it.

    Returns (coordinate, attempted, method, diagnostics). ``diagnostics`` may
    carry ``interengine_distance_m`` / ``consensus_threshold_m`` for the
    EasyOCR<->Tesseract cross-check (F-03).
      - "easyocr_tesseract_consensus" — EasyOCR's read, cross-checked and
        confirmed by an independent Tesseract pass (tight uncertainty).
      - "easyocr_text_disputed" — EasyOCR's read, but a Tesseract cross-check
        landed on a materially different coordinate (wide uncertainty,
        needs_review) — never silently trust one engine over a disagreement.
      - "easyocr_text" — EasyOCR's read, Tesseract unavailable or found
        nothing to compare against (unchanged legacy behaviour).
      - "text" — printed coordinate readout from the Tesseract-only path.
      - "easyocr_pin_landmark" / "tesseract_pin_landmark" — a plain map
        screenshot geolocated from its drop-pin plus visible place labels
        (see map_pin_geolocate.py).
      - "none".

    The structured, multi-field version is
    ``core.intel.image_extraction.extract_from_url`` -- this stays a 4-tuple
    for twikit_monitor / backfill until they are migrated.
    """
    executable = shutil.which("tesseract")
    if not executable and importlib.util.find_spec("easyocr") is None:
        return None, False, "none", {}
    payload = _download_bounded_image(url)
    if payload is None:
        return None, False, "none", {}
    return _extract_coordinate_from_bytes(payload, executable=executable)
