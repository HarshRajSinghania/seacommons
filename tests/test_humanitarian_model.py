# SPDX-License-Identifier: AGPL-3.0-or-later
from core.intel.humanitarian import humanitarian_case_metadata


def test_alarm_phone_case_preserves_approximate_people_count() -> None:
    case = humanitarian_case_metadata(
        "🆘 from ~30 people in distress in the Aegean",
        incident_id="2093662092645548206",
        source="alarm_phone",
        distress=True,
    )
    assert case["humanitarian_case_id"] == "HUM-X-2093662092645548206"
    assert case["humanitarian_case_type"] == "distress"
    assert case["humanitarian_status"] == "ongoing"
    assert case["people_reported"] == 30
    assert case["people_precision"] == "approximate"
    assert case["verification_level"] == "direct_humanitarian_source"


def test_humanitarian_resolution_is_an_outcome_not_a_new_distress() -> None:
    case = humanitarian_case_metadata(
        "Rescued to Lampedusa. All 47 people arrived safely.",
        incident_id="reply-1",
        source="alarm_phone",
        distress=False,
        resolved=True,
    )
    assert case["humanitarian_case_type"] == "resolution"
    assert case["humanitarian_status"] == "resolved"
    assert case["people_reported"] == 47
    assert case["people_precision"] == "exact"


def test_alarm_phone_hope_for_safety_is_not_a_resolution() -> None:
    case = humanitarian_case_metadata(
        "🆘 9 people in distress in the #WesternMed. We were alerted by worried relatives. "
        "Since then, there are no news from them. We hope they will be found and brought to safety!",
        incident_id="westernmed-9",
        source="alarm_phone",
        distress=True,
    )
    assert case["humanitarian_case_type"] == "distress"
    assert case["humanitarian_status"] == "ongoing"
