# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/prompt.md P1 -- Alarm Phone media as a first-class evidence object."""
from __future__ import annotations

import io

import pytest

from core.intel import media_evidence as me
from core.intel.media_evidence import (
    MediaEvidence,
    capture_media_evidence,
    classify_media_outcome,
)


def _png_bytes(w: int = 24, h: int = 16) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fake_fetch(monkeypatch):
    """Default: every allow-listed URL returns a small PNG. Tests override."""
    monkeypatch.setattr(me, "_fetch", lambda url: (_png_bytes(), me.FETCH_OK)
                        if me._host_allowed(url) else (None, me.FETCH_BLOCKED_HOST))
    monkeypatch.setattr(me, "_store_durable",
                        lambda data, mime: f"/api/v1/media/{'a' * 64}.png")


_URL = "https://pbs.twimg.com/media/abc.jpg"


# 1. media present + OCR coordinate
def test_media_with_ocr_coordinate():
    ev = capture_media_evidence(
        [_URL], source_url="https://x.com/i/web/status/1",
        ocr_method="text", ocr_coord=(34.27, 11.94), ocr_engine="tesseract",
    )
    assert len(ev) == 1
    assert ev[0].fetch_status == "ok"
    assert ev[0].stored_media_url.endswith(".png")
    assert ev[0].sha256 and len(ev[0].sha256) == 64
    assert ev[0].ocr_status == me.OCR_COORDINATES_FOUND
    assert ev[0].coordinate_candidates[0]["lat"] == 34.27
    assert classify_media_outcome(ev, (34.27, 11.94), "text") == me.MEDIA_COORDINATES_FOUND


# 2. media present + OCR no coordinate
def test_media_with_no_ocr_coordinate():
    ev = capture_media_evidence([_URL], ocr_method=None, ocr_coord=None)
    assert ev[0].fetch_status == "ok"
    assert ev[0].ocr_status == me.OCR_NO_COORDINATE
    assert classify_media_outcome(ev, None, None) == me.OCR_FAILED


# 3. pin-only image -> approximate, never a precise coordinate outcome
def test_pin_only_image_stays_approximate():
    ev = capture_media_evidence([_URL], ocr_method="easyocr_pin_landmark", ocr_coord=(35.1, 25.7))
    assert ev[0].ocr_status == me.OCR_PIN_ONLY
    assert ev[0].coordinate_candidates[0]["approximate"] is True
    assert classify_media_outcome(ev, (35.1, 25.7), "easyocr_pin_landmark") == me.VISUAL_PIN_ONLY


# 4. no-media tweet
def test_no_media_tweet():
    ev = capture_media_evidence([])
    assert ev == []
    assert classify_media_outcome(ev, None, None) == me.MEDIA_NO_LOCATION


# 5. failed media download
def test_failed_media_download(monkeypatch):
    monkeypatch.setattr(me, "_fetch", lambda url: (None, me.FETCH_FAILED))
    ev = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(34.0, 12.0))
    assert ev[0].fetch_status == "fetch_failed"
    assert ev[0].stored_media_url is None
    assert ev[0].sha256 is None
    assert classify_media_outcome(ev, (34.0, 12.0), "text") == me.MEDIA_NO_LOCATION


# 6. non-allow-listed host is never fetched (SSRF guard)
def test_disallowed_host_is_blocked():
    ev = capture_media_evidence(["https://evil.example/x.jpg"], ocr_method="text")
    assert ev[0].fetch_status == "blocked_host"
    assert ev[0].stored_media_url is None


# 7. historical pbs URL unavailable -> the durable copy still serves
def test_stored_copy_survives_original_url_loss():
    ev = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(34.0, 12.0))
    # even if pbs.twimg.com later 404s, the feed still points at our copy
    assert ev[0].stored_media_url.startswith("/api/v1/media/")
    assert ev[0].original_media_url == _URL


# 8. public projection preserves media + never leaks the raw ocr_text
def test_live_projection_exposes_safe_media_evidence():
    from core.intel.store import IntelEvent
    from core.live.projection import _public_intel_feature

    raw = MediaEvidence(
        source_url="https://x.com/i/web/status/9",
        original_media_url=_URL,
        stored_media_url="/api/v1/media/" + "b" * 64 + ".png",
        sha256="b" * 64, mime_type="image/png", width=24, height=16,
        fetch_status="ok", ocr_status="coordinates_found", ocr_engine="tesseract",
        ocr_text="N 34 16.292 E 011 56.538", location_method="text",
        coordinate_candidates=[{"lat": 34.27, "lon": 11.94}],
    )
    event = IntelEvent(
        id="media-proj-1", type="twitter", severity="high", lat=34.27, lon=11.94,
        title="Alarm Phone distress", text="", source="Alarm Phone",
        metadata={
            "is_distress": True, "source_policy": "operator_published",
            "publication_status": "published", "coordinate_source": "media_ocr_text",
            "coordinate_review_status": "machine_ocr_unverified",
            "media_evidence": [raw.as_dict()], "media_outcome": "media_coordinates_found",
        },
    )
    feature = _public_intel_feature(event)
    props = feature["properties"]
    assert props["media_outcome"] == "media_coordinates_found"
    entry = props["media_evidence"][0]
    assert entry["stored_media_url"].endswith(".png")
    assert entry["ocr_status"] == "coordinates_found"
    assert "ocr_text" not in entry            # raw span never leaves the VM
    assert "coordinate_candidates" not in entry
    assert "sha256" not in entry


# E. media / OCR evidence never bypasses the Drift gate
def test_media_ocr_unverified_never_auto_drifts():
    from core.intel.drift_service import is_auto_drift_eligible
    from core.intel.store import IntelEvent

    event = IntelEvent(
        id="media-drift-1", type="twitter", severity="high", lat=34.2, lon=12.0,
        title="distress", text="", source="alarm_phone",
        metadata={
            "is_distress": True,
            "coordinate_source": "media_ocr_text",
            "coordinate_review_status": "machine_ocr_unverified",
            "media_outcome": "media_coordinates_found",
        },
    )
    ok, _ = is_auto_drift_eligible(event)
    assert ok is False
