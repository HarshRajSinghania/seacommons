# SPDX-License-Identifier: AGPL-3.0-or-later
"""core.intel.image_pin -- ranked pin candidates + unambiguous selection (docs/prompt.md §6)."""
from __future__ import annotations

import io

from core.intel.image_pin import PinCandidate, detect_pin, detect_pins, select_pin
from PIL import Image, ImageDraw

from tests.fixtures.alarm_phone_images import CASES_BY_NAME, _draw_pin


def _canvas(w=400, h=300, bg=(238, 232, 220)):
    return Image.new("RGB", (w, h), bg)


def _teardrop(img, x, y, colour):
    """A drop-pin (head + teardrop) whose tip is exactly (x, y)."""
    _draw_pin(ImageDraw.Draw(img), x, y, colour)


def test_colour_detector_finds_each_marker_hue():
    for colour, name in (
        ((214, 40, 40), "red"),
        ((26, 115, 232), "blue"),
        ((240, 150, 30), "amber"),
        ((250, 245, 35), "yellow"),
    ):
        img = _canvas()
        _teardrop(img, 200, 167, colour)
        pin = detect_pin(img)
        assert pin is not None, name
        assert abs(pin[0] - 200) <= 4 and abs(pin[1] - 167) <= 4, name


def test_shape_detector_finds_a_pin_outside_the_colour_masks():
    # a saturated green marker matches none of the four RGB colour masks;
    # the HSV shape detector must still find it.
    img = _canvas()
    _teardrop(img, 150, 140, (30, 170, 60))
    candidates = detect_pins(img)
    assert any(c.detector == "shape_hsv" for c in candidates)
    pin = detect_pin(img)
    assert pin is not None
    assert abs(pin[0] - 150) <= 5 and abs(pin[1] - 140) <= 5


def test_large_coloured_region_is_not_a_pin():
    img = _canvas()
    px = img.load()
    for y in range(200):
        for x in range(400):
            px[x, y] = (30, 90, 200)  # a whole blue sea band
    assert detect_pin(img) is None


def test_two_confident_separate_blobs_fail_closed():
    img = _canvas()
    _teardrop(img, 120, 120, (214, 40, 40))
    _teardrop(img, 300, 200, (214, 40, 40))
    candidates = detect_pins(img)
    assert len(candidates) >= 2
    assert select_pin(candidates) is None


def test_agreeing_detections_are_merged_not_rejected():
    # one green teardrop -> the colour masks miss it, the shape detector hits
    # it; a single detection is accepted. Add a red teardrop at the SAME spot
    # so both detectors fire and must be merged, not treated as ambiguous.
    img = _canvas()
    _teardrop(img, 210, 160, (214, 40, 40))
    chosen = select_pin(detect_pins(img))
    assert chosen is not None
    assert abs(chosen.x - 210) <= 5 and abs(chosen.y - 160) <= 5


def test_no_candidates_returns_none():
    assert detect_pin(_canvas()) is None
    assert select_pin([]) is None


def test_low_confidence_top_candidate_is_rejected():
    weak = PinCandidate(10.0, 10.0, 0.2, "shape_hsv", None, "wide")
    assert select_pin([weak]) is None


def test_every_candidate_carries_a_confidence_and_tip():
    img = _canvas()
    _teardrop(img, 200, 167, (214, 40, 40))
    for candidate in detect_pins(img):
        assert 0.0 <= candidate.confidence <= 1.0
        d = candidate.as_dict()
        assert {"x", "y", "confidence", "detector", "color", "shape"} <= d.keys()


def test_circle_marker_uses_centre_not_tip():
    img = _canvas()
    px = img.load()
    cx, cy, radius = 220, 150, 11
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                px[x, y] = (247, 220, 40)
    pin = detect_pin(img)
    assert pin is not None
    assert abs(pin[1] - cy) <= 4  # centre, not the bottom edge (cy + radius)


def test_fixture_pin_only_map_is_detected():
    payload = CASES_BY_NAME["pin_only_red"].render()
    with Image.open(io.BytesIO(payload)) as source:
        assert detect_pin(source.convert("RGB")) is not None


def test_unique_dual_detector_teardrop_beats_basemap_circle_clutter():
    candidates = [
        PinCandidate(636.5, 219.7, 0.88, "color_mask", "blue", "circle"),
        PinCandidate(636.5, 219.7, 0.87, "shape_hsv", None, "circle"),
        PinCandidate(321.4, 629.9, 0.84, "color_mask", "red", "teardrop"),
        PinCandidate(321.4, 629.9, 0.84, "shape_hsv", None, "teardrop"),
        PinCandidate(457.3, 142.9, 0.84, "shape_hsv", None, "circle"),
    ]
    chosen = select_pin(candidates)
    assert chosen is not None
    assert chosen.shape == "teardrop"
    assert abs(chosen.x - 321.4) < 2 and abs(chosen.y - 629.9) < 2


def test_two_dual_detector_teardrops_still_fail_closed():
    candidates = [
        PinCandidate(120, 120, 0.82, "color_mask", "red", "teardrop"),
        PinCandidate(120, 120, 0.80, "shape_hsv", None, "teardrop"),
        PinCandidate(300, 200, 0.83, "color_mask", "red", "teardrop"),
        PinCandidate(300, 200, 0.81, "shape_hsv", None, "teardrop"),
    ]
    assert select_pin(candidates) is None
