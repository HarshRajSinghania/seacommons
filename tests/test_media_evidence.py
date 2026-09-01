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
    from core.config import config

    monkeypatch.setattr(config, "INTERNAL_PROXY_SECRET", "s3cr3t")
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


# ============================================================================
# FINAL MERGE GATE -- executable verification (docs/prompt.md)
# ============================================================================

class _RaisingOpener:
    """Any network attempt is a test failure."""

    def open(self, *_a, **_k):  # noqa: D401
        raise AssertionError("network request executed after a security rejection")


# ---- 2A. FETCH / SSRF ------------------------------------------------------

def test_no_network_call_after_host_rejection(monkeypatch):
    monkeypatch.setattr(me, "_OPENER", _RaisingOpener())
    data, status = _REAL_FETCH("https://evil.example/x.jpg")
    assert (data, status) == (None, me.FETCH_BLOCKED_HOST)


def test_no_network_call_after_private_ip_rejection(monkeypatch):
    monkeypatch.setattr(me, "_OPENER", _RaisingOpener())
    monkeypatch.setattr(me.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    data, status = _REAL_FETCH(_URL)
    assert (data, status) == (None, me.FETCH_PRIVATE_IP_BLOCKED)


def test_pbs_resolving_to_loopback_blocked(monkeypatch):
    monkeypatch.setattr(me, "_OPENER", _RaisingOpener())
    monkeypatch.setattr(me.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    assert _REAL_FETCH(_URL) == (None, me.FETCH_PRIVATE_IP_BLOCKED)


def test_ipv6_loopback_linklocal_reserved_blocked(monkeypatch):
    monkeypatch.setattr(me, "_OPENER", _RaisingOpener())
    for addr in ("::1", "fe80::1", "::ffff:10.0.0.1"):
        monkeypatch.setattr(me.socket, "getaddrinfo",
                            lambda *a, _addr=addr, **k: [(10, 1, 6, "", (_addr, 443, 0, 0))])
        assert _REAL_FETCH(_URL) == (None, me.FETCH_PRIVATE_IP_BLOCKED), addr


def test_redirect_307_to_private_target_blocked(monkeypatch):
    monkeypatch.setattr(me, "_resolves_public", lambda host: True)

    class _O:
        def open(self, *a, **k):
            raise me.urllib.error.HTTPError(_URL, 307, "redirect", {}, None)

    monkeypatch.setattr(me, "_OPENER", _O())
    assert _REAL_FETCH(_URL) == (None, me.FETCH_REDIRECT_BLOCKED)


# ---- 2B. IMAGE VALIDATION ------------------------------------------------

def test_malformed_png_rejected(monkeypatch, _fake_io):
    truncated = _png_bytes()[:20]
    monkeypatch.setattr(me, "_fetch", lambda url: (truncated, me.FETCH_OK))
    ev = capture_media_evidence([_URL])
    assert ev[0].fetch_status == me.FETCH_INVALID_IMAGE
    assert _fake_io == {}


def test_unsupported_decoded_format_rejected(monkeypatch, _fake_io):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="BMP")
    monkeypatch.setattr(me, "_fetch", lambda url: (buf.getvalue(), me.FETCH_OK))
    ev = capture_media_evidence([_URL])
    assert ev[0].fetch_status == me.FETCH_INVALID_IMAGE
    assert _fake_io == {}


def test_oversized_byte_body_rejected_by_real_fetch(monkeypatch):
    monkeypatch.setattr(me, "_resolves_public", lambda host: True)
    big = b"\xff" * (me._MAX_IMAGE_BYTES + 5)

    class _Resp:
        headers = {"Content-Type": "image/jpeg"}

        def geturl(self):
            return _URL

        def read(self, n):
            return big[:n]

        def close(self):
            pass

    class _O:
        def open(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(me, "_OPENER", _O())
    assert _REAL_FETCH(_URL) == (None, me.FETCH_TOO_LARGE)


def test_decoded_pixel_cap_rejects_large_image(monkeypatch, _fake_io):
    monkeypatch.setattr(me, "_MAX_DECODED_PIXELS", 100)  # 24*16 = 384 > 100
    monkeypatch.setattr(me, "_fetch", lambda url: (_png_bytes(24, 16), me.FETCH_OK))
    ev = capture_media_evidence([_URL])
    assert ev[0].fetch_status == me.FETCH_INVALID_IMAGE
    assert _fake_io == {}


# ---- 2C. STORAGE separation --------------------------------------------

def test_original_and_public_derivative_land_in_separate_prefixes(monkeypatch, _fake_io):
    original = _jpeg_bytes(48, 32)
    monkeypatch.setattr(me, "_fetch", lambda url: (original, me.FETCH_OK))
    ev = capture_media_evidence([_URL])
    sha = ev[0].sha256
    orig_key = f"{me._ORIG_PREFIX}/{sha}.jpg"
    pub_key = f"{me._PUB_PREFIX}/{sha}.jpg"
    assert set(_fake_io) == {orig_key, pub_key}
    assert _fake_io[orig_key] == original                     # byte-identical original
    assert _fake_io[pub_key] != original                      # re-encoded derivative
    # derivative is still a valid, decodable JPEG
    from PIL import Image

    with Image.open(io.BytesIO(_fake_io[pub_key])) as im:
        assert im.format == "JPEG"


def test_public_derivative_is_jpg_or_png_only(monkeypatch, _fake_io):
    from PIL import Image

    for fmt, saver in (("WEBP", "WEBP"), ("GIF", "GIF")):
        _fake_io.clear()
        buf = io.BytesIO()
        Image.new("RGB", (12, 12), (9, 9, 9)).save(buf, format=saver)
        monkeypatch.setattr(me, "_fetch", lambda url, _b=buf.getvalue(): (_b, me.FETCH_OK))
        ev = capture_media_evidence([_URL])
        assert ev[0].stored_media_url.endswith(".jpg"), fmt
        pub_key = next(k for k in _fake_io if k.startswith(me._PUB_PREFIX))
        with Image.open(io.BytesIO(_fake_io[pub_key])) as im:
            assert im.format in ("JPEG", "PNG")


# ---- 3. AUTHORIZATION BOUNDARY ---------------------------------------

def test_unrelated_protected_route_still_requires_auth(monkeypatch):
    from core.config import config

    monkeypatch.setattr(config, "INTERNAL_PROXY_SECRET", "s3cr3t")
    resp = _route_client().get("/api/v1/intel/events")
    assert resp.status_code == 401


def test_media_route_encoded_traversal_returns_404(monkeypatch):
    import core.object_store as obj

    monkeypatch.setattr(obj, "get",
                        lambda key: (_ for _ in ()).throw(AssertionError("lookup attempted")))
    client = _route_client()
    for bad in ("%2e%2e%2fpasswd", "%2e%2e%5cwin", f"{'a' * 64}.jpg%00", "orig%2f" + "a" * 64):
        assert client.get(f"/api/v1/media/{bad}").status_code == 404


def test_media_route_cannot_reach_private_original(monkeypatch):
    sha = "a" * 64
    store = {f"media/orig/{sha}.jpg": _jpeg_bytes()}  # only the private original exists
    import core.object_store as obj

    monkeypatch.setattr(obj, "get", lambda key: store[key])
    resp = _route_client().get(f"/api/v1/media/{sha}.jpg")
    assert resp.status_code == 404  # route only reads media/pub/


# ---- 4. EDGE PUBLISHER PROJECTION PRIVACY ----------------------------

def _alarm_phone_row_with_media():
    from types import SimpleNamespace

    raw = MediaEvidence(
        source_url="https://x.com/i/web/status/42",
        original_media_url="https://pbs.twimg.com/media/orig-secret.jpg",
        stored_media_url="/api/v1/media/" + "e" * 64 + ".jpg",
        sha256="e" * 64, mime_type="image/jpeg", width=1280, height=720,
        fetch_status="ok", ocr_status="coordinates_found", ocr_engine="tesseract",
        ocr_text="N 34 16.292 E 011 56.538", location_method="text",
        coordinate_candidates=[{"lat": 34.27, "lon": 11.94, "method": "text"}],
    )
    meta = {
        "is_distress": True, "confidence": 0.72, "location_uncertainty_m": 5000,
        "source_policy": "operator_published", "publication_status": "published",
        "media_urls": ["https://pbs.twimg.com/media/orig-secret.jpg"],
        "coordinate_source": "media_ocr_text",
        "coordinate_review_status": "machine_ocr_unverified",
        "media_evidence": [raw.as_dict()], "media_outcome": "media_coordinates_found",
    }
    return SimpleNamespace(
        id="evt-media-1", type="distress", severity="critical", lat=34.27, lon=11.94,
        title="Boat in distress", text="Alarm Phone alert", url="https://x.com/i/web/status/42",
        source="alarm_phone", linked_mmsi="", timestamp_utc="2026-08-02T12:00:00+00:00",
        meta=meta,
    )


_FORBIDDEN_IN_PUBLIC = (
    "orig-secret", "media/orig/", "N 34 16.292", '"ocr_text"',
    '"coordinate_candidates"', '"sha256"',
)


def test_edge_projection_never_leaks_private_media_fields():
    import json
    from datetime import datetime, timezone

    from core.live_edge_publisher import public_event_from_row

    payload = public_event_from_row(
        _alarm_phone_row_with_media(), "node",
        now=datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc), same_source=[],
    )
    assert payload is not None
    blob = json.dumps(payload)
    for needle in _FORBIDDEN_IN_PUBLIC:
        assert needle not in blob, needle
    props = payload["properties"]
    entry = props["media_evidence"][0]
    assert entry["stored_media_url"] == "/api/v1/media/" + "e" * 64 + ".jpg"
    assert props["media_outcome"] == "media_coordinates_found"


def test_vm_projection_never_leaks_private_media_fields():
    import json

    from core.intel.store import IntelEvent
    from core.live.projection import _public_intel_feature

    row = _alarm_phone_row_with_media()
    event = IntelEvent(
        id=row.id, type="twitter", severity="high", lat=row.lat, lon=row.lon,
        title=row.title, text="", source="Alarm Phone", metadata=row.meta,
    )
    blob = json.dumps(_public_intel_feature(event))
    for needle in _FORBIDDEN_IN_PUBLIC:
        assert needle not in blob, needle


# ---- 5. OUTCOME SEMANTICS (each independently) ----------------------

def test_every_outcome_state_is_reachable(monkeypatch):
    # no_media
    assert classify_media_outcome([], None, None) == me.NO_MEDIA
    # media_blocked
    assert classify_media_outcome(
        capture_media_evidence(["https://evil.example/a.jpg"]), None, None) == me.MEDIA_BLOCKED
    # media_fetch_failed
    monkeypatch.setattr(me, "_fetch", lambda url: (None, me.FETCH_FAILED))
    assert classify_media_outcome(capture_media_evidence([_URL]), None, None) == me.MEDIA_FETCH_FAILED
    # media_too_large
    monkeypatch.setattr(me, "_fetch", lambda url: (None, me.FETCH_TOO_LARGE))
    assert classify_media_outcome(capture_media_evidence([_URL]), None, None) == me.MEDIA_TOO_LARGE
    # media_invalid
    monkeypatch.setattr(me, "_fetch", lambda url: (b"junk", me.FETCH_OK))
    assert classify_media_outcome(capture_media_evidence([_URL]), None, None) == me.MEDIA_INVALID
    # stored + OCR ran, no coordinate -> media_stored_no_location
    monkeypatch.setattr(me, "_fetch", lambda url: (_png_bytes(), me.FETCH_OK))
    ev = capture_media_evidence([_URL], ocr_ran=True)
    assert classify_media_outcome(ev, None, None, ocr_ran=True) == me.MEDIA_STORED_NO_LOCATION
    # stored + OCR execution failed -> ocr_failed
    ev = capture_media_evidence([_URL], ocr_ran=False)
    assert classify_media_outcome(ev, None, None, ocr_ran=False) == me.OCR_FAILED
    # accepted machine coordinate -> media_coordinates_found
    ev = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(1.0, 2.0))
    assert classify_media_outcome(ev, (1.0, 2.0), "text") == me.MEDIA_COORDINATES_FOUND
    # visual pin only
    ev = capture_media_evidence([_URL], ocr_method="easyocr_pin_landmark", ocr_coord=(1.0, 2.0))
    assert classify_media_outcome(ev, (1.0, 2.0), "easyocr_pin_landmark") == me.VISUAL_PIN_ONLY


def test_three_ocr_states_are_distinct():
    not_attempted = capture_media_evidence([_URL], ocr_method=None, ocr_coord=None, ocr_ran=False)
    ran_no_coord = capture_media_evidence([_URL], ocr_method=None, ocr_coord=None, ocr_ran=True)
    ran_with_coord = capture_media_evidence([_URL], ocr_method="text", ocr_coord=(1.0, 2.0))
    assert not_attempted[0].ocr_status == me.OCR_EXECUTION_FAILED
    assert ran_no_coord[0].ocr_status == me.OCR_NO_COORDINATE
    assert ran_with_coord[0].ocr_status == me.OCR_COORDINATES_FOUND
    assert len({not_attempted[0].ocr_status, ran_no_coord[0].ocr_status,
                ran_with_coord[0].ocr_status}) == 3


def test_accepted_machine_coordinate_carries_unverified_review_status():
    from core.intel.location_evidence import evidence_from_ocr_method

    md = evidence_from_ocr_method("text", 34.27, 11.94).as_metadata()
    assert md["coordinate_review_status"] == "machine_ocr_unverified"


# ---- 6. QUEUE / BACKPRESSURE (real MediaOcrQueue) -------------------

def test_queue_worker_exception_does_not_kill_future_jobs():
    from core.intel.media_ocr_queue import media_ocr_queue

    media_ocr_queue.reset()
    done: list[str] = []

    def _boom():
        raise RuntimeError("worker blew up")

    media_ocr_queue.submit("k-bad", _boom)
    import time as _t

    _t.sleep(0.2)
    media_ocr_queue.submit("k-good", lambda: done.append("ok"))
    _t.sleep(0.3)
    assert done == ["ok"]
    media_ocr_queue.reset()


def test_queue_burst_is_bounded_and_overflow_is_explicit():
    from core.intel.media_ocr_queue import (
        MEDIA_OCR_QUEUE_MAXSIZE,
        MEDIA_OCR_WORKERS,
        media_ocr_queue,
    )

    assert MEDIA_OCR_WORKERS >= 1 and MEDIA_OCR_QUEUE_MAXSIZE >= 1
    media_ocr_queue.reset()
    import threading

    gate = threading.Event()
    _wait = lambda: gate.wait(5)
    outcomes = [
        media_ocr_queue.submit(f"burst:{i}", _wait)
        for i in range(MEDIA_OCR_QUEUE_MAXSIZE * 4)
    ]
    assert "deferred_queue_full" in outcomes or "dropped" in outcomes
    assert media_ocr_queue.submit("burst:0", _wait) == "deduplicated"
    gate.set()
    media_ocr_queue.reset()


# ---- 7. DRIFT F-01: force cannot bypass the evidence gate -----------

def _ineligible_media_event(outcome, review="machine_ocr_unverified", source="media_ocr_text"):
    from core.intel.store import IntelEvent

    return IntelEvent(
        id=f"drift-force-{outcome}", type="twitter", severity="high", lat=34.2, lon=12.0,
        title="distress", text="", source="alarm_phone",
        metadata={
            "is_distress": True, "coordinate_source": source,
            "coordinate_review_status": review, "media_outcome": outcome,
        },
    )


def test_force_scheduling_cannot_bypass_evidence_gate(monkeypatch):
    from core.intel import drift_service

    ev = _ineligible_media_event("media_coordinates_found")
    updates: dict = {}

    class _FakeStore:
        def get(self, _id):
            return ev

        def update_metadata(self, _id, metadata):
            updates.update(metadata)

    monkeypatch.setattr(drift_service, "intel_store", _FakeStore())
    monkeypatch.setattr(drift_service, "acquire_drift_slot",
                        lambda: (_ for _ in ()).throw(AssertionError("slot acquired for ineligible event")))

    scheduled = drift_service.schedule_intel_drift(
        ev.id, ev.lat, ev.lon, None, None, "2026-08-02T12:00:00+00:00",
        force=True, background=False,
    )
    assert scheduled is False
    assert updates.get("drift_status") == "ineligible"


# ---- 8. COMPATIBILITY --------------------------------------------------

def test_meta_media_urls_still_written_by_twikit():
    import inspect

    from core.intel import twikit_monitor

    # the compat key the edge/backfill still read is written unchanged
    assert '"media_urls": media_urls[:6]' in inspect.getsource(twikit_monitor)


def test_event_without_media_evidence_still_projects():
    from core.intel.store import IntelEvent
    from core.live.projection import _public_intel_feature

    event = IntelEvent(
        id="no-media-1", type="twitter", severity="high", lat=34.0, lon=12.0,
        title="distress", text="", source="Alarm Phone",
        metadata={"is_distress": True, "source_policy": "operator_published",
                  "publication_status": "published"},
    )
    feature = _public_intel_feature(event)
    assert "media_evidence" not in feature["properties"]
    assert "media_outcome" not in feature["properties"]


def test_media_fetch_failure_is_best_effort_and_never_raises(monkeypatch):
    monkeypatch.setattr(me, "_fetch",
                        lambda url: (_ for _ in ()).throw(RuntimeError("network gone")))
    # capture_media_evidence must not propagate -- twikit wraps it, but the
    # per-URL failure itself is contained here
    try:
        ev = capture_media_evidence([_URL])
    except RuntimeError:
        raise AssertionError("capture_media_evidence leaked a fetch exception")
    assert ev[0].fetch_status in (me.FETCH_FAILED, me.FETCH_NOT_ATTEMPTED)
