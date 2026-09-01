# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/prompt.md P1 -- Alarm Phone media as a first-class evidence object,
plus the pre-merge hardening: SSRF / redirect / private-IP guard, Pillow
decode-verify, explicit outcome states, private-original / public-thumbnail
split, and bounded (never per-event-thread) execution."""
from __future__ import annotations

import io

import pytest

from core.intel import media_evidence as me
from core.intel.media_evidence import (
    MediaEvidence,
    capture_media_evidence,
    classify_media_outcome,
)

# the genuine implementation, captured before the autouse fixture stubs it out
_REAL_FETCH = me._fetch


def _png_bytes(w: int = 24, h: int = 16) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(w: int = 24, h: int = 16) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 60, 80)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fake_io(monkeypatch):
    """Default: every allow-listed URL returns a small real PNG; object store
    writes go to an in-memory dict. Individual tests override _fetch."""
    store: dict[str, bytes] = {}

    monkeypatch.setattr(
        me, "_fetch",
        lambda url: (_png_bytes(), me.FETCH_OK)
        if me._host_allowed(url) else (None, me.FETCH_BLOCKED_HOST),
    )

    def _fake_put(key, data, content_type=None):
        store[key] = bytes(data)

    import core.object_store as obj

    monkeypatch.setattr(obj, "put", _fake_put)
    monkeypatch.setattr(obj, "get", lambda key: store[key])
    return store


_URL = "https://pbs.twimg.com/media/abc.jpg"


# ---------------------------------------------------------------- outcomes ---

def test_media_with_ocr_coordinate(_fake_io):
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
    # private original + public thumbnail both written
    assert f"{me._ORIG_PREFIX}/{ev[0].sha256}.png" in _fake_io
    assert f"{me._PUB_PREFIX}/{ev[0].sha256}.png" in _fake_io
    assert classify_media_outcome(ev, (34.27, 11.94), "text") == me.MEDIA_COORDINATES_FOUND


def test_media_present_but_ocr_ran_with_no_coordinate():
    ev = capture_media_evidence([_URL], ocr_method=None, ocr_coord=None, ocr_ran=True)
    assert ev[0].fetch_status == "ok"
    assert ev[0].ocr_status == me.OCR_NO_COORDINATE
    assert classify_media_outcome(ev, None, None, ocr_ran=True) == me.MEDIA_STORED_NO_LOCATION


def test_media_present_but_ocr_execution_failed():
    ev = capture_media_evidence([_URL], ocr_method=None, ocr_coord=None, ocr_ran=False)
    assert ev[0].ocr_status == me.OCR_EXECUTION_FAILED
    assert classify_media_outcome(ev, None, None, ocr_ran=False) == me.OCR_FAILED


def test_pin_only_image_stays_approximate():
    ev = capture_media_evidence([_URL], ocr_method="easyocr_pin_landmark", ocr_coord=(35.1, 25.7))
    assert ev[0].ocr_status == me.OCR_PIN_ONLY
    assert ev[0].coordinate_candidates[0]["approximate"] is True
    assert classify_media_outcome(ev, (35.1, 25.7), "easyocr_pin_landmark") == me.VISUAL_PIN_ONLY


def test_no_media_tweet():
    ev = capture_media_evidence([])
    assert ev == []
    assert classify_media_outcome(ev, None, None) == me.NO_MEDIA


def test_failed_media_download(monkeypatch):
    monkeypatch.setattr(me, "_fetch", lambda url: (None, me.FETCH_FAILED))
    ev = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(34.0, 12.0))
    assert ev[0].fetch_status == "fetch_failed"
    assert ev[0].stored_media_url is None
    assert ev[0].sha256 is None
    assert classify_media_outcome(ev, (34.0, 12.0), "text") == me.MEDIA_FETCH_FAILED


def test_oversize_media_reported_distinctly(monkeypatch):
    monkeypatch.setattr(me, "_fetch", lambda url: (None, me.FETCH_TOO_LARGE))
    ev = capture_media_evidence([_URL])
    assert ev[0].fetch_status == "too_large"
    assert classify_media_outcome(ev, None, None) == me.MEDIA_TOO_LARGE


def test_blocked_host_reported_distinctly():
    ev = capture_media_evidence(["https://evil.example/x.jpg"], ocr_method="text")
    assert ev[0].fetch_status == "blocked_host"
    assert ev[0].stored_media_url is None
    assert classify_media_outcome(ev, None, None) == me.MEDIA_BLOCKED


# ------------------------------------------------------ image validation ---

def test_undecodable_bytes_never_reach_object_store(monkeypatch, _fake_io):
    monkeypatch.setattr(me, "_fetch", lambda url: (b"not an image at all", me.FETCH_OK))
    ev = capture_media_evidence([_URL])
    assert ev[0].fetch_status == me.FETCH_INVALID_IMAGE
    assert ev[0].stored_media_url is None
    assert _fake_io == {}  # object_store.put never called
    assert classify_media_outcome(ev, None, None) == me.MEDIA_INVALID


def test_content_type_is_not_trusted(monkeypatch, _fake_io):
    # server could claim image/jpeg for an HTML error page; decode is the gate
    monkeypatch.setattr(me, "_fetch", lambda url: (b"<html>429</html>", me.FETCH_OK))
    ev = capture_media_evidence([_URL])
    assert ev[0].fetch_status == me.FETCH_INVALID_IMAGE


def test_decode_bomb_dimensions_rejected(monkeypatch):
    huge = (me._MAX_DECODED_PIXELS // 1000) + 1000
    assert me._validate_image_dims(1000, huge) is False


def test_jpeg_and_png_accepted(monkeypatch):
    monkeypatch.setattr(me, "_fetch", lambda url: (_jpeg_bytes(), me.FETCH_OK))
    ev = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(1.0, 2.0))
    assert ev[0].mime_type == "image/jpeg"
    assert ev[0].stored_media_url.endswith(".jpg")


# ------------------------------------------------ SSRF / redirect / DNS ---

def test_fetch_blocks_non_allowlisted_host_without_network():
    data, status = _REAL_FETCH("https://169.254.169.254/latest/meta-data/")
    assert data is None
    assert status == me.FETCH_BLOCKED_HOST


def test_fetch_blocks_when_host_resolves_to_private_ip(monkeypatch):
    # allow-listed name, but DNS points at a private address -> hard block
    monkeypatch.setattr(
        me.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    data, status = _REAL_FETCH(_URL)
    assert data is None
    assert status == me.FETCH_PRIVATE_IP_BLOCKED


def test_fetch_does_not_follow_redirects(monkeypatch):
    monkeypatch.setattr(
        me, "_resolves_public",
        lambda host: True,
    )

    class _Resp:
        code = 302
        headers = {"Location": "http://127.0.0.1/"}

    class _FakeOpener:
        def open(self, *a, **k):
            import urllib.error

            raise urllib.error.HTTPError(_URL, 302, "redirect", {}, None)

    monkeypatch.setattr(me, "_OPENER", _FakeOpener())
    data, status = _REAL_FETCH(_URL)
    assert data is None
    assert status == me.FETCH_REDIRECT_BLOCKED


def test_fetch_rejects_final_url_off_allowlist(monkeypatch):
    monkeypatch.setattr(me, "_resolves_public", lambda host: True)

    class _Resp:
        headers = {"Content-Type": "image/png"}

        def geturl(self):
            return "https://evil.example/x.png"

        def read(self, n):
            return _png_bytes()

        def close(self):
            pass

    class _FakeOpener:
        def open(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(me, "_OPENER", _FakeOpener())
    data, status = _REAL_FETCH(_URL)
    assert data is None
    assert status == me.FETCH_REDIRECT_BLOCKED


# ----------------------------------------------------- durable / survival ---

def test_stored_copy_survives_original_url_loss():
    ev = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(34.0, 12.0))
    assert ev[0].stored_media_url.startswith("/api/v1/media/")
    assert ev[0].original_media_url == _URL


# ------------------------------------------------------- public projection ---

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
    assert "sha256" not in entry              # even though the URL is content-addressed


# --------------------------------------------------------------- drift gate ---

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


def test_visual_pin_only_never_auto_drifts():
    from core.intel.drift_service import is_auto_drift_eligible
    from core.intel.store import IntelEvent

    event = IntelEvent(
        id="media-drift-2", type="twitter", severity="high", lat=35.0, lon=25.0,
        title="distress", text="", source="alarm_phone",
        metadata={
            "is_distress": True,
            "coordinate_source": "media_ocr_pin_landmark",
            "coordinate_review_status": "machine_ocr_unverified",
            "media_outcome": "visual_pin_only",
        },
    )
    ok, _ = is_auto_drift_eligible(event)
    assert ok is False


# ------------------------------------------------------------- backpressure ---

def test_media_evidence_capture_runs_only_through_bounded_queue(monkeypatch):
    """A media burst must go through the fixed MediaOcrQueue pool, never a
    per-event thread (docs/prompt.md hardening item 6 / docs/fixes.md F-02)."""
    import threading

    from core.intel import twikit_monitor as tm
    from core.intel.media_ocr_queue import MEDIA_OCR_QUEUE_MAXSIZE, media_ocr_queue

    media_ocr_queue.reset()
    baseline_threads = threading.active_count()
    seen: list[str] = []

    monkeypatch.setattr(tm.TwikitMonitor, "_apply_media_ocr",
                        lambda self, event_id, urls: seen.append(event_id))

    monitor = tm.TwikitMonitor.__new__(tm.TwikitMonitor)
    for i in range(MEDIA_OCR_QUEUE_MAXSIZE * 4):
        monitor._schedule_media_ocr(f"tw{i}", f"ev{i}", [_URL])

    # pool is fixed-size: thread growth is bounded by the worker count, not
    # by the number of scheduled jobs
    assert threading.active_count() - baseline_threads <= 8
    media_ocr_queue.reset()


# ------------------------------------------------------- public media route ---

def _route_client():
    from fastapi.testclient import TestClient

    from core.api.main import app

    return TestClient(app)


def test_media_route_serves_only_public_thumbnail(monkeypatch):
    calls: list[str] = []

    def _fake_get(key):
        calls.append(key)
        if key == f"media/pub/{'c' * 64}.jpg":
            return _jpeg_bytes()
        raise FileNotFoundError(key)

    import core.object_store as obj

    monkeypatch.setattr(obj, "get", _fake_get)
    resp = _route_client().get(f"/api/v1/media/{'c' * 64}.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "immutable" not in resp.headers.get("cache-control", "")
    # the route only ever looks under the public prefix -- never media/orig/
    assert calls == [f"media/pub/{'c' * 64}.jpg"]


def test_media_route_is_public_even_with_internal_proxy_secret(monkeypatch):
    from core import config

    monkeypatch.setattr(config, "INTERNAL_PROXY_SECRET", "s3cr3t", raising=False)
    import core.object_store as obj

    monkeypatch.setattr(obj, "get", lambda key: _jpeg_bytes())
    # no x-seacommons-internal header, yet still served
    resp = _route_client().get(f"/api/v1/media/{'d' * 64}.jpg")
    assert resp.status_code == 200


def test_media_route_rejects_bad_key_shape(monkeypatch):
    import core.object_store as obj

    monkeypatch.setattr(obj, "get", lambda key: (_ for _ in ()).throw(AssertionError("lookup attempted")))
    client = _route_client()
    for bad in ("../etc/passwd", "abc", "g" * 64 + ".jpg", "e" * 64, "e" * 64 + ".webp"):
        assert client.get(f"/api/v1/media/{bad}").status_code == 404


def test_media_route_404_for_missing_object(monkeypatch):
    import core.object_store as obj

    monkeypatch.setattr(obj, "get", lambda key: (_ for _ in ()).throw(FileNotFoundError(key)))
    resp = _route_client().get(f"/api/v1/media/{'f' * 64}.png")
    assert resp.status_code == 404
