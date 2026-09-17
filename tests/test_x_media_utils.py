# SPDX-License-Identifier: AGPL-3.0-or-later
"""EasyOCR/Tesseract cross-check in _ocr_photo (x_media_utils.py).

User follow-up ("fix humanitarian tesseract piu preciso"): EasyOCR and
Tesseract were never cross-checked against each other -- if EasyOCR found a
coordinate, Tesseract never even ran, so a wrong EasyOCR read was trusted at
face value. These tests exercise the new cross-check branching directly,
independent of how twikit_monitor.py maps its method string to metadata
(covered separately in test_twikit_monitor.py).
"""
from __future__ import annotations

from core.intel import x_media_utils


def _patch_download(monkeypatch, payload: bytes = b"fake-image-bytes"):
    monkeypatch.setattr(
        x_media_utils,
        "_download_bounded_image",
        lambda _url: payload,
    )


def test_ocr_photo_confirms_when_tesseract_agrees_with_easyocr(monkeypatch):
    monkeypatch.setattr(x_media_utils, "shutil", x_media_utils.shutil)
    monkeypatch.setattr(x_media_utils.shutil, "which", lambda name: "/usr/bin/tesseract")
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        x_media_utils, "_easyocr_image", lambda payload: ((35.500, 24.900), [], True)
    )
    monkeypatch.setattr(
        x_media_utils, "_tesseract_cross_check", lambda payload, executable: (35.501, 24.899)
    )

    coord, attempted, method, diag = x_media_utils._ocr_photo(
        "https://pbs.twimg.com/media/map.jpg"
    )

    assert coord == (35.500, 24.900)
    assert attempted is True
    assert method == "easyocr_tesseract_consensus"
    # docs/fixes.md F-03: the measured inter-engine distance and the threshold
    # used are persisted, in metres.
    assert diag["interengine_distance_m"] < x_media_utils.OCR_CROSS_ENGINE_MAX_DISTANCE_M
    assert diag["consensus_threshold_m"] == x_media_utils.OCR_CROSS_ENGINE_MAX_DISTANCE_M


def test_ocr_photo_disputes_a_kilometre_scale_disagreement_that_degree_delta_missed(monkeypatch):
    """0.03 deg latitude is ~3.3 km -- the old tolerance called this "agreement".
    A ~1.5 km gap between the two engines is a dispute, not a consensus."""
    monkeypatch.setattr(x_media_utils.shutil, "which", lambda name: "/usr/bin/tesseract")
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        x_media_utils, "_easyocr_image", lambda payload: ((35.500, 24.900), [], True)
    )
    monkeypatch.setattr(
        x_media_utils, "_tesseract_cross_check", lambda payload, executable: (35.513, 24.900)
    )
    _coord, _attempted, method, diag = x_media_utils._ocr_photo(
        "https://pbs.twimg.com/media/map.jpg"
    )
    assert method == "easyocr_text_disputed"
    assert 1200 < diag["interengine_distance_m"] < 1600


def test_ocr_photo_flags_dispute_when_tesseract_disagrees_with_easyocr(monkeypatch):
    monkeypatch.setattr(x_media_utils.shutil, "which", lambda name: "/usr/bin/tesseract")
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        x_media_utils, "_easyocr_image", lambda payload: ((35.5, 24.9), [], True)
    )
    # Materially different coordinate -- ~90 km, well outside the metric tolerance.
    monkeypatch.setattr(
        x_media_utils, "_tesseract_cross_check", lambda payload, executable: (36.2, 25.6)
    )

    coord, _attempted, method, _diag = x_media_utils._ocr_photo(
        "https://pbs.twimg.com/media/map.jpg"
    )

    assert coord == (35.5, 24.9)  # EasyOCR's read is kept, just flagged
    assert method == "easyocr_text_disputed"


def test_ocr_photo_keeps_legacy_method_when_tesseract_finds_nothing(monkeypatch):
    monkeypatch.setattr(x_media_utils.shutil, "which", lambda name: "/usr/bin/tesseract")
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        x_media_utils, "_easyocr_image", lambda payload: ((35.5, 24.9), [], True)
    )
    monkeypatch.setattr(x_media_utils, "_tesseract_cross_check", lambda payload, executable: None)

    coord, _attempted, method, _diag = x_media_utils._ocr_photo(
        "https://pbs.twimg.com/media/map.jpg"
    )

    assert coord == (35.5, 24.9)
    assert method == "easyocr_text"


def test_ocr_photo_skips_cross_check_when_tesseract_binary_missing(monkeypatch):
    monkeypatch.setattr(x_media_utils.shutil, "which", lambda name: None)
    # The initial availability guard also accepts "easyocr importable" as an
    # alternative to a tesseract binary; force that branch regardless of
    # whether the real (heavy, optional) easyocr package is installed here.
    monkeypatch.setattr(
        x_media_utils.importlib.util, "find_spec", lambda name: object() if name == "easyocr" else None
    )
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        x_media_utils, "_easyocr_image", lambda payload: ((35.5, 24.9), [], True)
    )

    def _boom(*_a, **_k):
        raise AssertionError("cross-check must not run without a tesseract binary")

    monkeypatch.setattr(x_media_utils, "_tesseract_cross_check", _boom)

    coord, _attempted, method, _diag = x_media_utils._ocr_photo(
        "https://pbs.twimg.com/media/map.jpg"
    )

    assert coord == (35.5, 24.9)
    assert method == "easyocr_text"


def test_ocr_photo_survives_a_broken_cross_check(monkeypatch):
    """A crash in the cross-check pass must never lose an EasyOCR read that
    already succeeded."""
    monkeypatch.setattr(x_media_utils.shutil, "which", lambda name: "/usr/bin/tesseract")
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        x_media_utils, "_easyocr_image", lambda payload: ((35.5, 24.9), [], True)
    )

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(x_media_utils, "_tesseract_cross_check", _raise)

    coord, _attempted, method, _diag = x_media_utils._ocr_photo(
        "https://pbs.twimg.com/media/map.jpg"
    )

    assert coord == (35.5, 24.9)
    assert method == "easyocr_text"


def test_pin_landmark_propagates_solver_uncertainty(monkeypatch):
    import io
    from types import SimpleNamespace

    from core.intel import map_pin_geolocate, x_media_utils
    from PIL import Image

    payload = io.BytesIO()
    Image.new("RGB", (320, 240), "white").save(payload, format="PNG")
    monkeypatch.setattr(x_media_utils, "_easyocr_image", lambda _payload: (None, [{"text": "Heraklion"}], True))
    monkeypatch.setattr(x_media_utils, "ocr_png_coordinate", lambda *_a, **_k: (None, True))
    monkeypatch.setattr(map_pin_geolocate, "geolocate_pin_from_image", lambda *_a, **_k: (34.1, 25.1))
    monkeypatch.setattr(
        map_pin_geolocate,
        "geolocate_pin_detailed",
        lambda *_a, **_k: SimpleNamespace(
            estimated_position_error_m=72_000.0,
            confidence=0.05,
            fit_residual_px=5.0,
            max_extrapolation_px=700.0,
            landmarks_used=["rethimno", "heraklion"],
        ),
    )

    coord, attempted, method, diagnostics = x_media_utils._extract_coordinate_from_bytes(
        payload.getvalue(), executable="/usr/bin/tesseract"
    )
    assert coord == (34.1, 25.1)
    assert attempted is True
    assert method == "easyocr_pin_landmark"
    assert diagnostics["estimated_position_error_m"] == 72_000.0
    assert diagnostics["pin_solver_confidence"] == 0.05


def test_pin_landmark_retries_tesseract_when_easyocr_boxes_cannot_fit(monkeypatch):
    import io
    from types import SimpleNamespace

    from core.intel import map_pin_geolocate, x_media_utils
    from PIL import Image

    payload = io.BytesIO()
    Image.new("RGB", (320, 240), "white").save(payload, format="PNG")
    monkeypatch.setattr(x_media_utils, "_easyocr_image", lambda _payload: (None, [{"text": "Chrisi"}], True))
    monkeypatch.setattr(x_media_utils, "ocr_png_coordinate", lambda *_a, **_k: (None, True))
    calls = []
    def _geo(*_a, **kwargs):
        calls.append(kwargs.get("word_boxes"))
        return None if kwargs.get("word_boxes") else (34.1, 25.1)
    monkeypatch.setattr(map_pin_geolocate, "geolocate_pin_from_image", _geo)
    monkeypatch.setattr(
        map_pin_geolocate,
        "geolocate_pin_detailed",
        lambda *_a, **_k: SimpleNamespace(
            estimated_position_error_m=67_000.0,
            confidence=0.49,
            fit_residual_px=1.3,
            max_extrapolation_px=704.0,
            landmarks_used=["agios nikolaos", "ierapetra"],
        ),
    )
    coord, attempted, method, diagnostics = x_media_utils._extract_coordinate_from_bytes(
        payload.getvalue(), executable="/usr/bin/tesseract"
    )
    assert coord == (34.1, 25.1)
    assert attempted is True
    assert method == "tesseract_pin_landmark"
    assert calls == [[{"text": "Chrisi"}], None]
    assert diagnostics["estimated_position_error_m"] == 67_000.0
