# SPDX-License-Identifier: AGPL-3.0-or-later
"""Map-pin detection with ranked candidates (docs/prompt.md §6).

`map_pin_geolocate._detect_marker_pixel` ran four hard-coded colour masks and
returned the tip of the first blob that passed one compactness gate, or
`None`. A pin of another hue (green, a black-outline Leaflet default), a pin
with a shadow, or two red blobs all collapsed to "no pin" with no way for a
caller to fall back to "approximate, low confidence".

This module keeps the colour masks as one detector and adds a second,
colour-independent one: an HSV high-saturation mask, connected components,
then per-component geometry (compactness, aspect, fill ratio, isolation).
Every blob from either detector is returned as a `PinCandidate` with a
confidence and the tip pixel; `select_pin` picks a single pin only when one
candidate is clearly the marker (prompt §6: "only select a pin when evidence
is sufficiently unambiguous").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# A blob is a plausible pin only inside this size envelope (fractions of the
# frame). Matches the old _blob_from_mask gates.
_MAX_BOX_W_FRAC = 0.10
_MAX_BOX_H_FRAC = 0.14
_MIN_AREA_PX = 10
_MAX_AREA_FRAC = 0.02
_MIN_FILL_RATIO = 0.20
# Longest side the mask analysis runs at -- a pin blob stays many px across
# after this downscale, and it bounds the connected-components pass.
_ANALYSIS_MAX_SIDE = 1000
# select_pin acceptance
_MIN_SELECT_CONFIDENCE = 0.35
_DOMINANCE_MARGIN = 0.15
_AGREE_PX = 18.0


@dataclass
class PinCandidate:
    x: float
    y: float
    confidence: float
    detector: str          # color_mask | shape_hsv
    color: Optional[str]    # red | blue | amber | yellow | None
    shape: str              # teardrop | circle | blob

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "confidence": round(self.confidence, 2),
            "detector": self.detector,
            "color": self.color,
            "shape": self.shape,
        }


def _label_components(mask):
    """4-connectivity connected components of a boolean mask, numpy-only.

    Returns (labels, count) with labels 1..count (0 is background). Iterative
    flood fill -- the mask is already downscaled so the pixel count is small.
    """
    import numpy as np

    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    current = 0
    stack: list[tuple[int, int]] = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if labels[sy, sx]:
            continue
        current += 1
        stack.append((sy, sx))
        labels[sy, sx] = current
        while stack:
            y, x = stack.pop()
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = current
                    stack.append((ny, nx))
    return labels, current


def _blob_candidate(
    ys, xs, *, width: int, height: int, detector: str, color: Optional[str]
) -> Optional[PinCandidate]:
    """Score one blob's pixels; None if it can't be a pin marker."""
    area = len(xs)
    if area < _MIN_AREA_PX or area > _MAX_AREA_FRAC * width * height:
        return None
    min_x, max_x = int(xs.min()), int(xs.max())
    min_y, max_y = int(ys.min()), int(ys.max())
    box_w = max_x - min_x + 1
    box_h = max_y - min_y + 1
    if box_w > _MAX_BOX_W_FRAC * width or box_h > _MAX_BOX_H_FRAC * height:
        return None
    fill_ratio = area / float(box_w * box_h)
    if fill_ratio < _MIN_FILL_RATIO:
        return None

    import numpy as np

    aspect = box_h / max(1, box_w)
    # "Pointed downward": the bottom band is much narrower than the top band
    # -- a drop-pin's teardrop, whose bbox is often near-square once the
    # coloured body is isolated from a lighter head.
    band = max(1, int(round(box_h * 0.22)))
    top_rows = xs[ys < min_y + band]
    bot_rows = xs[ys > max_y - band]
    top_w = (int(top_rows.max() - top_rows.min()) + 1) if len(top_rows) else box_w
    bot_w = (int(bot_rows.max() - bot_rows.min()) + 1) if len(bot_rows) else box_w
    pointed_down = bot_w <= 0.5 * top_w

    if pointed_down:
        shape = "teardrop"
        tip_y = float(max_y)
        center_x = float(bot_rows.mean()) if len(bot_rows) else float(xs.mean())
    elif 0.8 <= aspect <= 1.25:
        # a circular incident marker encodes its position at the centre
        shape = "circle"
        tip_y = float(ys.mean())
        center_x = float(xs.mean())
    else:
        shape = "wide"
        tip_y = float(max_y)
        center_x = float(xs.mean())

    # Confidence: a real marker is compact (high fill), the right size, and
    # its shape looks like a pin/circle rather than a smear.
    size_score = 1.0 - min(1.0, abs(area - 240) / 900.0)
    shape_score = 1.0 if shape != "wide" else 0.5
    confidence = max(0.05, min(0.9, 0.25 + 0.35 * fill_ratio + 0.2 * size_score + 0.2 * shape_score))
    return PinCandidate(center_x, tip_y, confidence, detector, color, shape)


def _colour_candidates(pixels, width: int, height: int) -> list[PinCandidate]:
    import numpy as np

    r = pixels[:, :, 0].astype(int)
    g = pixels[:, :, 1].astype(int)
    b = pixels[:, :, 2].astype(int)
    masks = {
        "red": (r > 150) & (g < 110) & (b < 110) & (r - g > 60) & (r - b > 60),
        "blue": (b > 150) & (r < 120) & (g < 150) & (b - r > 55) & (b - g > 25),
        "amber": (r > 180) & (g > 90) & (g < 190) & (b < 100) & (r - b > 90) & (r - g > 25),
        "yellow": (r > 205) & (g > 205) & (b < 135) & (r - b > 80) & (g - b > 80),
    }
    out: list[PinCandidate] = []
    for color, mask in masks.items():
        mask = np.asarray(mask)
        if not mask.any() or int(mask.sum()) > _MAX_AREA_FRAC * width * height:
            continue
        labels, count = _label_components(mask)
        for label in range(1, count + 1):
            ys, xs = np.nonzero(labels == label)
            candidate = _blob_candidate(
                ys, xs, width=width, height=height, detector="color_mask", color=color
            )
            if candidate is not None:
                out.append(candidate)
    return out


def _shape_candidates(image, width: int, height: int) -> list[PinCandidate]:
    """Colour-independent: HSV high-saturation compact blobs."""
    import numpy as np

    hsv = np.asarray(image.convert("HSV"))
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    # saturated and not near-white / not near-black: a coloured marker on a
    # muted basemap. The basemap sea band is saturated too, but its blob is
    # far too large and is rejected by the size gate.
    mask = (sat > 120) & (val > 90) & (val < 250)
    mask = np.asarray(mask)
    total = int(mask.sum())
    if total == 0 or total > 0.25 * width * height:
        return []
    labels, count = _label_components(mask)
    out: list[PinCandidate] = []
    for label in range(1, count + 1):
        ys, xs = np.nonzero(labels == label)
        candidate = _blob_candidate(
            ys, xs, width=width, height=height, detector="shape_hsv", color=None
        )
        if candidate is not None:
            out.append(candidate)
    return out


def detect_pins(image) -> list[PinCandidate]:
    """Every pin candidate from both detectors, most confident first."""
    from PIL import Image

    rgb = image.convert("RGB")
    scale = 1.0
    longest = max(rgb.width, rgb.height)
    if longest > _ANALYSIS_MAX_SIDE:
        scale = _ANALYSIS_MAX_SIDE / longest
        rgb = rgb.resize(
            (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
            Image.Resampling.BILINEAR,
        )
    import numpy as np

    pixels = np.asarray(rgb)
    width, height = rgb.width, rgb.height
    candidates = _colour_candidates(pixels, width, height) + _shape_candidates(rgb, width, height)
    # back to original pixel space
    if scale != 1.0:
        for candidate in candidates:
            candidate.x /= scale
            candidate.y /= scale
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def _agree(a: PinCandidate, b: PinCandidate) -> bool:
    return abs(a.x - b.x) <= _AGREE_PX and abs(a.y - b.y) <= _AGREE_PX


def select_pin(candidates: list[PinCandidate]) -> Optional[PinCandidate]:
    """Select one map pin after spatially clustering detector overlap.

    Real map screenshots contain many saturated POI circles. A drop-pin is
    stronger evidence when the colour and shape detectors agree on the same
    teardrop, especially a red one, than a slightly higher-scoring basemap
    circle. Distinct competing teardrops still fail closed.
    """
    if not candidates:
        return None
    clusters: list[list[PinCandidate]] = []
    for candidate in candidates:
        for cluster in clusters:
            if any(_agree(candidate, member) for member in cluster):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])

    ranked: list[tuple[float, PinCandidate]] = []
    for cluster in clusters:
        best = max(cluster, key=lambda c: c.confidence)
        detectors = {c.detector for c in cluster}
        colours = {c.color for c in cluster if c.color}
        shapes = {c.shape for c in cluster}
        merged = PinCandidate(
            x=sum(c.x for c in cluster) / len(cluster),
            y=sum(c.y for c in cluster) / len(cluster),
            confidence=min(0.9, best.confidence + (0.08 if len(detectors) >= 2 else 0.0)),
            detector="+".join(sorted(detectors)),
            color=best.color or next(iter(colours), None),
            shape="teardrop" if "teardrop" in shapes else best.shape,
        )
        score = merged.confidence
        if merged.shape == "teardrop":
            score += 0.16
        if merged.color == "red":
            score += 0.10
        if len(detectors) >= 2:
            score += 0.08
        ranked.append((score, merged))

    ranked.sort(key=lambda item: item[0], reverse=True)
    score, best = ranked[0]
    if best.confidence < _MIN_SELECT_CONFIDENCE:
        return None
    if len(ranked) == 1:
        return best
    next_score, contender = ranked[1]
    if best.shape == contender.shape == "teardrop" and score - next_score < _DOMINANCE_MARGIN:
        return None
    if score - next_score >= _DOMINANCE_MARGIN or (best.shape == "teardrop") != (contender.shape == "teardrop"):
        return best
    return None


def detect_pin(image) -> Optional[tuple[float, float]]:
    """The single selected pin's tip pixel, or None."""
    chosen = select_pin(detect_pins(image))
    return (chosen.x, chosen.y) if chosen is not None else None
