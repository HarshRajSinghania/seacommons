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


def test_spoofing_is_incident_type_not_risk_level():
    out = taxonomy_fields(
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        metadata={"anomaly_type": "impossible_speed"},
    )
    assert out["main_category"] == "maritime"
    assert out["incident_type"] == "spoofing"


def test_hypothesis_maps_into_maritime_incident_type():
    out = taxonomy_fields(
        event_type="ais_anomaly",
        maritime_domain="grey_zone",
        hypothesis_type="dark_transit",
        metadata={"evidence_stage": "derived"},
    )
    assert out["main_category"] == "maritime"
    assert out["incident_type"] == "dark_activity"
