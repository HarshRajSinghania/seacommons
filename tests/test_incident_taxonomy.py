from core.domain.incident_taxonomy import taxonomy_fields


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
    cases = (
        ("gap", "ais_gap"),
        ("long_gap", "ais_gap"),
        ("position_jump", "position_anomaly"),
        ("impossible_speed", "position_anomaly"),
        ("rendezvous", "rendezvous"),
        ("infra_proximity", "infrastructure_proximity"),
    )
    for anomaly_type, expected in cases:
        out = taxonomy_fields(
            event_type="ais_anomaly",
            maritime_domain="grey_zone",
            metadata={"anomaly_type": anomaly_type},
        )
        assert out["main_category"] == "maritime"
        assert out["incident_type"] == expected
        assert out["observation_type"] == expected
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
    out = taxonomy_fields(
        event_type="distress",
        maritime_domain="safety",
        metadata={"ais_nav_status_kind": "distress_beacon"},
    )
    assert out["main_category"] == "maritime"
    assert out["incident_type"] == "distress_beacon"
    assert out["observation_type"] == "distress_beacon"
