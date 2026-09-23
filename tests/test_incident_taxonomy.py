from core.domain.incident_taxonomy import (
    STABLE_HUMANITARIAN_INCIDENT_TYPES,
    STABLE_MARITIME_INCIDENT_TYPES,
    incident_type,
    taxonomy_fields,
)


def test_humanitarian_rescue_is_category_not_source():
    out = taxonomy_fields(
        event_type="ngo_activity",
        maritime_domain="sar",
        humanitarian_case_type="rescue_update",
        metadata={"operator_type": "civil_ngo"},
    )
    assert out["main_category"] == "humanitarian"
    assert out["incident_type"] == "rescue"


def test_maritime_transfer_keeps_sanctions_as_facet():
    out = taxonomy_fields(
        event_type="correlated_alert",
        maritime_domain="sanctions",
        metadata={
            "alert_type": "sts_transfer",
            "sanctions_matched": True,
            "verification_status": "multi_source_corroborated",
        },
    )
    assert out["main_category"] == "maritime"
    assert out["incident_type"] == "transfer"
    assert out["sanctions_matched"] is True
    assert out["corroborated"] is True


def test_raw_observations_do_not_claim_hypothesis_semantics():
    # observation_type is the raw, detector-specific evidence label.
    # incident_type is the stable, closed, UI-facing topic bucket the raw
    # label is filed under -- it is never a hypothesis-strength claim (no
    # "spoofing"/"dark activity" wording is asserted just because the topic
    # bucket happens to share that name), and it is always a member of the
    # closed set the public selector taxonomy is built from.
    cases = (
        ("gap", "ais_gap", "ais_gap"),
        ("long_gap", "ais_gap", "ais_gap"),
        ("position_jump", "position_anomaly", "position_integrity"),
        ("impossible_speed", "position_anomaly", "position_integrity"),
        ("rendezvous", "rendezvous", "transfer"),
        ("infra_proximity", "infrastructure_proximity", "infrastructure_proximity"),
    )
    for anomaly_type, expected_observation, expected_incident in cases:
        out = taxonomy_fields(
            event_type="ais_anomaly",
            maritime_domain="grey_zone",
            metadata={"anomaly_type": anomaly_type},
        )
        assert out["main_category"] == "maritime"
        assert out["observation_type"] == expected_observation
        assert out["incident_type"] == expected_incident
        assert out["incident_type"] in STABLE_MARITIME_INCIDENT_TYPES
        assert out["hypothesis_type"] is None


def test_hypothesis_maps_into_maritime_incident_type():
    out = taxonomy_fields(
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        hypothesis_type="dark_transit",
        metadata={"evidence_stage": "derived"},
    )
    assert out["main_category"] == "maritime"
    assert out["incident_type"] == "dark_activity"
    assert out["observation_type"] == "maritime_context"
    assert out["hypothesis_type"] == "dark_transit"


def test_review_state_and_many_same_lineage_detectors_are_not_corroboration():
    out = taxonomy_fields(
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        metadata={
            "anomaly_type": "gap",
            "evidence_stage": "assessed",
            "verification_status": "reviewed",
            "evidence_count": 4,
            "independent_source_count": 1,
            "contributing_independence_groups": ["ais_sensor_lineage"],
        },
    )
    assert out["corroborated"] is False
    assert out["evidence_state"] == "assessed"
    assert out["verification_status"] == "reviewed"


def test_two_validated_independent_lineages_are_corroboration():
    out = taxonomy_fields(
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        metadata={
            "anomaly_type": "gap",
            "contributing_independence_groups": [
                "ais_sensor_lineage",
                "official_report",
            ],
        },
    )
    assert out["corroborated"] is True


def test_dedicated_ais_distress_beacon_has_neutral_maritime_type():
    # Regression for a real production defect: a dedicated AIS-SART/MOB/
    # EPIRB self-report is evidence-level observation_type=distress_beacon,
    # but it must be filed under the stable, closed navigation_safety
    # bucket -- the same bucket as aground/NUC/restricted manoeuvrability --
    # not leaked as a raw, unbucketed incident_type. A bare "distress_beacon"
    # incident_type is not a member of the public selector taxonomy
    # (apps/web/src/main.jsx SIGNALS_MACRO_GROUPS) and silently disappears
    # from the map even though the backend still counts it as a Live item.
    out = taxonomy_fields(
        event_type="distress",
        maritime_domain="safety",
        metadata={"ais_nav_status_kind": "distress_beacon"},
    )
    assert out["main_category"] == "maritime"
    assert out["incident_type"] == "navigation_safety"
    assert out["incident_type"] in STABLE_MARITIME_INCIDENT_TYPES
    assert out["observation_type"] == "distress_beacon"


def test_two_distinct_beacon_observations_are_not_asserted_as_one_incident():
    # Regression fixture for the reported production case: two AIS-MOB
    # observations near Mallorca. They must remain two distinct evidence
    # observations -- same stable incident_type/main_category (so both
    # render), but nothing here merges or asserts they are one casualty.
    mallorca_a = taxonomy_fields(
        event_type="distress",
        maritime_domain="safety",
        metadata={"ais_nav_status_kind": "distress_beacon", "linked_mmsi": "972111222"},
    )
    mallorca_b = taxonomy_fields(
        event_type="distress",
        maritime_domain="safety",
        metadata={"ais_nav_status_kind": "distress_beacon", "linked_mmsi": "972333444"},
    )
    for out in (mallorca_a, mallorca_b):
        assert out["main_category"] == "maritime"
        assert out["incident_type"] == "navigation_safety"
        assert out["corroborated"] is False
    # taxonomy_fields is a pure per-observation projection: it carries no
    # cross-observation identity/clustering decision to assert or deny.


def test_incident_type_is_always_a_member_of_the_closed_stable_set():
    # Property test: whatever an existing or future detector calls its own
    # finding (anomaly_type / alert_type / ais_nav_status_kind /
    # hypothesis_type), incident_type() must resolve into the closed,
    # stable, UI-facing bucket set -- never a raw passthrough. This is what
    # makes "a new detector label breaks the public selector taxonomy"
    # a permanent regression class, not a one-off fixture.
    maritime_anomaly_types = (
        "gap", "long_gap", "ais_gap", "signal_gap", "transponder_off",
        "position_jump", "impossible_speed", "teleport", "circle_spoof",
        "static_spoof", "circular_pattern", "static_position_inconsistency",
        "rendezvous", "ais_rendezvous", "sts",
        "infra_proximity", "infrastructure_proximity",
        "loiter", "abnormal_dwell", "stationary_anomaly",
        "sanctioned_port_call",
        "not_under_command", "aground", "restricted_manoeuvrability",
        "flag_hopping", "mmsi_mismatch", "piracy", "oil_spill",
        "",  # unknown/unset anomaly must fail closed to maritime_context
    )
    for anomaly_type in maritime_anomaly_types:
        result = incident_type(
            event_type="ais_anomaly",
            maritime_domain="grey_zone",
            metadata={"anomaly_type": anomaly_type},
        )
        assert result in STABLE_MARITIME_INCIDENT_TYPES, (anomaly_type, result)

    for nav_kind in ("distress_beacon", "aground", "not_under_command", "restricted_manoeuvrability"):
        result = incident_type(
            event_type="distress",
            maritime_domain="safety",
            metadata={"ais_nav_status_kind": nav_kind},
        )
        assert result in STABLE_MARITIME_INCIDENT_TYPES, (nav_kind, result)

    for hypothesis_type in (
        "dark_transit", "position_spoofing", "covert_rendezvous", "infrastructure_pattern",
    ):
        result = incident_type(
            event_type="ais_anomaly",
            maritime_domain="grey_zone",
            hypothesis_type=hypothesis_type,
            metadata={},
        )
        assert result in STABLE_MARITIME_INCIDENT_TYPES, (hypothesis_type, result)

    for hct in (
        "rescue_update", "rescue_completed", "rescue", "distress", "missing",
        "shipwreck", "pushback", "land_humanitarian", "resolution", "",
    ):
        result = incident_type(
            event_type="distress",
            maritime_domain="sar",
            humanitarian_case_type=hct,
            metadata={},
        )
        assert result in STABLE_HUMANITARIAN_INCIDENT_TYPES, (hct, result)
