from core.intel.store import IntelEvent
from core.live.projection import _public_intel_feature, is_useful_public_case_feature


def _event(anomaly_type: str, publication_status: str = "published") -> IntelEvent:
    return IntelEvent(
        id=f"test-{anomaly_type}",
        type="vessel_identity",
        severity="high",
        lat=37.0,
        lon=15.0,
        title="Maritime compliance fact",
        source="SeaCommons MDA",
        linked_mmsi="244123456",
        metadata={
            "anomaly_type": anomaly_type,
            "maritime_domain": "sanctions",
            "publication_status": publication_status,
            "source_policy": "official_api",
            "verification_status": "multi_source_corroborated",
            "sanctions_matched": True,
        },
    )


def test_qualified_port_call_can_cross_public_case_gate():
    feature = _public_intel_feature(
        _event("sanctioned_port_call"),
        allowed_domains=frozenset({"sanctions"}),
    )
    assert feature is not None
    assert feature["properties"]["visual_category"] == "sanctions"
    assert is_useful_public_case_feature(feature) is True



def test_static_list_match_stays_out_of_public_case_surface():
    feature = _public_intel_feature(
        _event("sdn_match"),
        allowed_domains=frozenset({"sanctions"}),
    )
    assert feature is None


def test_internal_port_call_stays_private():
    feature = _public_intel_feature(
        _event("sanctioned_port_call", publication_status="internal"),
        allowed_domains=frozenset({"sanctions"}),
    )
    assert feature is None
