# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coordinate extraction: region gating and sea-snapping.

Every coordinate this module returns is a boat position — it must be inside
the area SeaCommons covers and in the water.
"""
from __future__ import annotations

from core.intel.geoextract import (
    extract_coords,
    extract_numeric_coords,
    extract_relative_coords,
    is_concluded_incident,
    is_direct_distress_call,
    is_distress,
)


def test_numeric_coords_outside_the_region_are_rejected() -> None:
    # Real prod case: a text-only tweet about AlarmPhone activists in
    # Cote d'Ivoire yielded "-7, 44" (the Indian Ocean).
    assert extract_numeric_coords("meeting in Abidjan on -7, 44 next week") is None
    # Mid-Atlantic and the Gulf are out too.
    assert extract_numeric_coords("Position 15.0N 40.0W") is None


def test_numeric_coords_inside_the_region_pass() -> None:
    assert extract_numeric_coords("Position: 35.10N 013.50E") == (35.1, 13.5)


def test_concluded_incident_recognises_present_perfect_found_and_reception_centre() -> None:
    # Real prod case: a reply threaded onto an active case ("@HCoastGuard We
    # have now learned that the people have been found and taken to a
    # reception centre. We wish them the best for their future in Europe!")
    # left the incident stuck on needs_review because neither existing
    # pattern matches present-perfect "have been found" (only "were/was
    # found") nor "taken to a reception centre" at all.
    assert is_concluded_incident(
        "We have now learned that the people have been found and taken to "
        "a reception centre. We wish them the best for their future in Europe!"
    ) is True


def test_ongoing_incident_still_overrides_present_perfect_found() -> None:
    # The broadened "found" pattern must not defeat the existing ongoing-
    # danger override — the same guard is_resolved_distress documents.
    assert is_concluded_incident(
        "They have been found by the police. Since then we have no news."
    ) is False
    # Canary Islands / Atlantic route stays in.
    lat, lon = extract_numeric_coords("N 28° 30' / W 015° 00'")
    assert abs(lat - 28.5) < 0.01 and abs(lon + 15.0) < 0.01


def test_extract_coords_sea_snaps_a_numeric_readout(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.intel.landmask.is_on_land",
        lambda lat, lon: abs(lat - 35.5) < 0.05 and abs(lon - 12.6) < 0.05,
    )
    result = extract_coords("boat in distress at 35.50N 12.60E")
    assert result is not None
    lat, lon = result
    assert not (abs(lat - 35.5) < 0.05 and abs(lon - 12.6) < 0.05)  # moved off Lampedusa


def test_relative_offset_is_sea_snapped(monkeypatch) -> None:
    # "20 km south of Crete" can land the computed point back on the island.
    seen: list[tuple[float, float]] = []

    def fake_is_on_land(lat: float, lon: float) -> bool:
        seen.append((lat, lon))
        return len(seen) == 1  # origin is "land", the first ring hit is "sea"

    monkeypatch.setattr("core.intel.landmask.is_on_land", fake_is_on_land)
    result = extract_relative_coords("rubber boat 20 km south of Crete")
    assert result is not None
    assert len(seen) >= 2  # a snap search ran


def test_is_distress_excludes_the_ngo_org_name_not_a_real_call() -> None:
    # Real false-positive case (docs/ALERT_RECOGNITION_BASELINE.md): the
    # bare "sos" keyword matched the org's own name in an RSS-style post.
    assert is_distress("SOS Mediterranee published its annual report on Mediterranean crossings") is False
    # A genuine standalone SOS is unaffected.
    assert is_distress("🆘 30 people in a rubber boat, urgent rescue needed") is True


def test_is_distress_no_longer_triggers_on_bare_rescue_operation() -> None:
    # docs/ALERT_RECOGNITION_BASELINE.md: too weak/ambiguous alone --
    # matched abstract policy language, not a live incident.
    assert is_distress("funding package for search and rescue operations in the central Mediterranean") is False


def test_distress_functions_exclude_retrospective_commemoration() -> None:
    # docs/ALERT_RECOGNITION_BASELINE.md: an anniversary/vigil/memorial is
    # not a new incident report -- both is_distress() and the stricter
    # is_direct_distress_call() independently false-positived on this shape
    # (the "shipwreck" keyword ignoring tense/context).
    text = "Last year's shipwreck anniversary was marked with a vigil in Lampedusa"
    assert is_distress(text) is False
    assert is_direct_distress_call(text) is False
    # A live shipwreck report is unaffected.
    assert is_direct_distress_call("Shipwreck in the WesternMed. We were alerted by relatives.") is True


def test_alarm_phone_coastal_place_aliases_are_geolocatable() -> None:
    for text in (
        "30 people left Bordj El Bahri, #Algeria",
        "25 people left Tipaza, #Algeria",
        "26 people left AinTaya #Algérie",
        "15 people left #Cherchell, #Algeria",
        "30 people close to #Kalymnos, #Greece",
    ):
        assert extract_coords(text) is not None, text
