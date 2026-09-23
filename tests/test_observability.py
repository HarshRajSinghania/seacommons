from __future__ import annotations

import logging
import uuid

from core.api.main import _run_intel_sync_tick
from core.intel.source_registry import source_registry
from core.observability import (
    record_drift_maintenance_report,
    record_hypothesis_transition,
    refresh_source_health_gauges,
)
from prometheus_client import generate_latest


def test_split_runtime_sync_metrics_record_success_and_failure(caplog) -> None:
    def failed_sync() -> tuple[int, int]:
        raise RuntimeError("database unavailable with private detail")

    with caplog.at_level(logging.WARNING, logger="seacommons.api"):
        failures = _run_intel_sync_tick(failed_sync, consecutive_failures=0)
    assert failures == 1
    assert "error_type=RuntimeError" in caplog.text
    assert "private detail" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="seacommons.api"):
        failures = _run_intel_sync_tick(failed_sync, consecutive_failures=2)
    assert failures == 3
    assert any(record.levelno == logging.ERROR for record in caplog.records)

    with caplog.at_level(logging.INFO, logger="seacommons.api"):
        failures = _run_intel_sync_tick(lambda: (2, 3), consecutive_failures=failures)
    assert failures == 0
    assert "recovered after 3 consecutive failure(s)" in caplog.text

    metrics = generate_latest().decode()
    assert 'seacommons_intel_sync_runs_total{outcome="failure"}' in metrics
    assert 'seacommons_intel_sync_runs_total{outcome="success"}' in metrics
    assert "seacommons_intel_sync_consecutive_failures 0.0" in metrics
    assert "seacommons_intel_sync_last_success_unixtime" in metrics


def test_intel_source_health_metrics_are_aggregated_without_source_labels() -> None:
    name = f"pytest-source-{uuid.uuid4()}"
    source_registry.register(name, "test")
    source_registry.record_poll(name, events_found=2)

    refresh_source_health_gauges()
    metrics = generate_latest().decode()

    assert 'seacommons_intel_sources{status="active"}' in metrics
    assert "seacommons_intel_source_events_last_hour" in metrics
    assert name not in metrics


# ── docs/fixes.md M11 ────────────────────────────────────────────────────

def test_record_hypothesis_transition_increments_the_labelled_counter() -> None:
    record_hypothesis_transition("dark_transit", "review_ready")
    metrics = generate_latest().decode()
    assert (
        'seacommons_hypothesis_events_total{hypothesis_type="dark_transit",state="review_ready"}'
        in metrics
    )


def test_record_hypothesis_transition_never_raises_on_a_broken_backend(monkeypatch) -> None:
    import core.observability as observability_module

    def _boom(*args, **kwargs):
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(observability_module.HYPOTHESIS_EVENTS, "labels", _boom)
    record_hypothesis_transition("dark_transit", "assessed")  # must not raise


def test_record_drift_maintenance_report_sets_stuck_and_invalid_gauges() -> None:
    record_drift_maintenance_report({"scanned": 5, "stuck": 3, "invalid": 2, "fixed": 0})
    metrics = generate_latest().decode()
    assert 'seacommons_drift_status{status="stuck"} 3.0' in metrics
    assert 'seacommons_drift_status{status="invalid"} 2.0' in metrics


def test_record_drift_maintenance_report_never_raises_on_missing_keys() -> None:
    record_drift_maintenance_report({})  # must not raise, defaults to 0


def test_health_data_route_exposes_the_real_data_health_summary() -> None:
    """docs/fixes.md M14.5: the real internal /health/data endpoint --
    exercised end to end via the FastAPI route, not just the underlying
    function."""
    from core.api.main import app
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/health/data")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) >= {
        "healthy", "source_outage_detected", "stuck_drift_detected",
        "edge_failure_detected", "degraded_sources",
    }


def test_maritime_episode_metric_uses_only_bounded_semantic_labels() -> None:
    from core import observability

    observability.record_maritime_episode_evaluation(
        "gap_episode", "single_source_multi_indicator"
    )
    metrics = generate_latest().decode()
    assert (
        'seacommons_maritime_episode_evaluations_total{episode_family="gap_episode",verification_status="single_source_multi_indicator"}'
        in metrics
    )


def test_v1_hypothesis_decision_metric_distinguishes_eligible_from_ineligible() -> None:
    from core import observability

    observability.record_v1_hypothesis_decision("dark_transit", "ineligible")
    observability.record_v1_hypothesis_decision("position_spoofing", "eligible")
    metrics = generate_latest().decode()
    assert 'seacommons_v1_hypothesis_decisions_total{hypothesis_type="dark_transit",outcome="ineligible"}' in metrics
    assert 'seacommons_v1_hypothesis_decisions_total{hypothesis_type="position_spoofing",outcome="eligible"}' in metrics


def test_ais_fusion_metrics_use_only_bounded_provider_labels() -> None:
    from core import observability

    observability.record_ais_fusion_observation(
        provider="aiscast", upstream="volunteer", mode="shadow", outcome="received"
    )
    metrics = generate_latest().decode()
    assert (
        'seacommons_ais_fusion_observations_total{mode="shadow",outcome="received",provider="aiscast",upstream="volunteer"}'
        in metrics
    )
    assert "station_id" not in metrics


def test_ais_fusion_metric_normalizes_unbounded_upstream_to_other() -> None:
    from core import observability

    observability.record_ais_fusion_observation(
        provider="aiscast", upstream="random-station-feed-123", mode="shadow", outcome="received"
    )
    metrics = generate_latest().decode()
    assert 'upstream="other"' in metrics


def test_humanitarian_verification_metrics_normalize_sensitive_unbounded_labels() -> None:
    from core import observability

    secret = "258479000-private@example.com-message-123"
    observability.record_humanitarian_verification_event(
        stage=secret, source_role=secret, outcome=secret,
    )
    metrics = generate_latest().decode()
    assert secret not in metrics
    assert 'seacommons_humanitarian_verification_events_total{outcome="other",source_role="unknown",stage="other"}' in metrics


def test_humanitarian_verification_metrics_accept_bounded_resolution_outcome() -> None:
    from core import observability

    observability.record_humanitarian_verification_event(
        stage="resolution", source_role="verification", outcome="rescue_confirmed",
    )
    metrics = generate_latest().decode()
    assert 'stage="resolution"' in metrics
    assert 'source_role="verification"' in metrics
    assert 'outcome="rescue_confirmed"' in metrics


def test_remote_radio_metrics_normalize_unbounded_labels() -> None:
    from core import observability

    secret = "receiver-258479000-private@example.com-session-123"
    observability.record_remote_radio_event(provider=secret, state=secret, outcome=secret)
    metrics = generate_latest().decode()
    assert secret not in metrics
    assert 'seacommons_remote_radio_events_total{outcome="other",provider="other",state="disconnected"}' in metrics


def test_remote_radio_metrics_accept_bounded_provider_state_outcome() -> None:
    from core import observability

    observability.record_remote_radio_event(
        provider="openwebrx", state="connected", outcome="observation"
    )
    metrics = generate_latest().decode()
    assert 'provider="openwebrx"' in metrics
    assert 'state="connected"' in metrics
    assert 'outcome="observation"' in metrics


def test_cross_modal_metrics_normalize_hostile_labels() -> None:
    from prometheus_client import generate_latest

    from core import observability

    secret = "receiver-258479000-private@example.com-packet-123"
    observability.record_cross_modal_event(stage=secret, state=secret, outcome=secret)
    metrics = generate_latest().decode()
    assert secret not in metrics
    assert 'seacommons_cross_modal_events_total{outcome="other",stage="other",state="other"}' in metrics


def test_cross_modal_metrics_accept_bounded_independence_event() -> None:
    from prometheus_client import generate_latest

    from core import observability

    observability.record_cross_modal_event(
        stage="independence", state="multi_lineage", outcome="evaluated"
    )
    metrics = generate_latest().decode()
    assert 'stage="independence"' in metrics
    assert 'state="multi_lineage"' in metrics
    assert 'outcome="evaluated"' in metrics


def test_remote_radio_metrics_accept_failover_lifecycle_outcomes() -> None:
    from core import observability

    for outcome in (
        "reconnected", "reconnect_failed", "failover",
        "standby_promoted", "cooldown_retry",
    ):
        observability.record_remote_radio_event(
            provider="kiwisdr", state="connected", outcome=outcome
        )
    metrics = generate_latest().decode()

    for outcome in (
        "reconnected", "reconnect_failed", "failover",
        "standby_promoted", "cooldown_retry",
    ):
        assert f'outcome="{outcome}"' in metrics


def test_outbound_http_metrics_use_only_bounded_labels() -> None:
    from prometheus_client import generate_latest

    from core import observability

    secret = "host.example?token=private-value"
    observability.record_outbound_request(
        policy=secret,
        method="TRACE",
        outcome=secret,
    )
    metrics = generate_latest().decode()
    assert secret not in metrics
    assert (
        'seacommons_outbound_http_requests_total{method="other",outcome="other",policy="other"}'
        in metrics
    )


def test_outbound_http_metrics_accept_canonical_labels() -> None:
    from prometheus_client import generate_latest

    from core import observability

    observability.record_outbound_request(
        policy="public_untrusted",
        method="GET",
        outcome="blocked_target",
    )
    metrics = generate_latest().decode()
    assert (
        'seacommons_outbound_http_requests_total{method="GET",outcome="blocked_target",policy="public_untrusted"}'
        in metrics
    )


def test_humanitarian_pipeline_failure_metric_has_bounded_stage_labels() -> None:
    from core import observability

    secret = "event-raw-private-content-123"
    observability.record_humanitarian_pipeline_failure(secret)
    metrics = generate_latest().decode()
    assert secret not in metrics
    assert 'seacommons_humanitarian_pipeline_failures_total{stage="other"}' in metrics
