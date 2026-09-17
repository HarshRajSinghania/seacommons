# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical safe reprocessor for historical Alarm Phone events (docs/prompt.md)."""
from __future__ import annotations

import re

from core.intel import backfill_alarm_phone as bf
from core.intel.x_media_utils import _syndication_token, _x_photo_urls


def _add_row(**over):
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    base = dict(
        id=over.pop("id", "row1"),
        timestamp_utc=over.pop("timestamp_utc", "2026-08-20T00:00:00+00:00"),
        type="twitter",
        severity="high",
        title=over.pop("title", "Boat in distress"),
        text=over.pop("text", ""),
        source=over.pop("source", "alarm_phone"),
        lat=over.pop("lat", None),
        lon=over.pop("lon", None),
        meta=over.pop("meta", {}),
    )
    base.update(over)  # remaining kwargs are explicit columns
    with session_scope() as db:
        db.add(IntelEventDB(**base))
    return base["id"]


def _row(event_id):
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    with session_scope() as db:
        r = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
        if r is None:
            return None
        db.expunge(r)
        return r


# ── existing utility coverage ──────────────────────────────────────────────
def test_syndication_token_is_stripped_base36() -> None:
    token = _syndication_token("1519480761749016577")
    assert token and re.fullmatch(r"[0-9a-z]+", token)
    assert "0" not in token and "." not in token
    assert token == _syndication_token("1519480761749016577")


def test_photo_urls_harvest_media_and_quoted_and_filter_host() -> None:
    payload = {
        "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/media/a.jpg"}],
        "photos": [
            {"url": "https://pbs.twimg.com/media/b.jpg"},
            {"url": "https://evil.example/c.jpg"},
            {"url": "https://pbs.twimg.com/media/a.jpg"},
        ],
        "quoted_tweet": {
            "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/media/quote.jpg"}],
        },
    }
    assert _x_photo_urls(payload) == [
        "https://pbs.twimg.com/media/a.jpg",
        "https://pbs.twimg.com/media/b.jpg",
        "https://pbs.twimg.com/media/quote.jpg",
    ]


# ── A. selector must not starve Alarm Phone behind a recency window ────────
def test_find_candidates_filters_alarm_phone_before_limit() -> None:
    for i in range(900):
        _add_row(
            id=f"noise-{i}",
            source="SeaCommons MDA",
            type="ais_anomaly",
            timestamp_utc=f"2026-08-31T{i % 24:02d}:00:00+00:00",
            meta={"maritime_domain": "grey_zone"},
        )
    _add_row(
        id="ap-old-weak",
        source="Alarm Phone",
        timestamp_utc="2026-08-01T00:00:00+00:00",
        meta={"is_distress": True, "coordinate_source": "region_area"},
    )
    ids = {c["id"] for c in bf.find_candidates(limit=200)}
    assert "ap-old-weak" in ids


# ── B. an unverified-OCR Alarm Phone row is a canonical candidate ──────────
def test_media_ocr_unverified_is_canonical_candidate() -> None:
    _add_row(
        id="ap-ocr-unv",
        source="alarm_phone",
        lat=34.2,
        lon=12.0,
        coordinate_review_status="machine_ocr_unverified",
        meta={"is_distress": True, "coordinate_source": "media_ocr_text"},
    )
    ids = {c["id"] for c in bf.find_candidates(limit=50)}
    assert "ap-ocr-unv" in ids


# ── C. canonicalize without moving a good position, no drift ──────────────
def test_canonicalize_missing_columns_without_replacing_good_position() -> None:
    _add_row(
        id="ap-canon",
        source="alarm_phone",
        lat=34.271,
        lon=11.942,
        text="30 people in distress, engine failure. 34 16.2N 011 56.5E",
        coordinate_review_status="not_required",
        meta={"is_distress": True, "coordinate_source": "post_text",
              "location_uncertainty_m": 250},
    )
    result = bf.canonicalize_event("ap-canon", apply=True)
    assert result["wrote"] is True
    r = _row("ap-canon")
    assert (r.lat, r.lon) == (34.271, 11.942)  # position untouched
    assert r.maritime_domain == "sar"
    assert r.operational_tier == "operational"
    assert r.humanitarian_case_type == "distress"
    assert r.incident_lifecycle in {"active", "needs_review", "archived"}
    assert r.location_status == "positioned"
    assert r.coordinate_review_status == "not_required"
    assert r.schema_version == 1
    # provenance envelope preserved
    assert r.meta["coordinate_source"] == "post_text"


# ── D. NULL stored lifecycle must count as a canonical change ──────────────
def test_null_lifecycle_counts_as_canonical_change() -> None:
    _add_row(
        id="ap-nolife",
        source="alarm_phone",
        lat=34.0,
        lon=12.0,
        text="Boat in distress off Lampedusa",
        meta={"is_distress": True, "coordinate_source": "post_text"},
    )
    assert _row("ap-nolife").incident_lifecycle is None
    result = bf.canonicalize_event("ap-nolife", apply=False)
    assert result["changed"] is True
    assert result["fields"]["incident_lifecycle"] is not None


# ── E. dry run writes nothing ────────────────────────────────────────────
def test_dry_run_never_writes() -> None:
    _add_row(
        id="ap-dry",
        source="alarm_phone",
        lat=34.0,
        lon=12.0,
        text="Boat in distress",
        meta={"is_distress": True, "coordinate_source": "region_area"},
    )
    before = _row("ap-dry")
    snap = (before.lat, before.lon, before.maritime_domain, before.incident_lifecycle,
            dict(before.meta))

    bf.run(apply=False, limit=10, with_drift=False)

    after = _row("ap-dry")
    assert (after.lat, after.lon, after.maritime_domain, after.incident_lifecycle,
            dict(after.meta)) == snap


def test_benchmark_mode_reads_json_resolves_media_and_writes_nothing(tmp_path, monkeypatch) -> None:
    """docs/prompt.md §12: --benchmark runs V1/V2 over local ground truth and
    touches no database."""
    import json

    from tests.fixtures.alarm_phone_images import CASES_BY_NAME

    case = CASES_BY_NAME["pin_only_red"]
    (tmp_path / "case1.json").write_text(json.dumps({
        "tweet_id": "111",
        "image_type": "map_screenshot",
        "has_coordinate_text": False,
        "has_pin": True,
        "expected_coordinate": list(case.expected_coordinate),
        "tolerance_km": 25.0,
    }))

    monkeypatch.setattr(
        "core.intel.x_media_utils.fetch_tweet_photos",
        lambda tweet_id: ["https://pbs.twimg.com/media/x.jpg"],
    )
    monkeypatch.setattr(
        "core.intel.x_media_utils._download_bounded_image", lambda url: case.render()
    )
    evaluated = {}

    def _fake_evaluate(items, **kw):
        evaluated["items"] = items
        return _FakeReport()

    monkeypatch.setattr("core.intel.image_benchmark.evaluate", _fake_evaluate)

    text = bf.run_benchmark(str(tmp_path), limit=10)
    assert "media retrieval recall  1/1" in text
    assert len(evaluated["items"]) == 1
    assert evaluated["items"][0].has_pin is True


class _FakeReport:
    def format_text(self) -> str:
        return "V1/V2 disagreements: 0"


# ── F. no permanently-zero misleading report bucket ──────────────────────
def test_report_buckets_are_honest() -> None:
    from core.db.session import session_scope

    with session_scope():
        pass
    report = bf.run(apply=False, limit=1, with_drift=False)
    # deduplication is the ingestion path's job, not this reprocessor's --
    # there must be no misleading always-zero duplicate bucket.
    assert "duplicate_merged" not in report
    assert set(bf._REPORT_KEYS) <= set(report)
    assert "canonicalized" in report and "already_canonical" in report


# ── run() end-to-end ─────────────────────────────────────────────────────
def test_run_canonicalizes_and_reprocesses(monkeypatch) -> None:
    _add_row(
        id="ap-both",
        source="alarm_phone",
        text="20 people in distress south of Crete",
        meta={"is_distress": True, "coordinate_source": "region_area"},
    )
    monkeypatch.setattr(bf, "resolve_position", lambda c: (35.4, 24.9, "text"))
    monkeypatch.setattr("core.intel.landmask.in_operational_region", lambda *a: True)
    monkeypatch.setattr("core.intel.landmask.nearest_sea_point", lambda a, b: (a, b))
    monkeypatch.setattr("core.intel.landmask.is_on_land", lambda a, b: False)

    report = bf.run(apply=True, limit=10, with_drift=False)
    assert report["scanned"] == 1
    assert report["canonicalized"] == 1
    assert report["newly_positioned_approximate"] == 1
    assert report["drift_eligible"] == 1  # policy /2: OCR sea coordinate is a drift origin
    r = _row("ap-both")
    assert r.humanitarian_case_type == "distress"
    assert (r.lat, r.lon) == (35.4, 24.9)


def test_run_apply_drifts_a_sea_coordinate_recovered_by_backfill(monkeypatch) -> None:
    _add_row(
        id="ap-drift",
        source="alarm_phone",
        text="Boat in distress",
        meta={"is_distress": True, "coordinate_source": "region_area", "persons": 12},
    )
    monkeypatch.setattr(bf, "resolve_position", lambda c: (35.5, 14.1, "pin_landmark"))
    monkeypatch.setattr("core.intel.landmask.in_operational_region", lambda *a: True)
    monkeypatch.setattr("core.intel.landmask.nearest_sea_point", lambda a, b: (a, b))
    monkeypatch.setattr("core.intel.landmask.is_on_land", lambda a, b: False)
    drift_calls: list = []
    import core.intel.drift_service as ds
    monkeypatch.setattr(ds, "schedule_intel_drift", lambda *a, **k: (drift_calls.append(a), True)[1])

    report = bf.run(apply=True, limit=10, with_drift=True)
    assert report["drift_eligible"] == 1 and report["drift_rejected"] == 0
    assert len(drift_calls) == 1



def test_apply_position_preserves_solver_uncertainty(monkeypatch) -> None:
    _add_row(
        id="ap-pin-unc", source="alarm_phone", lat=34.8, lon=24.8,
        coordinate_review_status="not_applicable",
        meta={"coordinate_source": "region_area", "area_geojson": {"type": "Polygon", "coordinates": []}},
    )
    monkeypatch.setattr("core.intel.landmask.in_operational_region", lambda *a: True)
    monkeypatch.setattr("core.intel.landmask.nearest_sea_point", lambda a, b: (a, b))
    outcome = bf.apply_position(
        "ap-pin-unc", 34.2, 25.4, "easyocr_pin_landmark",
        estimated_position_error_m=63_118.0,
    )
    assert outcome == "newly_positioned_approximate"
    r = _row("ap-pin-unc")
    assert (r.lat, r.lon) == (34.2, 25.4)
    assert r.location_uncertainty_m == 63_118.0
    assert r.meta["coordinate_source"] == "media_pin_landmark"
    assert "area_geojson" not in r.meta

def test_apply_position_never_downgrades_and_is_idempotent() -> None:
    _add_row(
        id="ap-nodown",
        source="alarm_phone",
        lat=34.0,
        lon=12.0,
        coordinate_review_status="not_required",
        meta={"coordinate_source": "post_text", "coordinate_review_status": "not_required"},
    )
    outcome = bf.apply_position("ap-nodown", 35.0, 13.0, "pin_landmark")
    assert outcome == "already_good"
    r = _row("ap-nodown")
    assert (r.lat, r.lon) == (34.0, 12.0)


def test_land_humanitarian_candidate_gets_no_maritime_position(monkeypatch) -> None:
    _add_row(
        id="ap-land",
        source="alarm_phone",
        title="Group located in the forest near Evros, taken to a reception centre",
        meta={"coordinate_source": "region_area"},
    )
    calls: list = []
    monkeypatch.setattr(bf, "resolve_position", lambda c: calls.append(c) or (35.5, 14.1, "text"))
    report = bf.run(apply=True, limit=10, with_drift=True)
    assert report["land_humanitarian"] == 1
    assert calls == []
