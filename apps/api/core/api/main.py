# SPDX-License-Identifier: AGPL-3.0-or-later
"""SuezCanal core API entry point."""
from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional during bootstrap
    def load_dotenv(*_args, **_kwargs):
        return False

# Load .env — prefer repo root first so local/dev/prod use one canonical runtime profile
for _env in [
    Path(__file__).parents[4] / ".env",   # repo root       (local dev)
    Path(__file__).parents[2] / ".env",   # apps/api/.env   (legacy fallback)
]:
    if _env.exists():
        load_dotenv(_env)
        break

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException

from core.config import config
from core import bootstrap
from core.api.routes import alerts, drift, anomaly, forensic, integrations, ops, vessels
from core.api.routes import ingest, probability, weather, zones, intel, cases, governance, live, play, connectors
from core.api.routes import mda, audit, operator_dashboard, operator_ingestion, status
from core.db.session import init_database
from core.security import READ_ROLES, WRITE_ROLES, require_roles, validate_production_security
from core.config_validation import validate_configuration
from core.observability import configure_logging, metrics_middleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
configure_logging()
logger = logging.getLogger("seacommons.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_security()
    for _warning in validate_configuration():
        logger.warning("Configuration: %s", _warning)
    logger.info(
        "Seacommons API starting up (RUNTIME_PROFILE=%s)",
        config.RUNTIME_PROFILE,
    )
    init_database()
    if config.JOB_EXECUTION_MODE != "queue":
        bootstrap.reset_stale_computing_jobs()
    try:
        from core.intel.store import intel_store
        intel_store.load_from_db()
        intel_store.reset_computing_drifts()
    except Exception as exc:
        logger.warning("Intel DB reload failed: %s", exc)
    if (
        config.JOB_EXECUTION_MODE != "queue"
        and not config.DEMO_PUBLIC_MODE
        and config.OPENDRIFT_PREWARM_ENABLED
    ):
        try:
            from core.drift.opendrift_pool import prewarm
            prewarm()
        except Exception as exc:
            logger.warning("OpenDrift prewarm failed to start: %s", exc)
    elif config.JOB_EXECUTION_MODE != "queue" and not config.DEMO_PUBLIC_MODE:
        logger.info("OpenDrift pre-warm disabled; model will load on first use")
    if config.INTEL_MONITORS_ENABLED:
        bootstrap.start_background_sensors()
        bootstrap.start_intel_engine()
        bootstrap.start_scheduler()
    else:
        # Monitors run as a standalone process elsewhere (core.intel_worker_main)
        # writing to the same shared DB — periodically pull their writes (and
        # any metadata updates to events already cached) into this process.
        logger.info("Intel monitors disabled here (INTEL_MONITORS_ENABLED=false) — syncing from DB instead")
        _start_intel_sync_loop()
    yield
    logger.info("Seacommons API shutting down")
    try:
        from core.scheduler import stop as scheduler_stop
        scheduler_stop()
    except Exception:
        pass


def _start_intel_sync_loop() -> None:
    """Periodically pull fresh/updated intel events from DB (split-deployment mode)."""
    import threading
    import time

    def _loop():
        from core.intel.store import intel_store
        consecutive_failures = 0
        while True:
            time.sleep(30)
            consecutive_failures = _run_intel_sync_tick(
                intel_store.sync_from_db,
                consecutive_failures=consecutive_failures,
            )

    threading.Thread(target=_loop, daemon=True, name="intel-db-sync").start()


def _run_intel_sync_tick(
    sync_from_db: Callable[[], tuple[int, int]],
    *,
    consecutive_failures: int,
) -> int:
    """Run one observable sync tick and return the updated failure streak."""
    from core.observability import record_intel_sync_failure, record_intel_sync_success

    try:
        new_count, updated_count = sync_from_db()
    except Exception as exc:
        failures = consecutive_failures + 1
        record_intel_sync_failure(failures)
        log = logger.error if failures >= 3 else logger.warning
        log(
            "Intel DB sync tick failed (consecutive_failures=%d, error_type=%s)",
            failures,
            type(exc).__name__,
        )
        return failures

    record_intel_sync_success(new_count, updated_count)
    if consecutive_failures:
        logger.info(
            "Intel DB sync recovered after %d consecutive failure(s)",
            consecutive_failures,
        )
    return 0


app = FastAPI(
    title="Seacommons",
    description="Operational maritime SAR and signal dashboard",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.middleware("http")(metrics_middleware)


@app.middleware("http")
async def authorization_gate(request, call_next):
    """Protect operational reads and all mutations; webhook authenticity is route-specific."""
    path = request.url.path
    if config.DEMO_PUBLIC_MODE and request.method not in {"GET", "HEAD", "OPTIONS"} and not (
        request.method == "POST" and path == "/api/v1/alert"
    ):
        return JSONResponse(status_code=403, content={"detail": "This operation is disabled in the public demo"})
    public = path in {
        "/", "/health", "/ready", "/metrics", "/docs", "/openapi.json", "/redoc",
        "/status", "/api/v1/status",
        "/api/v1/ingest/twilio/whatsapp", "/api/v1/ingest/twilio/sms",
        "/api/v1/ingest/meta/whatsapp",
        "/api/v1/ingest/telegram", "/api/v1/ingest/webhook",
    } or (
        # Public Play simulations and the public Live map's "simulate drift"
        # action are resource-bounded by the route's per-IP rate limit and
        # global drift concurrency slot. Operational workspaces, forensic
        # records and case data remain authenticated. /api/v1/intel/external
        # is the same pattern as the ingest/webhook routes above — its own
        # HMAC shared-secret check inside the route is the real gate, not
        # OIDC roles, since it's meant for a standalone external script.
        request.method == "POST"
        and path in {"/api/v1/alert", "/api/v1/intel/auto-drift", "/api/v1/intel/external"}
    ) or (
        request.method in {"GET", "HEAD", "OPTIONS"}
        and path.startswith(("/api/v1/live/", "/api/v1/play/"))
    ) or (
        # Private operator ingestion diagnostics authenticate inside the route
        # with a gateway secret injected only by the dedicated reverse proxy.
        # This mirrors signed webhook routes: middleware does not require OIDC,
        # but the route still fails closed without the server-side credential.
        request.method in {"GET", "HEAD", "OPTIONS"}
        and path.startswith("/api/v1/operator/ingestion")
    ) or (
        # docs/prompt.md P1 C: the media route is an EXPLICIT public contract,
        # not an accident of the proxy header. It serves only the re-encoded
        # public thumbnail (media/pub/<sha>.<jpg|png>), never the private
        # durable original, and validates the key shape before any lookup.
        request.method in {"GET", "HEAD", "OPTIONS"}
        and path.startswith("/api/v1/media/")
    )
    try:
        if not public and config.INTERNAL_PROXY_SECRET:
            # AUTH_ENABLED is fail-open by design (core.security.authenticate
            # returns an administrator Principal when it's False, for local
            # dev). Until OIDC is actually deployed, this is the only thing
            # standing between the open internet and every operator-only
            # route — require_roles below would silently let anyone through.
            import hmac as _hmac_lib

            supplied = request.headers.get("x-seacommons-internal", "")
            if not _hmac_lib.compare_digest(supplied, config.INTERNAL_PROXY_SECRET):
                return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        if not public:
            if path.startswith(("/api/v1/admin", "/api/v1/governance")):
                require_roles(request, {"administrator"})
            elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                require_roles(request, WRITE_ROLES)
            elif path.startswith(("/api/v1/forensic", "/api/v1/intel", "/api/v1/ingest/signals", "/api/v1/inbox", "/api/v1/cases", "/api/v1/audit", "/api/v1/jobs", "/api/v1/admin")):
                require_roles(request, READ_ROLES)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)

# CORS: allow_credentials MUST be False when allow_origins=["*"].
# Starlette ≥0.40 raises ValueError otherwise (HTTP spec violation).
# The frontend never sends cookies, so credentials=False is correct.
# Production frontends use the dedicated SeaCommons API hosts. Local origins
# remain available for development.
_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS",
    "https://console.seacommons.org,https://demo.seacommons.org,"
    "http://localhost:5173,http://localhost:3000,http://localhost:8000",
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)

app.include_router(alerts.router)
app.include_router(drift.router)
app.include_router(anomaly.router)
app.include_router(forensic.router)
app.include_router(integrations.router)
app.include_router(ops.router)
app.include_router(vessels.router)
app.include_router(ingest.router)
app.include_router(probability.router)
app.include_router(weather.router)
app.include_router(zones.router)
app.include_router(intel.router)
app.include_router(cases.router)
app.include_router(governance.router)
app.include_router(live.router)
app.include_router(play.router)
app.include_router(connectors.router)
app.include_router(mda.router)
app.include_router(audit.router)
app.include_router(operator_ingestion.router)
app.include_router(operator_dashboard.router)
app.include_router(status.router)


@app.get("/")
async def root_status():
    return status.build_public_status(24)


@app.get("/health")
async def health():
    from core.build_info import revision

    return {
        "status": "ok",
        "runtime_profile": config.RUNTIME_PROFILE,
        "revision": revision(),
    }


@app.get("/ready")
async def ready():
    from core.db.session import engine
    from sqlalchemy import text
    try:
        with engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok"}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "error"})


@app.get("/health/data")
async def health_data():
    """Analyst data-health summary (docs/fixes.md M11/M14.5): source
    outage, stuck/invalid Drift jobs and edge-heartbeat failure, all
    observable in one call without inspecting the database manually.
    Never a database identifier or any private event content -- only
    counts, booleans and source names (core.observability_health's own
    docstring guarantee).
    """
    from dataclasses import asdict

    from core.observability_health import gather_data_health_summary

    try:
        summary = gather_data_health_summary()
    except Exception as exc:  # pragma: no cover - health check must not 500 the API
        logger.warning("health_data unavailable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"healthy": False, "error": "data_health_unavailable"},
        )
    return asdict(summary)


@app.get("/metrics")
async def metrics():
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    from core.observability import refresh_operational_gauges
    refresh_operational_gauges()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/v1/config")
async def get_config():
    return {
        "runtime_profile": config.RUNTIME_PROFILE,
        "sensors": {
            "infrasound": config.INFRASOUND_ENABLED,
            "seismic": config.SEISMIC_ENABLED,
            "tid": config.TID_ENABLED,
            "gnss": config.GNSS_ENABLED,
            "adsb": config.ADSB_ENABLED,
            "sdr": config.SDR_ENABLED,
        },
        "aisstream": bool(config.AISSTREAM_KEY),
    }
