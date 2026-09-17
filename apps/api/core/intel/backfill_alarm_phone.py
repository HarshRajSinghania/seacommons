# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical safe reprocessor for historical Alarm Phone events.

Two independent jobs over the same Alarm Phone rows (docs/prompt.md):

1. Canonicalization -- backfill the explicit IntelEventDB classification
   columns (maritime_domain / operational_tier / humanitarian_case_type /
   incident_lifecycle / location_status / coordinate_review_status /
   location_uncertainty_m) for rows that predate them, using the SAME
   classifiers live ingestion uses. Never touches lat/lon.

2. Coordinate reprocessing -- for a row whose position is missing or weak
   (region_area / place_centroid) or only machine-OCR-unverified, re-fetch
   the tweet image and try for a better coordinate. Never downgrades a
   verified text/consensus coordinate. Never sea-snaps a land case.

Deduplication of translated / near-duplicate posts is NOT done here: that is
the ingestion path's job (twikit_monitor content-hash + source+URL dedup).

Dry-run by default:

    python -m core.intel.backfill_alarm_phone            # report only
    python -m core.intel.backfill_alarm_phone --apply    # canonicalize + reposition
    python -m core.intel.backfill_alarm_phone --apply --drift   # + gated drift

Do NOT pass --drift during historical canonical repair.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_WEAK_COORD_SOURCES = frozenset({"", "none", "None", "region_area", "place_centroid"})
_UNVERIFIED_REVIEW = frozenset(
    {"machine_ocr_unverified", "machine_ocr_disputed_needs_review"}
)
_ALARM_PHONE_SOURCES = ["alarm phone", "alarm_phone", "alarmphone"]
_ALARM_PHONE_HANDLES = ["alarm_phone", "alarmphone"]

# The canonical columns a fully-classified row must carry.
_CANONICAL_COLUMNS = (
    "schema_version",
    "maritime_domain",
    "operational_tier",
    "humanitarian_case_type",
    "incident_lifecycle",
    "location_status",
)


def _is_alarm_phone(row: Any) -> bool:
    source = str(getattr(row, "source", "") or "").lower()
    if source in {"alarm phone", "alarm_phone", "alarmphone"}:
        return True
    meta = getattr(row, "meta", None) or {}
    return str(meta.get("tracked_account") or "").lower() in {"alarm_phone", "alarmphone"}


def _is_weak_position(row: Any) -> bool:
    meta = getattr(row, "meta", None) or {}
    if getattr(row, "lat", None) is None or getattr(row, "lon", None) is None:
        return True
    if str(meta.get("coordinate_source") or "") in _WEAK_COORD_SOURCES:
        return True
    return str(getattr(row, "coordinate_review_status", "") or "") in _UNVERIFIED_REVIEW


def _canonical_needed(row: Any) -> bool:
    meta = getattr(row, "meta", None) or {}
    for col in _CANONICAL_COLUMNS:
        if getattr(row, col, None) in (None, ""):
            return True
    # A coarse fallback mis-tagged not_required is an inconsistency to fix.
    src = str(meta.get("coordinate_source") or "").lower()
    review = str(getattr(row, "coordinate_review_status", "") or "").lower()
    if src in _WEAK_COORD_SOURCES and review in ("", "not_required"):
        return True
    return False


def find_candidates(limit: int = 200) -> list[Any]:
    """Alarm Phone rows needing canonicalization or coordinate reprocessing.

    The Alarm Phone + candidate filter runs at the DB level; ``limit`` is
    applied only *after* it, so a burst of unrelated recent rows can never
    starve the reprocessor (docs/prompt.md sec 2).
    """
    from sqlalchemy import func, or_, select

    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    tracked = IntelEventDB.meta["tracked_account"].as_string()
    coord_src = IntelEventDB.meta["coordinate_source"].as_string()
    is_alarm_phone = or_(
        func.lower(IntelEventDB.source).in_(_ALARM_PHONE_SOURCES),
        func.lower(tracked).in_(_ALARM_PHONE_HANDLES),
    )
    canonical_needed = or_(
        *[getattr(IntelEventDB, col).is_(None) for col in _CANONICAL_COLUMNS]
    )
    coordinate_reprocess_needed = or_(
        IntelEventDB.lat.is_(None),
        IntelEventDB.lon.is_(None),
        coord_src.in_(list(_WEAK_COORD_SOURCES)),
        func.lower(IntelEventDB.coordinate_review_status).in_(list(_UNVERIFIED_REVIEW)),
    )

    with session_scope() as db:
        rows = (
            db.execute(
                select(IntelEventDB)
                .where(is_alarm_phone, or_(canonical_needed, coordinate_reprocess_needed))
                .order_by(IntelEventDB.timestamp_utc.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        out: list[dict] = []
        for row in rows:
            meta = dict(row.meta or {})
            out.append(
                {
                    "id": row.id,
                    "tweet_id": str(meta.get("tweet_id") or ""),
                    "quoted_tweet_id": str(meta.get("quoted_tweet_id") or ""),
                    "media_urls": list(meta.get("media_urls") or []),
                    "coordinate_source": meta.get("coordinate_source"),
                    "coordinate_review_status": row.coordinate_review_status,
                    "timestamp_utc": row.timestamp_utc,
                    "title": (row.title or "")[:80],
                    "text": (row.text or "")[:500],
                    "lat": row.lat,
                    "lon": row.lon,
                    "stored_canonical": {c: getattr(row, c) for c in _CANONICAL_COLUMNS},
                    "canonical_needed": _canonical_needed(row),
                    "coordinate_reprocess_needed": _is_weak_position(row),
                    "persons": meta.get("persons"),
                    "vessel_type": meta.get("vessel_type"),
                }
            )
    return out


# ── Canonicalization (independent of coordinate replacement) ─────────────────
def canonicalize_event(event_id: str, *, apply: bool) -> dict[str, Any]:
    """Recompute the canonical classification for one stored row.

    Returns ``{"changed": bool, "fields": {...}, "wrote": bool}``. Never
    modifies lat/lon or the ``meta`` provenance envelope beyond mirroring the
    recomputed canonical values (and the coordinate_review_status
    correction).
    """
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    from core.intel.humanitarian import canonical_classification
    from core.intel.store import IntelEvent

    with session_scope() as db:
        row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
        if row is None:
            return {"changed": False, "fields": {}, "wrote": False}
        same_source = (
            db.query(IntelEventDB)
            .filter(IntelEventDB.source == row.source)
            .order_by(IntelEventDB.timestamp_utc.desc())
            .limit(200)
            .all()
        )
        # Seed metadata with any classification the row carries only as an
        # explicit column (dual-write may have written one side, not the
        # other) so canonical_classification sees the full picture.
        meta = dict(row.meta or {})
        for col in ("coordinate_review_status", "location_uncertainty_m", "incident_lifecycle"):
            if meta.get(col) is None and getattr(row, col, None) is not None:
                meta[col] = getattr(row, col)
        event = IntelEvent(
            id=str(row.id),
            timestamp_utc=str(row.timestamp_utc or ""),
            type=str(row.type or ""),
            severity=str(row.severity or ""),
            lat=row.lat,
            lon=row.lon,
            title=str(row.title or ""),
            text=str(row.text or ""),
            source=str(row.source or ""),
            metadata=meta,
        )
        same = [
            IntelEvent(
                id=str(r.id), timestamp_utc=str(r.timestamp_utc or ""),
                type=str(r.type or ""), title=str(r.title or ""),
                text=str(r.text or ""), source=str(r.source or ""),
                metadata=dict(r.meta or {}),
            )
            for r in same_source
            if r.id != row.id
        ]
        fields = canonical_classification(event, same_source=same)
        fields["schema_version"] = 1

        current = {c: getattr(row, c) for c in fields}
        changed = any(current.get(k) != v for k, v in fields.items())
        if not changed:
            return {"changed": False, "fields": fields, "wrote": False}
        if not apply:
            return {"changed": True, "fields": fields, "wrote": False}

        merged = dict(row.meta or {})
        for key in ("humanitarian_case_type", "incident_lifecycle", "location_status",
                    "coordinate_review_status", "location_uncertainty_m", "maritime_domain"):
            if fields.get(key) is not None:
                merged[key] = fields[key]
        for col, value in fields.items():
            setattr(row, col, value)
        row.meta = merged
        db.flush()
    return {"changed": True, "fields": fields, "wrote": True}


# ── Coordinate reprocessing ─────────────────────────────────────────────────
def resolve_position(candidate: dict) -> tuple[float, float, str, dict[str, Any]] | None:
    """Best image-derived coordinate plus solver diagnostics, or None."""
    from core.intel.x_media_utils import _ocr_photo, fetch_tweet_photos

    urls = list(candidate.get("media_urls") or [])
    for tweet_id in (candidate.get("tweet_id"), candidate.get("quoted_tweet_id")):
        if tweet_id and not urls:
            urls = fetch_tweet_photos(tweet_id)
        elif tweet_id:
            urls += [u for u in fetch_tweet_photos(tweet_id) if u not in urls]

    for url in urls[:6]:
        try:
            result = _ocr_photo(url)
        except Exception as exc:
            logger.debug("backfill OCR failed for %s: %s", url, exc)
            continue
        coord, method, diagnostics = result[0], result[2], dict(result[3] or {})
        if coord is not None:
            return coord[0], coord[1], method, diagnostics
    return None


def _outcome_for_method(method: str) -> str:
    from core.intel.location_evidence import evidence_from_ocr_method

    evidence = evidence_from_ocr_method(method, None, None)
    if "disputed" in evidence.review_status:
        return "disputed"
    if evidence.coordinate_source == "media_ocr_consensus":
        return "newly_positioned_exact"
    return "newly_positioned_approximate"


def _is_land_case(candidate: dict) -> bool:
    from core.intel.humanitarian import _case_type

    text = f"{candidate.get('title') or ''} {candidate.get('text') or ''}"
    return _case_type(text, distress=False, resolved=False) == "land_humanitarian"


def apply_position(
    event_id: str, lat: float, lon: float, method: str, *,
    estimated_position_error_m: float | None = None,
) -> str:
    """Write an image-derived position back, unless it would downgrade the
    stored evidence. Idempotent. Returns an outcome bucket."""
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    from core.intel.landmask import in_operational_region, nearest_sea_point
    from core.intel.location_evidence import evidence_from_ocr_method, metadata_quality

    if not in_operational_region(lat, lon):
        logger.info("backfill: %s coord %.4f,%.4f out of region -- skipped", event_id, lat, lon)
        return "still_unpositioned"
    lat, lon = nearest_sea_point(float(lat), float(lon))
    new_meta = evidence_from_ocr_method(
        method, lat, lon, estimated_position_error_m=estimated_position_error_m
    ).as_metadata()

    with session_scope() as db:
        row = db.query(IntelEventDB).filter(IntelEventDB.id == event_id).first()
        if row is None:
            return "still_unpositioned"
        existing = dict(row.meta or {})
        if row.lat is not None and metadata_quality(new_meta) <= metadata_quality(existing):
            return "already_good"
        merged = {**existing, **new_meta, "backfilled_at": datetime.now(timezone.utc).isoformat()}
        evidence = [dict(e) for e in (existing.get("media_evidence") or []) if isinstance(e, dict)]
        if evidence:
            target = next((e for e in evidence if e.get("fetch_status") == "ok"), evidence[0])
            target["ocr_engine"] = "easyocr" if str(method).startswith("easyocr") else "tesseract"
            target["location_method"] = method
            target["ocr_status"] = "pin_only" if str(method).endswith("pin_landmark") else "coordinates_found"
            target["coordinate_candidates"] = [{"lat": round(float(lat), 5), "lon": round(float(lon), 5), "method": method, "approximate": str(method).endswith("pin_landmark")}]
            merged["media_evidence"] = evidence
            merged["media_outcome"] = "visual_pin_only" if str(method).endswith("pin_landmark") else "media_coordinates_found"
        for key in ("area_geojson", "area_confidence", "area_weather_narrowed"):
            merged.pop(key, None)
        row.lat = float(lat)
        row.lon = float(lon)
        row.meta = merged
        row.coordinate_review_status = new_meta.get("coordinate_review_status")
        row.location_uncertainty_m = new_meta.get("location_uncertainty_m")
        db.flush()
    return _outcome_for_method(method)


def _backfill_drift_eligible(candidate: dict, lat: float, lon: float, method: str) -> bool:
    from core.intel.drift_service import is_auto_drift_eligible
    from core.intel.location_evidence import evidence_from_ocr_method
    from core.intel.store import IntelEvent

    probe = IntelEvent(
        id=str(candidate["id"]),
        type="twitter",
        lat=lat,
        lon=lon,
        metadata={
            "is_distress": True,
            "incident_lifecycle": (candidate.get("stored_canonical") or {}).get(
                "incident_lifecycle"
            ) or "active",
            **evidence_from_ocr_method(method, lat, lon).as_metadata(),
        },
    )
    eligible, _reason = is_auto_drift_eligible(probe)
    return eligible


_REPORT_KEYS = (
    "scanned",
    "canonicalized",
    "already_canonical",
    "already_good",
    "newly_positioned_exact",
    "newly_positioned_approximate",
    "region_only",
    "still_unpositioned",
    "disputed",
    "land_humanitarian",
    "lifecycle_changed",
    "drift_eligible",
    "drift_rejected",
)
_APPLIED_OUTCOMES = frozenset(
    {"newly_positioned_exact", "newly_positioned_approximate", "disputed"}
)


def run(*, apply: bool, limit: int, with_drift: bool) -> dict:
    """docs/prompt.md: canonicalize + (independently) reprocess coordinates.

    ``scanned`` counts Alarm Phone candidates actually inspected.
    """
    candidates = find_candidates(limit)
    report = {key: 0 for key in _REPORT_KEYS}
    report["scanned"] = len(candidates)

    for candidate in candidates:
        # 1. Canonicalization -- always, independent of the position.
        canon = canonicalize_event(candidate["id"], apply=apply)
        if canon["changed"]:
            report["canonicalized"] += 1
            stored_life = (candidate.get("stored_canonical") or {}).get("incident_lifecycle")
            if canon["fields"].get("incident_lifecycle") != stored_life:
                report["lifecycle_changed"] += 1
        else:
            report["already_canonical"] += 1

        land = canon["fields"].get("humanitarian_case_type") == "land_humanitarian" \
            or _is_land_case(candidate)
        if land:
            report["land_humanitarian"] += 1
            _log(candidate, "land humanitarian -> canonical only, no maritime position")
            continue

        # 2. Coordinate reprocessing -- only when the position needs it.
        if not candidate.get("coordinate_reprocess_needed"):
            _log(candidate, "position already good -> canonical only")
            continue

        position = resolve_position(candidate)
        if position is None:
            bucket = (
                "region_only"
                if str(candidate.get("coordinate_source") or "") == "region_area"
                else "still_unpositioned"
            )
            report[bucket] += 1
            _log(candidate, f"no image coordinate -> {bucket}")
            continue

        if len(position) == 4:
            lat, lon, method, diagnostics = position
        else:  # compatibility with injected/test resolvers and older callers
            lat, lon, method = position
            diagnostics = {}
        estimated_error = diagnostics.get("estimated_position_error_m")
        if apply:
            outcome = apply_position(
                candidate["id"], lat, lon, method,
                estimated_position_error_m=estimated_error,
            )
        else:
            from core.intel.landmask import in_operational_region

            outcome = (
                _outcome_for_method(method)
                if in_operational_region(lat, lon)
                else "still_unpositioned"
            )
        report[outcome] = report.get(outcome, 0) + 1

        if outcome in _APPLIED_OUTCOMES:
            if _backfill_drift_eligible(candidate, lat, lon, method):
                report["drift_eligible"] += 1
                if apply and with_drift:
                    _queue_drift(candidate, lat, lon)
            else:
                report["drift_rejected"] += 1
        _log(candidate, f"{lat:.5f},{lon:.5f} via {method} -> {outcome}")

    report["dry_run"] = not apply
    return report


def _log(candidate: dict, status: str) -> None:
    logger.info(
        "%-14s %s  %s  -> %s",
        str(candidate["id"])[:14],
        candidate.get("timestamp_utc"),
        (candidate.get("title") or "")[:48],
        status,
    )


def _queue_drift(candidate: dict, lat: float, lon: float) -> None:
    try:
        from core.intel.drift_service import schedule_intel_drift

        schedule_intel_drift(
            candidate["id"],
            lat,
            lon,
            candidate.get("persons"),
            candidate.get("vessel_type") or "rubber_boat",
            candidate.get("timestamp_utc") or datetime.now(timezone.utc).isoformat(),
            force=True,
            background=False,
        )
    except Exception as exc:
        logger.warning("backfill drift failed for %s: %s", candidate["id"], exc)



def run_benchmark(directory: str, *, limit: int) -> str:
    """Compare V1/V2 image extraction over local ground-truth JSON, read-only."""
    import glob
    import json
    import os

    from core.intel.image_benchmark import BenchmarkItem, evaluate
    from core.intel.x_media_utils import _download_bounded_image, fetch_tweet_photos

    paths = sorted(glob.glob(os.path.join(directory, "*.json")))[:limit]
    if not paths:
        return f"no ground-truth files in {directory}/*.json"

    items: list[BenchmarkItem] = []
    media_wanted = media_resolved = 0
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            truth = json.load(handle)
        media_wanted += 1
        urls = list(truth.get("media_urls") or [])
        if not urls and truth.get("tweet_id"):
            try:
                urls = fetch_tweet_photos(str(truth["tweet_id"]))
            except Exception as exc:  # pragma: no cover - network best effort
                logger.debug("benchmark media resolve failed for %s: %s", path, exc)
        payload = None
        for url in urls[:6]:
            payload = _download_bounded_image(url)
            if payload is not None:
                break
        if payload is None:
            logger.info("benchmark: no media for %s", os.path.basename(path))
            continue
        media_resolved += 1
        expected = truth.get("expected_coordinate")
        items.append(
            BenchmarkItem(
                name=os.path.splitext(os.path.basename(path))[0],
                image_bytes=payload,
                image_kind=truth.get("image_type"),
                has_coordinate_text=bool(truth.get("has_coordinate_text")),
                has_pin=bool(truth.get("has_pin")),
                expected_coordinate=tuple(expected) if expected else None,
                tolerance_km=float(truth.get("tolerance_km", 25.0)),
            )
        )

    report = evaluate(items)
    header = (
        f"media retrieval recall  {media_resolved}/{media_wanted}"
        f"  ({media_resolved / media_wanted:.0%})\n"
    )
    return header + report.format_text()

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write back (default: dry run)")
    parser.add_argument("--drift", action="store_true", help="gated drift for repositioned events")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="V1/V2 image-pipeline comparison over local ground truth (no DB writes)",
    )
    parser.add_argument(
        "--benchmark-dir",
        default="benchmark/alarm_phone",
        help="directory of ground-truth JSON files",
    )
    args = parser.parse_args()

    if args.benchmark:
        logger.info(run_benchmark(args.benchmark_dir, limit=args.limit))
        return

    from core.db.session import init_database

    init_database()
    summary = run(apply=args.apply, limit=args.limit, with_drift=args.drift)
    logger.info("---")
    for key, value in summary.items():
        logger.info("%-24s %s", key, value)


if __name__ == "__main__":
    main()
