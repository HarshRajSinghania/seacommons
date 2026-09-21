# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vessel identity validation + screening."""
from __future__ import annotations

from core.mda.identity import (
    imo_check_digit_ok,
    mmsi_flag,
    mmsi_looks_synthetic,
    screen,
)


def test_imo_check_digit():
    assert imo_check_digit_ok("9074729") is True      # a real valid IMO
    assert imo_check_digit_ok("9074728") is False
    assert imo_check_digit_ok("123") is None
    assert imo_check_digit_ok("") is None


def test_mmsi_flag():
    assert mmsi_flag("247123456") == "IT"
    assert mmsi_flag("273123456") == "RU"
    assert mmsi_flag("636123456") == "LR"
    assert mmsi_flag("999999999") is None


def test_synthetic_mmsi():
    assert mmsi_looks_synthetic("242424242") == "repeated_digits"
    assert mmsi_looks_synthetic("123456789") == "sequential_digits"
    assert mmsi_looks_synthetic("970123456") == "reserved_prefix"
    assert mmsi_looks_synthetic("111111111") == "reserved_prefix"
    assert mmsi_looks_synthetic("888123456") == "unassigned_mid"
    assert mmsi_looks_synthetic("12345") == "wrong_length"
    assert mmsi_looks_synthetic("247123456") is None


def test_screen_flags_mismatch_and_bad_imo():
    r = screen(mmsi="247123456", imo="9074728", name="TEST", flag="PA")
    assert "imo_checksum_fail" in r["risk_flags"]
    assert "flag_mid_mismatch" in r["risk_flags"]   # MID says IT, claimed PA
    assert r["mid_flag"] == "IT"


def test_screen_clean_vessel():
    r = screen(mmsi="247123456", imo="9074729", name="CLEAN", flag="IT")
    assert r["risk_flags"] == [] or r["risk_flags"] == ["high_risk_flag"] * 0
    assert r["imo_valid"] is True


def test_screen_sanctions_hit(monkeypatch):
    from core.db.models import SanctionedVesselDB
    from core.db.session import engine, session_scope

    SanctionedVesselDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.add(SanctionedVesselDB(source_list="OFAC_SDN", name="SHADOW STAR",
                                  name_upper="SHADOW STAR", imo="9111111", mmsi="273999999",
                                  program="RUSSIA-EO14024"))
    r = screen(mmsi="273999999", name="whatever", flag="RU")
    assert "sanctions_hit" in r["risk_flags"]
    assert r["sanctions"][0]["list"] == "OFAC_SDN"
    assert r["sanctions"][0]["matched_on"] == ["mmsi"]
    assert "RUSSIA-EO14024" in r["sanctions"][0]["reason"]
    assert r["sanctions"][0]["source_url"].startswith("https://")


def test_name_only_sanctions_match_with_conflicting_imo_is_not_sanctions_hit():
    from core.db.models import SanctionedVesselDB
    from core.db.session import engine, session_scope

    SanctionedVesselDB.__table__.create(bind=engine(), checkfirst=True)
    with session_scope() as db:
        db.add(SanctionedVesselDB(
            source_list="OFAC_SDN",
            name="AMBIGUOUS TITAN",
            name_upper="AMBIGUOUS TITAN",
            imo="9293741",
            mmsi=None,
            program="TEST",
        ))
    result = screen(
        mmsi="304496000",
        imo="9126998",
        name="AMBIGUOUS TITAN",
        flag="AG",
    )
    assert "sanctions_hit" not in result["risk_flags"]
    assert "sanctions_name_only_match" in result["risk_flags"]
    assert "sanctions_identity_conflict" in result["risk_flags"]
    hit = result["sanctions"][0]
    assert hit["matched_on"] == ["name"]
    assert hit["identity_conflicts"] == ["imo_mismatch"]
