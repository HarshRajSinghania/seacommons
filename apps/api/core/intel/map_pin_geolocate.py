# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geolocate a map-screenshot pin that carries no printed coordinate text.

Alarm Phone (and other tracked accounts) sometimes attach a plain map
screenshot — basemap + place-name labels + a single drop-pin marker — with
no lat/lon readout anywhere in the image. `x_media_utils.ocr_png_coordinate`
only ever finds a position when one is printed as text, so those images
silently fall back to a rough place-name/region centroid.

This module recovers a real position from the image itself:
  1. Detect the pin marker's pixel position (colour + HSV-shape detectors,
     ranked candidates -- see core.intel.image_pin).
  2. OCR the full image for word-level bounding boxes.
  3. Match words (and adjacent-word phrases) against a small, precise
     gazetteer of unambiguous coastal towns/islands.
  4. With >= 2 matched landmarks, fit an independent linear pixel<->geo
     transform per axis (standard web maps are north-up, so x maps to
     longitude and y to latitude with no rotation/shear) and invert it for
     the pin's own pixel position.

Deliberately conservative: any missing precondition (no pin found, fewer
than two landmarks, degenerate/duplicate pixel positions, an out-of-range or
wildly extrapolated result) returns None rather than a guess — a caller that
gets None simply keeps the weaker fallback it already had.
"""
from __future__ import annotations

import io
import re
import shutil
from difflib import SequenceMatcher
import subprocess
import unicodedata
from typing import Optional

from core.intel.geoextract import PRECISE_PLACES

_MIN_LANDMARK_MATCHES = 2
_MIN_PIXEL_SPREAD = 20          # px — matched landmarks must actually spread out
_MIN_WORD_CONF = 40             # tesseract word confidence, 0-100
_LAT_RANGE = (20.0, 48.0)
_LON_RANGE = (-12.0, 42.0)
# A distress pin routinely sits hundreds of km out at sea from the nearest
# labelled coastal town (that's the whole point of "boat spotted south of
# Crete" reports), so this guards against a genuinely wrong landmark match
# (OCR misread / wrong instance of an ambiguous name) rather than against
# normal, expected extrapolation distance.
_MAX_KM_FROM_NEAREST_LANDMARK = 600.0
# Large region/island labels are not point landmarks. In particular the
# `crete`/`kriti` gazetteer coordinate is deliberately an offshore textual
# fallback, not the pixel location of the island-name label on a basemap.
_PIN_CALIBRATION_EXCLUSIONS = frozenset({"crete", "kriti"})
_EARTH_RADIUS_KM = 6371.0

_PLACES_SORTED = sorted(PRECISE_PLACES.items(), key=lambda kv: -len(kv[0]))
_MAX_PHRASE_WORDS = max((len(name.split()) for name in PRECISE_PLACES), default=1)


def _normalize_label(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z ]", "", stripped.lower()).strip()


def _detect_marker_pixel(image) -> Optional[tuple[int, int]]:
    """Find a single compact drop-pin / circle marker; None if none/ambiguous.

    Delegates to `image_pin`, which runs the colour masks and a
    colour-independent HSV shape detector, ranks every blob and only returns
    a pin when one candidate is unambiguously the marker (docs/prompt.md §6).
    """
    from core.intel.image_pin import detect_pin

    tip = detect_pin(image)
    if tip is None:
        return None
    return int(round(tip[0])), int(round(tip[1]))


def _ocr_pass(image, *, executable: str, block_prefix: str) -> list[dict]:
    """One tesseract TSV word-box pass over a (sub)image, in that image's
    own local pixel space (offset 0,0). `block_prefix` keeps this pass's
    (block, par, line) keys distinct from any other pass/tile so
    `_match_landmarks` never joins words across pass/tile boundaries.

    Empirically (verified against real Alarm Phone map screenshots), `--psm
    11` (sparse text) fragments scattered map labels into unusable garbage —
    treating dozens of stray coastline/road pixels as isolated "words" — no
    matter how much the image is scaled or sharpened. `--psm 6` (uniform
    text block) reads real labels ("Heraklion", "Chrisi", ...) cleanly at or
    near native resolution; a modest 2x upscale surfaces a few more without
    the artefacts that heavier scaling + sharpening introduced in testing.
    """
    from PIL import Image, ImageOps

    if image.width < 1 or image.height < 1:
        return []
    scaled = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    scaled = ImageOps.autocontrast(scaled.convert("RGB"))
    buf = io.BytesIO()
    scaled.save(buf, format="PNG")
    scale = 2

    # Same one-Tesseract-job-at-a-time discipline as x_media_utils.py's
    # _TESSERACT_LOCK -- this module's own subprocess calls were the other
    # unbounded source of concurrent tesseract processes on the pilot VM's
    # 2 vCPUs, alongside ocr_png_coordinate's sweep.
    from core.intel.x_media_utils import _TESSERACT_LOCK

    try:
        with _TESSERACT_LOCK:
            result = subprocess.run(
                [executable, "stdin", "stdout", "--psm", "6", "-l", "eng", "tsv"],
                input=buf.getvalue(),
                capture_output=True,
                check=False,
                timeout=20,
            )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    boxes: list[dict] = []
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    for line in lines[1:]:  # skip header
        fields = line.split("\t")
        if len(fields) < 12:
            continue
        level, _page, block, par, line_num, word_num, left, top, w, h, conf, text = fields[:12]
        text = text.strip()
        if level != "5" or not text:
            continue
        try:
            conf_f = float(conf)
        except ValueError:
            continue
        if conf_f < _MIN_WORD_CONF:
            continue
        try:
            boxes.append({
                "text": text,
                "left": int(left) // scale,
                "top": int(top) // scale,
                "width": int(w) // scale,
                "height": int(h) // scale,
                "block": f"{block_prefix}-{block}", "par": par, "line": line_num,
                "word": int(word_num),
            })
        except ValueError:
            continue
    return boxes


def _tile_regions(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """A 2x2 grid with ~15% overlap so a label straddling a naive cut line
    still lands wholly inside at least one tile."""
    half_w, half_h = width / 2, height / 2
    ow, oh = int(half_w * 0.15), int(half_h * 0.15)
    return [
        (0, 0, int(half_w) + ow, int(half_h) + oh),
        (int(half_w) - ow, 0, width, int(half_h) + oh),
        (0, int(half_h) - oh, int(half_w) + ow, height),
        (int(half_w) - ow, int(half_h) - oh, width, height),
    ]


def _ocr_word_boxes(image, *, executable: str) -> list[dict]:
    """Word-level OCR boxes (pixel-space), merged from two passes.

    A single whole-image `--psm 6` pass treats the entire screenshot as one
    text block, which — verified against real screenshots — reads a strong
    label or two cleanly but garbles most others once several spatially
    separated labels compete for one reading order. Re-running the same pass
    on four overlapping image quadrants gives each label a much smaller,
    closer-to-uniform block to be read in, recovering labels the whole-image
    pass missed; results are merged (duplicates across tiles are harmless —
    `_match_landmarks` just keeps the first per name).
    """
    boxes = _ocr_pass(image, executable=executable, block_prefix="full")
    for index, (left, top, right, bottom) in enumerate(_tile_regions(image.width, image.height)):
        tile = image.crop((left, top, right, bottom))
        tile_boxes = _ocr_pass(tile, executable=executable, block_prefix=f"tile{index}")
        for box in tile_boxes:
            box["left"] += left
            box["top"] += top
        boxes.extend(tile_boxes)
    return boxes


def _fuzzy_place_name(text: str) -> Optional[str]:
    normalized = _normalize_label(text)
    if not normalized or normalized in _PIN_CALIBRATION_EXCLUSIONS:
        return None
    if normalized in PRECISE_PLACES:
        return normalized
    if len(normalized) < 6:
        return None
    best_name = None
    best_score = 0.0
    for name in PRECISE_PLACES:
        if name in _PIN_CALIBRATION_EXCLUSIONS or abs(len(name) - len(normalized)) > 3:
            continue
        score = SequenceMatcher(None, normalized, name).ratio()
        if score > best_score:
            best_name, best_score = name, score
    return best_name if best_score >= 0.82 else None


def _dedupe_word_boxes(word_boxes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for box in word_boxes:
        text = _normalize_label(str(box.get("text") or ""))
        if not text:
            continue
        cx = float(box["left"]) + float(box["width"]) / 2
        cy = float(box["top"]) + float(box["height"]) / 2
        duplicate = False
        for previous in out:
            ptext = _normalize_label(str(previous.get("text") or ""))
            pcx = float(previous["left"]) + float(previous["width"]) / 2
            pcy = float(previous["top"]) + float(previous["height"]) / 2
            if text == ptext and abs(cx - pcx) <= 18 and abs(cy - pcy) <= 18:
                duplicate = True
                break
        if not duplicate:
            out.append(box)
    return out


def _span_center(boxes: list[dict]) -> tuple[float, float]:
    left = min(b["left"] for b in boxes)
    top = min(b["top"] for b in boxes)
    right = max(b["left"] + b["width"] for b in boxes)
    bottom = max(b["top"] + b["height"] for b in boxes)
    return (left + right) / 2, (top + bottom) / 2


def _match_landmarks(word_boxes: list[dict]) -> list[tuple[str, float, float]]:
    """Match noisy, tiled map-label OCR to precise gazetteer points."""
    boxes = _dedupe_word_boxes(word_boxes)
    matched: dict[str, tuple[float, float]] = {}

    # Exact/fuzzy single labels first.
    for box in boxes:
        place = _fuzzy_place_name(str(box.get("text") or ""))
        if place and place not in matched:
            matched[place] = _span_center([box])

    # Same-line phrases from each OCR pass.
    by_line: dict[tuple[str, str, str], list[dict]] = {}
    for box in boxes:
        by_line.setdefault((box["block"], box["par"], box["line"]), []).append(box)
    for line_boxes in by_line.values():
        line_boxes = sorted(line_boxes, key=lambda b: b["left"])
        for start in range(len(line_boxes)):
            for span in range(2, min(_MAX_PHRASE_WORDS, len(line_boxes) - start) + 1):
                group = line_boxes[start:start + span]
                place = _fuzzy_place_name(" ".join(str(b["text"]) for b in group))
                if place and place not in matched:
                    matched[place] = _span_center(group)

    # Map labels frequently wrap vertically (e.g. AGIOS / Nikolaos). Join
    # nearby boxes with strong horizontal overlap even when Tesseract assigns
    # them to separate lines.
    for i, first in enumerate(boxes):
        for second in boxes[i + 1:]:
            c1x, c1y = _span_center([first])
            c2x, c2y = _span_center([second])
            vertical_gap = abs(c1y - c2y)
            horizontal_gap = abs(c1x - c2x)
            if vertical_gap > 55 or horizontal_gap > 90:
                continue
            ordered = [first, second] if c1y <= c2y else [second, first]
            place = _fuzzy_place_name(" ".join(str(b["text"]) for b in ordered))
            if place and place not in matched:
                matched[place] = _span_center(ordered)

    return [(name, px, py) for name, (px, py) in matched.items()]


def _drop_worst_landmark(
    landmarks: list[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    """Drop one landmark if its pixel position is a clear outlier against a
    linear pixel<->geo fit of the rest; otherwise keep them all."""
    import numpy as np

    names = [n for n, _, _ in landmarks]
    px = np.array([x for _, x, _ in landmarks])
    py = np.array([y for _, _, y in landmarks])
    lon = np.array([PRECISE_PLACES[n][1] for n in names])
    lat = np.array([PRECISE_PLACES[n][0] for n in names])

    residuals: list[float] = []
    for i in range(len(landmarks)):
        keep = [j for j in range(len(landmarks)) if j != i]
        if np.ptp(lon[keep]) < 1e-6 or np.ptp(lat[keep]) < 1e-6:
            return landmarks
        sx, ix = np.polyfit(lon[keep], px[keep], 1)
        sy, iy = np.polyfit(lat[keep], py[keep], 1)
        residuals.append(
            float(abs(px[i] - (sx * lon[i] + ix)) + abs(py[i] - (sy * lat[i] + iy)))
        )

    worst = int(np.argmax(residuals))
    others = [residuals[j] for j in range(len(residuals)) if j != worst]
    median_other = float(np.median(others)) if others else 0.0
    if residuals[worst] > max(25.0, 3.0 * median_other):
        return [lm for j, lm in enumerate(landmarks) if j != worst]
    return landmarks


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _fit_axis(pixel_values: list[float], geo_values: list[float]) -> Optional[tuple[float, float]]:
    """Least-squares pixel = slope*geo + intercept; None if geo values don't spread."""
    if max(geo_values) - min(geo_values) < 1e-6:
        return None
    import numpy as np

    slope, intercept = np.polyfit(geo_values, pixel_values, 1)
    return float(slope), float(intercept)


def geolocate_pin_from_image(
    payload: bytes,
    *,
    executable: Optional[str] = None,
    word_boxes: Optional[list[dict]] = None,
    sea_snap: bool = True,
) -> Optional[tuple[float, float]]:
    """Best-effort: recover the pin's real-world position from a plain map
    screenshot with no printed coordinates, using visible place labels as
    calibration points via a Web-Mercator fit (docs/prompt.md §7). Returns
    None on any missing precondition. ``sea_snap`` nudges a maritime result
    onto water; pass False for a land humanitarian case."""
    command = executable or shutil.which("tesseract")
    if not command and not word_boxes:
        return None

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 25_000_000
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source)
            pin = _detect_marker_pixel(image)
            if pin is None:
                return None
            detected_word_boxes = word_boxes or _ocr_word_boxes(image, executable=command)
    except Exception:
        return None

    solution = _solve(_match_landmarks(detected_word_boxes), pin, image.size)
    if solution is None:
        return None

    nearest_km = min(
        _haversine_km(
            solution.lat, solution.lon, PRECISE_PLACES[name][0], PRECISE_PLACES[name][1]
        )
        for name in solution.landmarks_used
    )
    if nearest_km > _MAX_KM_FROM_NEAREST_LANDMARK:
        return None

    if not sea_snap:
        return solution.lat, solution.lon
    # A pixel->geo fit off a handful of place labels can put the pin a few km
    # inland; a maritime pin is at sea, so nudge onto water. Conditional so a
    # land humanitarian case is never dragged into the water (docs/fixes.md
    # F-09 parity -- the caller passes sea_snap=False for those).
    from core.intel.landmask import nearest_sea_point

    return nearest_sea_point(solution.lat, solution.lon)


def _solve(matched: list[tuple[str, float, float]], pin: tuple[int, int], image_size):
    """Adapt _match_landmarks output to the Web-Mercator solver."""
    from core.intel.image_geolocate import Landmark, solve_pin_position

    landmarks = [
        Landmark(name, px, py, PRECISE_PLACES[name][0], PRECISE_PLACES[name][1])
        for name, px, py in matched
        if name in PRECISE_PLACES and name not in _PIN_CALIBRATION_EXCLUSIONS
    ]
    if len(landmarks) < _MIN_LANDMARK_MATCHES:
        return None
    return solve_pin_position(pin, landmarks, image_size=image_size)


def geolocate_pin_detailed(
    payload: bytes,
    *,
    executable: Optional[str] = None,
    word_boxes: Optional[list[dict]] = None,
):
    """Same as ``geolocate_pin_from_image`` but returns the full
    ``GeolocationSolution`` (residual, extrapolation, error estimate,
    confidence, landmarks) -- for the structured image pipeline."""
    command = executable or shutil.which("tesseract")
    if not command and not word_boxes:
        return None
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 25_000_000
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source)
            pin = _detect_marker_pixel(image)
            if pin is None:
                return None
            boxes = word_boxes or _ocr_word_boxes(image, executable=command)
            return _solve(_match_landmarks(boxes), pin, image.size)
    except Exception:
        return None
