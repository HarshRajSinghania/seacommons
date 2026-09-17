# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from core.intel.map_pin_geolocate import (
    _detect_marker_pixel,
    _drop_worst_landmark,
    _fit_axis,
    _haversine_km,
    _match_landmarks,
    _normalize_label,
    geolocate_pin_from_image,
)


def test_detect_marker_pixel_finds_non_red_markers() -> None:
    from PIL import Image

    for colour in ((214, 40, 40), (26, 115, 232), (240, 150, 30), (250, 245, 35)):
        img = Image.new("RGB", (400, 300), (238, 232, 220))  # light basemap
        for y in range(150, 168):
            for x in range(200 - (167 - y), 200 + (167 - y) + 1):  # teardrop-ish
                img.putpixel((x, y), colour)
        tip = _detect_marker_pixel(img)
        assert tip is not None, colour
        assert abs(tip[0] - 200) <= 3 and abs(tip[1] - 167) <= 3


def test_detect_marker_pixel_ignores_a_large_coloured_region() -> None:
    from PIL import Image

    img = Image.new("RGB", (400, 300), (238, 232, 220))
    for y in range(200):          # a big blue sea area, not a marker
        for x in range(400):
            img.putpixel((x, y), (30, 90, 200))
    assert _detect_marker_pixel(img) is None


def test_drop_worst_landmark_removes_a_single_outlier() -> None:
    # heraklion/rethymno/chania roughly along Crete's north coast; a 4th
    # "matched" label placed at a pixel far off the line is the misread.
    good = [
        ("heraklion", 300.0, 100.0),
        ("rethymno", 220.0, 100.0),
        ("chania", 140.0, 100.0),
    ]
    outlier = ("gavdos", 900.0, 900.0)
    kept = _drop_worst_landmark(good + [outlier])
    assert {n for n, _, _ in kept} == {"heraklion", "rethymno", "chania"}

    # nothing dropped when all four are consistent
    consistent = good + [("ierapetra", 360.0, 100.0)]
    assert len(_drop_worst_landmark(consistent)) == 4


def test_normalize_label_strips_accents_and_punctuation() -> None:
    assert _normalize_label("Réthymno,") == "rethymno"
    assert _normalize_label("Agíos Nikolaos") == "agios nikolaos"
    assert _normalize_label("  Heraklion  ") == "heraklion"


def _word(text, left, top, width, height, *, block="1", par="1", line="1", word=1):
    return {
        "text": text, "left": left, "top": top, "width": width, "height": height,
        "block": block, "par": par, "line": line, "word": word,
    }


def test_match_landmarks_finds_single_word_places() -> None:
    boxes = [
        _word("Heraklion", 100, 50, 90, 14, line="1", word=1),
        _word("Rethymno", 400, 55, 80, 14, line="2", word=1),
        _word("Unrelated", 200, 200, 90, 14, line="3", word=1),
    ]
    matches = {name: (px, py) for name, px, py in _match_landmarks(boxes)}
    assert "heraklion" in matches
    assert "rethymno" in matches
    assert "unrelated" not in matches
    assert matches["heraklion"] == (145.0, 57.0)


def test_match_landmarks_joins_adjacent_words_for_multiword_places() -> None:
    boxes = [
        _word("Agios", 100, 50, 60, 14, line="1", word=1),
        _word("Nikolaos", 165, 50, 90, 14, line="1", word=2),
    ]
    matches = {name: (px, py) for name, px, py in _match_landmarks(boxes)}
    assert "agios nikolaos" in matches


def test_fit_axis_recovers_linear_relationship() -> None:
    # pixel = 10*geo + 5, exactly — polyfit should recover it losslessly.
    geo = [1.0, 2.0, 3.0]
    pixel = [15.0, 25.0, 35.0]
    slope, intercept = _fit_axis(pixel, geo)
    assert abs(slope - 10.0) < 1e-6
    assert abs(intercept - 5.0) < 1e-6


def test_fit_axis_returns_none_for_degenerate_input() -> None:
    assert _fit_axis([10.0, 10.0, 10.0], [1.0, 1.0, 1.0]) is None


def test_haversine_km_known_distance() -> None:
    # Rome to Naples is a well-known ~190 km great-circle distance.
    km = _haversine_km(41.90, 12.50, 40.85, 14.27)
    assert 180 < km < 210


def test_haversine_km_zero_for_identical_points() -> None:
    assert _haversine_km(35.0, 25.0, 35.0, 25.0) == 0.0


def test_geolocate_pin_from_image_returns_none_without_tesseract(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert geolocate_pin_from_image(b"not a real image") is None


def test_geolocate_pin_from_image_end_to_end_with_synthetic_map(monkeypatch) -> None:
    """Build a synthetic 'map': two labelled towns + a red pin between them,
    positioned so the true pin location is knowable in advance, then check
    the recovered lat/lon is close to that ground truth."""
    import io

    import numpy as np
    from PIL import Image

    width, height = 900, 600
    img = np.full((height, width, 3), 235, dtype=np.uint8)  # light basemap

    # A single consistent north-up linear model (pixel = scale*geo + origin)
    # shared by every landmark AND the pin, exactly like a real web-map
    # projection over a small extent — so the fit should recover it exactly.
    def lon_to_x(lon: float) -> int:
        return round(400 * (lon - 24.0) + 50)

    def lat_to_y(lat: float) -> int:
        return round(800 * (35.50 - lat) + 50)  # y grows as lat decreases

    def x_to_lon(x: float) -> float:
        return (x - 50) / 400 + 24.0

    def y_to_lat(y: float) -> float:
        return 35.50 - (y - 50) / 800

    landmarks = {
        "Heraklion": (35.34, 25.13),
        "Rethymno": (35.37, 24.47),
        "Ierapetra": (35.01, 25.74),
    }
    pin_px = (400, 300)
    expected_lon = x_to_lon(pin_px[0])
    expected_lat = y_to_lat(pin_px[1])

    # Paint a compact red teardrop-ish blob at the pin's pixel position.
    px, py = pin_px
    img[py - 6:py + 6, px - 4:px + 4] = [220, 40, 30]

    image = Image.fromarray(img, mode="RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    payload = buf.getvalue()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")

    fake_boxes = []
    for line_num, (name, (lat, lon)) in enumerate(landmarks.items(), start=1):
        center_x, center_y = lon_to_x(lon), lat_to_y(lat)
        fake_boxes.append({
            "text": name, "left": center_x - 40, "top": center_y - 6,
            "width": 80, "height": 12,
            "block": "1", "par": "1", "line": str(line_num), "word": 1,
        })
    monkeypatch.setattr(
        "core.intel.map_pin_geolocate._ocr_word_boxes",
        lambda image, executable: fake_boxes,
    )

    result = geolocate_pin_from_image(payload)
    assert result is not None
    lat, lon = result
    assert abs(lon - expected_lon) < 0.02
    assert abs(lat - expected_lat) < 0.02


def test_geolocate_pin_from_image_refuses_a_fit_far_from_every_matched_landmark(monkeypatch) -> None:
    """Within the broad Mediterranean lat/lon bounds but ~800 km from both
    matched landmarks — the kind of result a genuinely wrong OCR match (not
    an out-of-theatre one) would produce. The nearest-landmark distance
    guard, not the lat/lon range check, must be what rejects this."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(
        "core.intel.map_pin_geolocate._detect_marker_pixel",
        lambda image: (150, -7240),
    )
    monkeypatch.setattr(
        "core.intel.map_pin_geolocate._ocr_word_boxes",
        lambda image, executable: [
            {"text": "Heraklion", "left": 110, "top": 94, "width": 80, "height": 12,
             "block": "1", "par": "1", "line": "1", "word": 1},
            {"text": "Rethymno", "left": 130, "top": 124, "width": 80, "height": 12,
             "block": "1", "par": "1", "line": "2", "word": 1},
        ],
    )
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (235, 235, 235)).save(buf, format="PNG")
    assert geolocate_pin_from_image(buf.getvalue()) is None


def test_geolocate_pin_from_image_refuses_single_landmark(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(
        "core.intel.map_pin_geolocate._detect_marker_pixel",
        lambda image: (400, 300),
    )
    monkeypatch.setattr(
        "core.intel.map_pin_geolocate._ocr_word_boxes",
        lambda image, executable: [
            {"text": "Heraklion", "left": 100, "top": 100, "width": 80, "height": 12,
             "block": "1", "par": "1", "line": "1", "word": 1},
        ],
    )
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (235, 235, 235)).save(buf, format="PNG")
    assert geolocate_pin_from_image(buf.getvalue()) is None


def test_match_landmarks_does_not_use_crete_region_label_as_point_calibration():
    boxes = [
        _word("Kriti", 180, 190, 60, 14, line="1", word=1),
        _word("Rethimno", 100, 90, 80, 14, line="2", word=1),
    ]
    names = {name for name, _px, _py in _match_landmarks(boxes)}
    assert "rethimno" in names
    assert "kriti" not in names


def test_match_landmarks_recovers_wrapped_and_slightly_misread_map_labels() -> None:
    boxes = [
        _word("AQIOS", 552, 103, 76, 23, block="full-1", line="1", word=6),
        _word("Nikolaos", 555, 133, 119, 25, block="full-1", line="2", word=6),
        _word("Aerapetra", 553, 254, 139, 34, block="full-1", line="4", word=4),
        _word("Chrisi", 488, 393, 77, 24, block="tile0-1", line="10", word=1),
    ]
    matches = {name: (px, py) for name, px, py in _match_landmarks(boxes)}
    assert "agios nikolaos" in matches
    assert "ierapetra" in matches
    assert "chrisi" in matches


def test_match_landmarks_does_not_fuzzy_match_short_noise() -> None:
    boxes = [
        _word("Sea", 100, 80, 40, 15, line="1"),
        _word("A", 200, 120, 10, 15, line="2"),
        _word("Niko", 300, 140, 50, 15, line="3"),
    ]
    assert _match_landmarks(boxes) == []
