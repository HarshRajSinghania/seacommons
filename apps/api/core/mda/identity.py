# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vessel identity intelligence — validation + sanctions screening.

Cheap, offline-first checks on the broadcast identity of a vessel:
  * IMO number check-digit validation
  * MMSI MID (first 3 digits) -> flag, and mismatch against the claimed flag
  * synthetic / invalid MMSI heuristics (repeated / sequential digits, an
    unassigned MID, a SART / EPIRB / AtoN range on something that is moving)
  * screening MMSI / IMO / name against the aggregated sanctions lists
    (OpenSanctions maritime bulk + OFAC SDN), cached in the `sanctioned_vessels`
    table and refreshed daily by the scheduler.

`screen()` returns a small set of risk flags; the identity-fraud detector and
the fusion rules consume them.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MID_FILE = Path(__file__).resolve().parents[1] / "data" / "reference" / "mmsi_mid.json"

try:
    _MID: dict[str, str] = json.loads(_MID_FILE.read_text(encoding="utf-8"))
except Exception:  # pragma: no cover
    _MID = {}

# MMSI leading digits that are NOT a normal ship station.
_SPECIAL_PREFIXES = ("00", "99", "98", "970", "972", "974", "111")

# flags most associated with the shadow / dark fleet (for a risk hint only)
_HIGH_RISK_FLAGS = {
    "PA", "LR", "MH", "CM", "PW", "CK", "KM", "GA", "TG", "TZ", "SL", "MN",
    "GN", "GW", "BB", "AG", "VC", "BZ", "DJ", "ST", "GY", "HN",
}


# ── pure validation ─────────────────────────────────────────────────────────

def imo_check_digit_ok(imo: Any) -> Optional[bool]:
    """True/False for a 7-digit IMO number; None when there is nothing to check."""
    s = re.sub(r"\D", "", str(imo or ""))
    if len(s) != 7:
        return None
    total = sum(int(s[i]) * (7 - i) for i in range(6))
    return total % 10 == int(s[6])


def mmsi_flag(mmsi: Any) -> Optional[str]:
    s = re.sub(r"\D", "", str(mmsi or ""))
    if len(s) < 3:
        return None
    return _MID.get(s[:3])


def mmsi_looks_synthetic(mmsi: Any) -> Optional[str]:
    """Return a reason string when the MMSI is not a plausible ship station."""
    s = re.sub(r"\D", "", str(mmsi or ""))
    if len(s) != 9:
        return "wrong_length"
    if s.startswith(_SPECIAL_PREFIXES):
        return "reserved_prefix"
    if len(set(s)) <= 2:
        return "repeated_digits"
    if s in {"123456789", "987654321"} or s == "".join(str((int(s[0]) + i) % 10) for i in range(9)):
        return "sequential_digits"
    if _MID.get(s[:3]) is None:
        return "unassigned_mid"
    return None


# ── sanctions screening ─────────────────────────────────────────────────────

def _ensure_table() -> None:
    from core.db.models import SanctionedVesselDB
    from core.db.session import engine

    SanctionedVesselDB.__table__.create(bind=engine(), checkfirst=True)


def screen(mmsi: Any = None, imo: Any = None, name: str = "", flag: str = "") -> dict[str, Any]:
    """Identity-integrity + sanctions screen. Never raises."""
    flags: list[str] = []
    hits: list[dict[str, Any]] = []

    imo_ok = imo_check_digit_ok(imo)
    if imo_ok is False:
        flags.append("imo_checksum_fail")

    synth = mmsi_looks_synthetic(mmsi)
    if synth:
        flags.append(f"mmsi_{synth}")

    mid_flag = mmsi_flag(mmsi)
    claimed = (flag or "").strip().upper()[:2]
    if mid_flag and claimed and mid_flag != claimed:
        flags.append("flag_mid_mismatch")
    if (mid_flag or claimed) in _HIGH_RISK_FLAGS:
        flags.append("high_risk_flag")

    try:
        hits = _sanctions_lookup(mmsi=mmsi, imo=imo, name=name)
        if hits:
            strong_hit = any(
                set(hit.get("matched_on") or ()) & {"mmsi", "imo"} for hit in hits
            )
            if strong_hit:
                flags.append("sanctions_hit")
            else:
                flags.append("sanctions_name_only_match")
            if any(hit.get("identity_conflicts") for hit in hits):
                flags.append("sanctions_identity_conflict")
    except Exception as exc:  # pragma: no cover
        logger.debug("sanctions lookup failed: %s", exc)

    return {
        "mmsi": str(mmsi) if mmsi else None,
        "imo": str(imo) if imo else None,
        "imo_valid": imo_ok,
        "mid_flag": mid_flag,
        "risk_flags": sorted(set(flags)),
        "sanctions": hits,
    }


def _sanctions_lookup(*, mmsi: Any = None, imo: Any = None, name: str = "") -> list[dict[str, Any]]:
    from sqlalchemy import or_

    from core.db.models import SanctionedVesselDB
    from core.db.session import session_scope

    mmsi_s = re.sub(r"\D", "", str(mmsi or "")) or None
    imo_s = re.sub(r"\D", "", str(imo or "")) or None
    name_s = (name or "").strip().upper() or None
    if not any((mmsi_s, imo_s, name_s)):
        return []
    conds = []
    if mmsi_s:
        conds.append(SanctionedVesselDB.mmsi == mmsi_s)
    if imo_s:
        conds.append(SanctionedVesselDB.imo == imo_s)
    if name_s and len(name_s) >= 4:
        conds.append(SanctionedVesselDB.name_upper == name_s)
    try:
        with session_scope() as db:
            rows = db.query(SanctionedVesselDB).filter(or_(*conds)).limit(10).all()
            results = []
            for row in rows:
                matched_on = []
                if mmsi_s and row.mmsi == mmsi_s:
                    matched_on.append("mmsi")
                if imo_s and row.imo == imo_s:
                    matched_on.append("imo")
                if name_s and row.name_upper == name_s:
                    matched_on.append("name")
                identity_conflicts = []
                if imo_s and row.imo and row.imo != imo_s:
                    identity_conflicts.append("imo_mismatch")
                if mmsi_s and row.mmsi and row.mmsi != mmsi_s:
                    identity_conflicts.append("mmsi_mismatch")
                program = (row.program or "").strip()
                source_url = (
                    "https://sanctionslistservice.ofac.treas.gov/"
                    if row.source_list == "OFAC_SDN"
                    else _OPENSANCTIONS_URL
                )
                results.append(
                    {
                        "list": row.source_list,
                        "name": row.name,
                        "imo": row.imo,
                        "mmsi": row.mmsi,
                        "program": program,
                        "listed_on": row.listed_on,
                        "matched_on": matched_on,
                        "identity_conflicts": identity_conflicts,
                        "reason": (
                            f"Listed under sanctions programme {program}."
                            if program
                            else f"Vessel record is present in {row.source_list}."
                        ),
                        "description": (
                            f"Identity matched on {', '.join(matched_on) or 'vessel name'}. "
                            "This describes the list match; it does not by itself prove "
                            "that the current AIS behaviour is unlawful."
                        ),
                        "source_url": source_url,
                    }
                )
            return results
    except Exception:
        return []


# ── daily refresh ──────────────────────────────────────────────────────────

_OPENSANCTIONS_URL = "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"


def refresh_sanctions() -> dict[str, Any]:
    """Rebuild `sanctioned_vessels` from OpenSanctions (aggregates OFAC + EU +
    UK + UN + national) + OFAC SDN advanced XML. Best-effort; keeps the old
    table if a download fails."""
    _ensure_table()
    rows: list[dict[str, Any]] = []
    try:
        rows += _load_opensanctions()
    except Exception as exc:
        logger.info("refresh_sanctions: OpenSanctions skipped: %s", exc)
    try:
        rows += _load_ofac_sdn()
    except Exception as exc:
        logger.info("refresh_sanctions: OFAC skipped: %s", exc)
    if not rows:
        return {"loaded": 0, "note": "no source reachable — table unchanged"}

    from core.db.models import SanctionedVesselDB
    from core.db.session import session_scope

    with session_scope() as db:
        db.query(SanctionedVesselDB).delete(synchronize_session=False)
        db.bulk_insert_mappings(SanctionedVesselDB, rows)
    logger.info("refresh_sanctions: %d vessel entries loaded", len(rows))
    _record_sanctions_observations(rows)
    return {"loaded": len(rows)}


def _record_sanctions_observations(rows: list[dict[str, Any]]) -> None:
    """docs/fixes.md M1.2: a durable SourceObservation per sanctioned-
    vessel listing row -- the exact "official-list match" evidence M6's
    (not yet built) hypothesis engine will need to cite: "sanctions-
    evasion hypothesis requires an official-list/entity link plus
    behavioural evidence; a sanctions list match alone publishes only the
    official-list fact, never 'evasion'". One batched transaction (this
    runs after a bulk list refresh, not per streamed event like the other
    adapters -- opening a session per row here would be hundreds to
    thousands of round trips for one refresh). Idempotent per row by
    (source_list, best available identifier): a re-refresh of an unchanged
    listing does not grow the table. Best-effort and strictly additive --
    never touches or replaces the sanctioned_vessels bulk-replace write
    path above, which stays authoritative regardless of whether this
    succeeds.
    """
    try:
        from core.db.session import session_scope
        from core.intel.source_observation import record_observation

        with session_scope() as db:
            for row in rows:
                identifier = row.get("imo") or row.get("mmsi") or row.get("name_upper") or ""
                if not identifier:
                    continue
                record_observation(
                    db,
                    service="maritime",
                    lane="intelligence",
                    observation_type="sanctions_list_match",
                    source_name=str(row.get("source_list") or "sanctions_list"),
                    source_policy="official_open",
                    source_id=identifier,
                    observed_at=(
                        row["updated_at"].isoformat()
                        if hasattr(row.get("updated_at"), "isoformat")
                        else str(row.get("updated_at") or datetime.now(timezone.utc).isoformat())
                    ),
                    raw_payload=json.dumps(row, default=str, sort_keys=True),
                    provenance={"program": row.get("program") or "", "listed_on": row.get("listed_on")},
                )
    except Exception as exc:
        logger.info("refresh_sanctions: source_observation batch skipped: %s", exc)


def _load_opensanctions() -> list[dict[str, Any]]:
    import csv
    import io

    import httpx

    r = httpx.get(_OPENSANCTIONS_URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    out: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        if (row.get("schema") or "").strip() != "Vessel":
            continue
        # simple CSV packs identifiers into columns that vary; be liberal
        imo = _digits(row.get("imoNumber") or row.get("registrationNumber") or "")
        mmsi = _digits(row.get("mmsi") or "")
        name = (row.get("name") or row.get("caption") or "").strip()
        if not (imo or mmsi or name):
            continue
        out.append({
            "source_list": "OpenSanctions", "name": name,
            "name_upper": name.upper(), "imo": imo or None, "mmsi": mmsi or None,
            "program": (
                row.get("program_ids") or row.get("sanctions") or row.get("topics") or ""
            )[:120],
            "listed_on": (row.get("first_seen") or "")[:10] or None,
            "updated_at": datetime.now(timezone.utc),
        })
    return out


def _load_ofac_sdn() -> list[dict[str, Any]]:
    import httpx

    r = httpx.get("https://sanctionslistservice.ofac.treas.gov/api/download/sdn.csv",
                  timeout=120, follow_redirects=True)
    if r.status_code != 200:
        r = httpx.get("https://www.treasury.gov/ofac/downloads/sdn.csv", timeout=120)
    r.raise_for_status()
    import csv
    import io
    out: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 12 or row[2].strip().lower() != "vessel":
            continue
        name = row[1].strip().strip('"')
        remarks = row[11]
        imo = _first(re.findall(r"IMO\s*(\d{7})", remarks))
        mmsi = _first(re.findall(r"MMSI\s*(\d{9})", remarks))
        out.append({
            "source_list": "OFAC_SDN", "name": name, "name_upper": name.upper(),
            "imo": imo, "mmsi": mmsi, "program": row[3][:120] if len(row) > 3 else "",
            "listed_on": None, "updated_at": datetime.now(timezone.utc),
        })
    return out


def _digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _first(seq: list[str]) -> Optional[str]:
    return seq[0] if seq else None
