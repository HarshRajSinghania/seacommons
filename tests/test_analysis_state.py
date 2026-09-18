from core.intel.analysis_state import annotate_event_analysis
from core.intel.store import IntelEvent


def test_raw_ais_anomaly_is_archived_as_anomaly_not_evidence():
    event = IntelEvent(
        type="ais_anomaly", source="ais",
        metadata={"anomaly_type": "gap"},
    )
    annotate_event_analysis(event)
    assert event.metadata["analysis_state"] == "anomaly"
    assert event.metadata["publication_state"] == "internal"
    assert event.metadata["resolution_state"] == "open"
    assert event.metadata["lineage_ids"]


def test_multisource_fused_alert_promotes_to_evidence():
    event = IntelEvent(
        type="correlated_alert", source="fusion",
        metadata={"verification_status": "multi_source_corroborated"},
    )
    annotate_event_analysis(event)
    assert event.metadata["analysis_state"] == "evidence"



def test_coverage_gap_remains_archived_but_is_explained():
    event = IntelEvent(
        type="ais_anomaly", source="ais",
        metadata={
            "anomaly_type": "gap",
            "gap_reason": {"hypothesis": "coverage_gap"},
        },
    )
    annotate_event_analysis(event)
    assert event.metadata["analysis_state"] == "anomaly"
    assert event.metadata["resolution_state"] == "explained"


def test_sanctions_fact_is_evidence_not_evasion_claim():
    event = IntelEvent(
        type="vessel_identity", source="mda",
        metadata={"anomaly_type": "sdn_match", "sanctions_matched": True},
    )
    annotate_event_analysis(event)
    assert event.metadata["analysis_state"] == "evidence"
