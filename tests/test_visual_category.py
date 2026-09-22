# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical visual taxonomy: category determines colour, never severity."""

from core.domain.visual_category import (
    CATEGORY_COLORS,
    classify_visual_category,
    is_alarm_phone,
    visual_category_fields,
)


def test_alarm_phone_is_always_red_regardless_of_lifecycle_or_severity() -> None:
    for source in ("alarm_phone", "Alarm Phone", "AlarmPhone"):
        for lifecycle in ("active", "needs_review", "resolved", "archived"):
            for severity in ("low", "medium", "high", "critical"):
                fields = visual_category_fields(
                    source=source,
                    event_type="distress",
                    maritime_domain="sar",
                    humanitarian_case_type="distress",
                    metadata={
                        "incident_lifecycle": lifecycle,
                        "severity": severity,
                        "ocr_engine": "tesseract",
                    },
                )
                assert fields["visual_category"] == "humanitarian_alarm_phone"
                assert fields["visual_color"] == "#ff3b3b"


def test_alarm_phone_land_and_region_stay_red() -> None:
    land = visual_category_fields(
        source="alarm_phone",
        event_type="twitter",
        maritime_domain="sar",
        humanitarian_case_type="land_humanitarian",
        metadata={"location_status": "withheld_from_maritime_map"},
    )
    assert land["visual_category"] == "humanitarian_alarm_phone"
    region = visual_category_fields(
        source="Alarm Phone",
        event_type="distress",
        maritime_domain="sar",
        metadata={"location_status": "region_only", "coordinate_source": "region_area"},
    )
    assert region["visual_color"] == "#ff3b3b"


def test_tracked_account_metadata_also_counts_as_alarm_phone() -> None:
    assert is_alarm_phone("twitter", {"tracked_account": "alarm_phone"}) is True


def test_classifier_never_reads_severity() -> None:
    # A plain social post with a "critical" severity must not become a casualty.
    assert classify_visual_category(
        source="someone",
        event_type="twitter",
        metadata={"severity": "critical"},
    ) == "social"
    # An anomaly is classified by its anomaly_type, not severity.
    assert classify_visual_category(
        source="SeaCommons MDA",
        event_type="ais_anomaly",
        metadata={"anomaly_type": "circle_spoof", "severity": "low"},
    ) == "spoofing"


def test_position_jump_is_position_integrity_visual_category() -> None:
    fields = visual_category_fields(
        source="SeaCommons episode engine",
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        metadata={"anomaly_type": "position_jump"},
    )
    assert fields["visual_category"] == "spoofing"
    assert fields["visual_color"] == CATEGORY_COLORS["spoofing"]


def test_non_alarm_phone_categories_are_distinct_colours() -> None:
    keys = [
        "navigation_casualty", "spoofing", "ais_gap", "loitering", "rendezvous",
        "sanctions", "infrastructure", "identity", "piracy", "environmental",
    ]
    assert len({CATEGORY_COLORS[k] for k in keys}) == len(keys)


def test_ais_distress_beacon_is_distress_even_in_safety_domain() -> None:
    fields = visual_category_fields(
        source="ais_sart",
        event_type="distress",
        maritime_domain="safety",
        metadata={
            "is_distress": True,
            "ais_nav_status_kind": "distress_beacon",
        },
    )
    assert fields["visual_category"] == "distress"
    assert fields["visual_color"] == "#ff3b3b"
