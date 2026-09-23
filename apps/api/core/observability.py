# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations
import json, logging, time, uuid
from collections import Counter as CollectionCounter
from datetime import datetime, timezone
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("seacommons_http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_LATENCY = Histogram("seacommons_http_request_duration_seconds", "HTTP request latency", ["method", "path"])
JOBS = Gauge("seacommons_jobs", "Durable jobs by state", ["status"])
WORKERS = Gauge("seacommons_workers_alive", "Workers with a fresh heartbeat")
JOB_RUNS = Counter("seacommons_job_runs_total", "Worker job executions", ["type", "outcome"])
INTEL_SYNC_RUNS = Counter(
    "seacommons_intel_sync_runs_total",
    "Split-runtime intel database sync attempts",
    ["outcome"],
)
INTEL_SYNC_EVENTS = Counter(
    "seacommons_intel_sync_events_total",
    "Intel events observed by split-runtime database sync",
    ["change"],
)
INTEL_SYNC_CONSECUTIVE_FAILURES = Gauge(
    "seacommons_intel_sync_consecutive_failures",
    "Consecutive split-runtime intel database sync failures",
)
INTEL_SYNC_LAST_SUCCESS = Gauge(
    "seacommons_intel_sync_last_success_unixtime",
    "Unix timestamp of the last successful split-runtime intel database sync",
)
INTEL_SOURCES = Gauge(
    "seacommons_intel_sources",
    "Registered intel sources by current health state",
    ["status"],
)
INTEL_SOURCE_EVENTS = Gauge(
    "seacommons_intel_source_events_last_hour",
    "Events received from all registered intel sources in the last hour",
)
# ── docs/fixes.md M11 -- pipeline/data-quality metrics ─────────────────────
# Wired live (docs/fixes.md M14.5): record_hypothesis_transition (from
# core.intel.hypothesis_engine.evaluate_episode(), M14.3),
# record_drift_maintenance_report (from core.observability_health.
# gather_data_health_summary(), the GET /health/data caller), and
# record_classification_fail_closed (from core.intel.service_taxonomy.
# classify_service()'s own fail-closed return). The remaining counters
# below (source parse failures, dedup outcomes, observation-to-incident/
# episode latency, AIS coverage, gap-candidate labels, case relinking,
# edge projection mismatch) are definitions only -- wiring each into its
# own ingest/dedup/classification call site is mechanical, per-call-site
# work spanning the whole ingestion pipeline, tracked as follow-up.
SOURCE_PARSE_FAILURES = Counter(
    "seacommons_source_parse_failures_total", "Source ingestion parse failures", ["source"],
)
DEDUP_EVENTS = Counter(
    "seacommons_dedup_events_total", "Ingested events by dedup outcome", ["outcome"],
)
OBSERVATION_TO_INCIDENT_LATENCY = Histogram(
    "seacommons_observation_to_incident_latency_seconds",
    "Time from a SourceObservation (M1.1) to the incident it produced",
)
OBSERVATION_TO_EPISODE_LATENCY = Histogram(
    "seacommons_observation_to_episode_latency_seconds",
    "Time from a SourceObservation (M1.1) to the episode (M5.2) it joined",
)
CLASSIFICATION_FAIL_CLOSED = Counter(
    "seacommons_classification_fail_closed_total",
    "Classifications that fell back to an unclassified/review state",
    ["classifier"],
)
LOCATION_STATUS_EVENTS = Gauge(
    "seacommons_location_status_events",
    "Events by location_status (M3) -- region_only vs positioned rate",
    ["status"],
)
DRIFT_STATUS = Gauge(
    "seacommons_drift_status", "Drift jobs by lifecycle status", ["status"],
)
AIS_COVERAGE = Gauge(
    "seacommons_ais_coverage_ratio",
    "AIS local_reporting_ratio (M4.2) by AOI/time bucket",
    ["aoi", "time_bucket"],
)
GAP_CANDIDATE_EVENTS = Counter(
    "seacommons_gap_candidate_events_total",
    "Gap-detector classification outcomes (M4.1/M4.3)", ["label"],
)
HYPOTHESIS_EVENTS = Counter(
    "seacommons_hypothesis_events_total",
    "InvestigationHypothesis (M6) state transitions", ["hypothesis_type", "state"],
)
MARITIME_EPISODE_EVALUATIONS = Counter(
    "seacommons_maritime_episode_evaluations_total",
    "Persisted MaritimeEpisode evaluations by bounded semantic class",
    ["episode_family", "verification_status"],
)
V1_HYPOTHESIS_DECISIONS = Counter(
    "seacommons_v1_hypothesis_decisions_total",
    "Observation->Episode->Hypothesis v1 eligibility decisions",
    ["hypothesis_type", "outcome"],
)
MDA_SCAN_STAGE_EVENTS = Counter(
    "seacommons_mda_scan_stage_events_total",
    "MDA scan stage completions/failures using bounded stage labels",
    ["stage", "outcome"],
)
CASE_RELINK_EVENTS = Counter(
    "seacommons_case_relink_events_total", "Incident linking outcomes", ["outcome"],
)
EDGE_PROJECTION_MISMATCH = Counter(
    "seacommons_edge_projection_mismatch_total",
    "VM-edge projection parity check failures (M9)",
)
AIS_FUSION_OBSERVATIONS = Counter(
    "seacommons_ais_fusion_observations_total",
    "Normalized AIS observations by bounded provider/upstream/runtime outcome",
    ["provider", "upstream", "mode", "outcome"],
)
HUMANITARIAN_VERIFICATION_EVENTS = Counter(
    "seacommons_humanitarian_verification_events_total",
    "Humanitarian verification pipeline events by bounded semantic stage",
    ["stage", "source_role", "outcome"],
)
HUMANITARIAN_PIPELINE_FAILURES = Counter(
    "seacommons_humanitarian_pipeline_failures_total",
    "Humanitarian incident pipeline failures by bounded stage",
    ["stage"],
)
REMOTE_RADIO_EVENTS = Counter(
    "seacommons_remote_radio_events_total",
    "Remote radio runtime events by bounded provider/state/outcome",
    ["provider", "state", "outcome"],
)
STRUCTURED_RADIO_EVENTS = Counter(
    "seacommons_structured_radio_events_total",
    "Structured DSC/NAVTEX ingestion events by bounded kind/outcome",
    ["kind", "outcome"],
)
CROSS_MODAL_EVENTS = Counter(
    "seacommons_cross_modal_events_total",
    "Cross-modal evidence-fusion events by bounded stage/state/outcome",
    ["stage", "state", "outcome"],
)

_HV_STAGES = frozenset({"claim_extraction", "association", "mission", "resolution"})
_HV_SOURCE_ROLES = frozenset({"operational_origin", "verification", "archive_reference", "unknown"})
_HV_OUTCOMES = frozenset({
    "observed", "none", "associated", "uncertain",
    "unrelated", "possible_response", "approaching", "on_scene",
    "probable_rescue_activity", "departing_scene", "post_rescue_transit",
    "insufficient_evidence", "no_resolution_evidence", "response_detected",
    "rescue_activity_probable", "rescue_confirmed", "disembarkation_confirmed",
    "fatal_outcome_reported", "contradictory_evidence", "other",
})


_HUMANITARIAN_PIPELINE_STAGES = frozenset({
    "source_policy", "classification", "incident_sync", "recognition",
    "claims", "correlation", "verification", "other",
})


def record_humanitarian_pipeline_failure(stage: str) -> None:
    """Count fail-open subscriber failures without high-cardinality labels."""
    try:
        label = stage if stage in _HUMANITARIAN_PIPELINE_STAGES else "other"
        HUMANITARIAN_PIPELINE_FAILURES.labels(label).inc()
    except Exception:  # pragma: no cover - metrics never block ingestion
        pass


def record_humanitarian_verification_event(*, stage: str, source_role: str, outcome: str) -> None:
    """Bounded-cardinality verification metric; never labels raw identifiers or text."""
    try:
        stage_label = stage if stage in _HV_STAGES else "other"
        role_label = source_role if source_role in _HV_SOURCE_ROLES else "unknown"
        outcome_label = outcome if outcome in _HV_OUTCOMES else "other"
        HUMANITARIAN_VERIFICATION_EVENTS.labels(stage_label, role_label, outcome_label).inc()
    except Exception:  # pragma: no cover - metrics never block verification
        pass


_REMOTE_RADIO_PROVIDERS = frozenset({"kiwisdr", "openwebrx"})
_REMOTE_RADIO_STATES = frozenset({"connected", "disconnected"})
_REMOTE_RADIO_OUTCOMES = frozenset({
    "started", "start_failed", "observation", "persist_failed",
    "reconnected", "reconnect_failed", "failover", "standby_promoted", "cooldown_retry",
})


def record_remote_radio_event(*, provider: str, state: str, outcome: str) -> None:
    """Bounded-cardinality remote-radio metric; never labels receiver/frequency/session IDs."""
    try:
        provider_label = provider if provider in _REMOTE_RADIO_PROVIDERS else "other"
        state_label = state if state in _REMOTE_RADIO_STATES else "disconnected"
        outcome_label = outcome if outcome in _REMOTE_RADIO_OUTCOMES else "other"
        REMOTE_RADIO_EVENTS.labels(provider_label, state_label, outcome_label).inc()
    except Exception:
        pass

_STRUCTURED_RADIO_KINDS = frozenset({"dsc", "navtex"})
_STRUCTURED_RADIO_OUTCOMES = frozenset({"accepted", "disabled", "invalid", "persist_failed", "projected", "context_only"})


def record_structured_radio_event(*, kind: str, outcome: str) -> None:
    """Bounded DSC/NAVTEX metric; never labels source-native identifiers or text."""
    try:
        kind_label = kind if kind in _STRUCTURED_RADIO_KINDS else "other"
        outcome_label = outcome if outcome in _STRUCTURED_RADIO_OUTCOMES else "other"
        STRUCTURED_RADIO_EVENTS.labels(kind_label, outcome_label).inc()
    except Exception:
        pass


_CROSS_MODAL_STAGES = frozenset({"packet", "independence", "humanitarian_context", "maritime_context"})
_CROSS_MODAL_STATES = frozenset({"single_lineage", "multi_lineage", "contradictory", "na"})
_CROSS_MODAL_OUTCOMES = frozenset({"created", "evaluated", "attached", "rejected", "other"})


def record_cross_modal_event(*, stage: str, state: str, outcome: str) -> None:
    """Bounded cross-modal metric; never labels evidence/source/subject identifiers."""
    try:
        stage_label = stage if stage in _CROSS_MODAL_STAGES else "other"
        state_label = state if state in _CROSS_MODAL_STATES else "other"
        outcome_label = outcome if outcome in _CROSS_MODAL_OUTCOMES else "other"
        CROSS_MODAL_EVENTS.labels(stage_label, state_label, outcome_label).inc()
    except Exception:
        pass


_AIS_PROVIDERS = frozenset({"aisstream", "aiscast"})
_AIS_UPSTREAMS = frozenset({
    "aisstream", "volunteer", "digitraffic", "kystverket",
    "barentswatch", "aishub", "unknown",
})
_AIS_MODES = frozenset({"legacy", "shadow", "fused"})
_AIS_OUTCOMES = frozenset({"received", "duplicate", "shadow", "canonical"})


def record_ais_fusion_observation(*, provider: str, upstream: str, mode: str, outcome: str) -> None:
    """Record AIS fusion flow without station/vessel/high-cardinality labels."""
    try:
        provider_label = provider if provider in _AIS_PROVIDERS else "other"
        upstream_label = upstream if upstream in _AIS_UPSTREAMS else "other"
        mode_label = mode if mode in _AIS_MODES else "legacy"
        outcome_label = outcome if outcome in _AIS_OUTCOMES else "received"
        AIS_FUSION_OBSERVATIONS.labels(
            provider_label, upstream_label, mode_label, outcome_label
        ).inc()
    except Exception:  # pragma: no cover - metrics never block AIS ingestion
        pass


def record_maritime_episode_evaluation(episode_family: str, verification_status: str) -> None:
    """Count a persisted episode evaluation using bounded semantic labels only."""
    try:
        MARITIME_EPISODE_EVALUATIONS.labels(episode_family, verification_status).inc()
    except Exception:  # pragma: no cover
        pass


def record_v1_hypothesis_decision(hypothesis_type: str, outcome: str) -> None:
    """Count v1 eligibility decisions without vessel or evidence identifiers."""
    try:
        V1_HYPOTHESIS_DECISIONS.labels(hypothesis_type, outcome).inc()
    except Exception:  # pragma: no cover
        pass


_MDA_SCAN_STAGES = frozenset({
    "rendezvous", "infra_loiter", "gap", "identity", "sanctioned_port_call",
    "mmsi_duplicate", "spoofing", "hypotheses",
})


def record_mda_scan_stage(*, stage: str, outcome: str) -> None:
    """Record stage health without letting metrics block the MDA pipeline."""
    try:
        stage_label = stage if stage in _MDA_SCAN_STAGES else "other"
        outcome_label = outcome if outcome in {"success", "error"} else "error"
        MDA_SCAN_STAGE_EVENTS.labels(stage_label, outcome_label).inc()
    except Exception:  # pragma: no cover
        pass


def record_hypothesis_transition(hypothesis_type: str, new_state: str) -> None:
    """Call from core.intel.hypothesis.transition() call sites once wired
    -- never raises, so a broken metrics backend can never block a
    hypothesis transition."""
    try:
        HYPOTHESIS_EVENTS.labels(hypothesis_type, new_state).inc()
    except Exception:  # pragma: no cover
        pass


def record_drift_maintenance_report(report: dict[str, int]) -> None:
    """Call after core.intel.backfill_drift_maintenance.run() (M8) --
    never raises."""
    try:
        DRIFT_STATUS.labels("stuck").set(report.get("stuck", 0))
        DRIFT_STATUS.labels("invalid").set(report.get("invalid", 0))
    except Exception:  # pragma: no cover
        pass


def record_classification_fail_closed(classifier: str) -> None:
    """Call from a classifier's own fail-closed branch (docs/fixes.md
    M11/M14.5) -- e.g. core.intel.service_taxonomy.classify_service()'s
    "unclassified" return. Never raises."""
    try:
        CLASSIFICATION_FAIL_CLOSED.labels(classifier).inc()
    except Exception:  # pragma: no cover
        pass


def current_gauge_value(gauge: Gauge, *, default: float = 0.0) -> float:
    """Read a Gauge's currently-set value without relying on the
    prometheus_client private ``_value`` attribute -- ``collect()`` is the
    stable public API for reading back what a metric currently holds."""
    try:
        for metric in gauge.collect():
            for sample in metric.samples:
                return sample.value
    except Exception:  # pragma: no cover
        pass
    return default


LIVE_PUBLISH_CYCLES = Counter(
    "seacommons_live_publish_cycles_total",
    "Live edge publisher cycles by outcome",
    ["outcome"],
)
LIVE_PUBLISH_EVENTS = Counter(
    "seacommons_live_publish_events_total",
    "Live edge payloads by pipeline stage",
    ["stage"],  # collected | delivered | delivery_failed
)
LIVE_OUTBOX_DEPTH = Gauge(
    "seacommons_live_outbox_depth",
    "Live edge publisher outbox rows by state",
    ["state"],  # pending | retrying
)
LIVE_PUBLISH_LAST_CYCLE = Gauge(
    "seacommons_live_publish_last_cycle_unixtime",
    "Unix timestamp of the last completed live edge publisher cycle",
)
LIVE_PUBLISH_LAST_DELIVERY = Gauge(
    "seacommons_live_publish_last_delivery_unixtime",
    "Unix timestamp of the last successful live edge delivery",
)
LIVE_EDGE_HEARTBEAT_OK = Gauge(
    "seacommons_live_edge_heartbeat_ok",
    "1 if the last live edge heartbeat POST succeeded, else 0",
)
OCR_QUEUE_DEPTH = Gauge(
    "seacommons_intel_ocr_queue_depth",
    "Media-OCR jobs pending or in flight in the bounded pool",
)
OCR_QUEUE_OLDEST = Gauge(
    "seacommons_intel_ocr_queue_oldest_job_seconds",
    "Age of the oldest queued media-OCR job",
)
OCR_QUEUE_REJECTED = Counter(
    "seacommons_intel_ocr_queue_rejected_total",
    "Media-OCR jobs the bounded pool could not accept immediately",
    ["reason"],  # deferred | dropped
)
OCR_JOB_DURATION = Histogram(
    "seacommons_intel_ocr_job_duration_seconds",
    "Wall time of one media-OCR job",
)
OCR_RESULTS = Counter(
    "seacommons_intel_ocr_results_total",
    "Media-OCR job outcomes",
    ["result"],  # consensus | disputed | text_unverified | pin_landmark | no_coordinate
)
OCR_DRIFT_REJECTED = Counter(
    "seacommons_intel_ocr_drift_rejected_total",
    "Auto-drift requests withheld because the location evidence failed the F-01 gate",
)


def record_ocr_result(result: str) -> None:
    try:
        OCR_RESULTS.labels(result).inc()
    except Exception:  # pragma: no cover - metrics must never break ingestion
        pass


def record_ocr_drift_rejected() -> None:
    try:
        OCR_DRIFT_REJECTED.inc()
    except Exception:  # pragma: no cover
        pass


def refresh_ocr_queue_gauges() -> None:
    try:
        from core.intel.media_ocr_queue import media_ocr_queue

        stats = media_ocr_queue.stats()
        OCR_QUEUE_DEPTH.set(stats["depth"] + stats.get("deferred", 0.0))
        OCR_QUEUE_OLDEST.set(stats["oldest_job_seconds"])
    except Exception:  # pragma: no cover - a scrape must not fail on this
        pass


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                   "logger": record.name, "message": record.getMessage()}
        for key in ("request_id", "job_id", "worker_id"):
            if hasattr(record, key): payload[key] = getattr(record, key)
        if record.exc_info: payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    from core.config import config
    if config.LOG_FORMAT != "json": return
    handler = logging.StreamHandler(); handler.setFormatter(JsonFormatter())
    root = logging.getLogger(); root.handlers[:] = [handler]; root.setLevel(logging.INFO)


async def metrics_middleware(request, call_next):
    started = time.perf_counter(); request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request); status = response.status_code
    except Exception:
        status = 500; raise
    finally:
        route = request.scope.get("route"); path = getattr(route, "path", request.url.path)
        HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
        HTTP_LATENCY.labels(request.method, path).observe(time.perf_counter() - started)
    response.headers["X-Request-Id"] = request_id
    return response


def refresh_operational_gauges() -> None:
    from datetime import timedelta
    from sqlalchemy import func, select
    from core.db.models import JobDB, WorkerHeartbeatDB
    from core.db.session import session_scope
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        counts = dict(db.execute(select(JobDB.status, func.count()).group_by(JobDB.status)).all())
        for state in ("queued", "running", "retry", "completed", "dead"):
            JOBS.labels(state).set(counts.get(state, 0))
        alive = db.execute(select(func.count()).select_from(WorkerHeartbeatDB).where(
            WorkerHeartbeatDB.last_seen_at >= now - timedelta(seconds=60))).scalar_one()
        WORKERS.set(alive)
    refresh_source_health_gauges()
    refresh_ocr_queue_gauges()


def refresh_source_health_gauges() -> None:
    """Export bounded-cardinality health totals from the in-process registry."""
    from core.intel.source_registry import source_registry

    sources = source_registry.get_all()
    counts = CollectionCounter(source.get("status", "pending") for source in sources)
    for status in ("pending", "active", "degraded", "offline"):
        INTEL_SOURCES.labels(status).set(counts.get(status, 0))
    INTEL_SOURCE_EVENTS.set(sum(int(source.get("events_last_hour") or 0) for source in sources))


def record_intel_sync_success(new_count: int, updated_count: int) -> None:
    """Record a successful API-side database sync in split-runtime mode."""
    INTEL_SYNC_RUNS.labels("success").inc()
    INTEL_SYNC_EVENTS.labels("new").inc(max(0, int(new_count)))
    INTEL_SYNC_EVENTS.labels("updated").inc(max(0, int(updated_count)))
    INTEL_SYNC_CONSECUTIVE_FAILURES.set(0)
    INTEL_SYNC_LAST_SUCCESS.set_to_current_time()


def record_intel_sync_failure(consecutive_failures: int) -> None:
    """Record a failed API-side database sync without exposing exception details."""
    INTEL_SYNC_RUNS.labels("failure").inc()
    INTEL_SYNC_CONSECUTIVE_FAILURES.set(max(1, int(consecutive_failures)))


def record_publisher_cycle(
    *, outcome: str, collected: int, outbox_counts: dict[str, int]
) -> None:
    """Record one live edge publisher cycle and the current outbox backlog."""
    LIVE_PUBLISH_CYCLES.labels(outcome if outcome in {"ok", "degraded"} else "degraded").inc()
    if collected:
        LIVE_PUBLISH_EVENTS.labels("collected").inc(max(0, int(collected)))
    LIVE_OUTBOX_DEPTH.labels("pending").set(int(outbox_counts.get("pending", 0)))
    LIVE_OUTBOX_DEPTH.labels("retrying").set(int(outbox_counts.get("retrying", 0)))
    LIVE_PUBLISH_LAST_CYCLE.set_to_current_time()


def record_publisher_delivery(*, delivered: bool) -> None:
    """Record a single edge delivery attempt outcome."""
    if delivered:
        LIVE_PUBLISH_EVENTS.labels("delivered").inc()
        LIVE_PUBLISH_LAST_DELIVERY.set_to_current_time()
    else:
        LIVE_PUBLISH_EVENTS.labels("delivery_failed").inc()


def record_publisher_heartbeat(*, ok: bool) -> None:
    LIVE_EDGE_HEARTBEAT_OK.set(1 if ok else 0)

REVIEW_EVENTS = Counter(
    "seacommons_review_events_total",
    "Explicit review decisions by bounded target/decision/outcome",
    ["target_type", "decision", "outcome"],
)
_REVIEW_TARGETS = frozenset({"humanitarian_resolution", "maritime_hypothesis"})
_REVIEW_DECISIONS = frozenset({"approve", "reject", "needs_more_evidence"})
_REVIEW_OUTCOMES = frozenset({"recorded", "applied", "replayed", "rejected", "needs_more_evidence", "failed"})

def record_review_event(*, target_type: str, decision: str, outcome: str) -> None:
    """Bounded-cardinality review metric; never labels target IDs, actors or evidence refs."""
    try:
        target = target_type if target_type in _REVIEW_TARGETS else "other"
        decision_label = decision if decision in _REVIEW_DECISIONS else "other"
        outcome_label = outcome if outcome in _REVIEW_OUTCOMES else "other"
        REVIEW_EVENTS.labels(target, decision_label, outcome_label).inc()
    except Exception:
        pass


OUTBOUND_REQUESTS = Counter(
    "seacommons_outbound_http_requests_total",
    "Outbound HTTP attempts by bounded policy/method/outcome",
    ["policy", "method", "outcome"],
)
_OUTBOUND_POLICIES = frozenset({"public_untrusted", "public_fixed", "operator_internal"})
_OUTBOUND_METHODS = frozenset({"GET", "HEAD", "POST"})
_OUTBOUND_OUTCOMES = frozenset({
    "success", "blocked_target", "invalid_request", "dns_failed",
    "redirect_blocked", "redirect_limit", "timeout", "tls_failed",
    "invalid_content_type", "response_too_large", "upstream_error",
})


def record_outbound_request(*, policy: str, method: str, outcome: str) -> None:
    """Record outbound HTTP flow without URL, host, IP, token, or source labels."""
    try:
        policy_label = policy if policy in _OUTBOUND_POLICIES else "other"
        method_label = method if method in _OUTBOUND_METHODS else "other"
        outcome_label = outcome if outcome in _OUTBOUND_OUTCOMES else "other"
        OUTBOUND_REQUESTS.labels(policy_label, method_label, outcome_label).inc()
    except Exception:  # pragma: no cover - metrics never block outbound policy
        pass
