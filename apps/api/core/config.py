# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations
import json
from typing import Any, Tuple, Type
from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from pydantic_settings.sources import EnvSettingsSource


class _SafeEnvSource(EnvSettingsSource):
    """Treats empty strings as absent for complex (list/dict) fields.

    pydantic-settings v2 calls json.loads on every non-None value for
    complex-typed fields, so passing WITNESS_ENDPOINTS="" from Docker
    raises a SettingsError before our field_validators ever run.
    """

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        # _field_is_complex is the authoritative check; value_is_complex arg may be False
        # even for list[str] fields, so we must use _field_is_complex here.
        is_complex, _ = self._field_is_complex(field)
        if (is_complex or value_is_complex) and isinstance(value, str) and not value.strip():
            return None
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class SuezCanalConfig(BaseSettings):

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _SafeEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )
    REDIS_URL: str = "redis://localhost:6379/0"
    DATABASE_URL: str = "postgresql://suez:canal@localhost:5432/suezcanal"
    AUTH_ENABLED: bool = False
    # Interim compensating control while OIDC isn't deployed yet (AUTH_ENABLED
    # is fail-open by design for local dev — see core.security.authenticate).
    # When set, the authorization_gate middleware requires every non-public
    # request to carry this value in X-SeaCommons-Internal, which only the
    # Vercel same-origin proxy (apps/web/api/proxy.js) attaches server-side —
    # the browser never sees it. Unset (the default) disables the check
    # entirely, so local dev is unaffected.
    INTERNAL_PROXY_SECRET: str = ""
    # Dedicated reverse-proxy credential for the private operator ingestion
    # surface. This value is injected by the ops-api Nginx vhost and is never
    # sent to browser JavaScript.
    OPERATOR_GATEWAY_SECRET: str = ""
    OIDC_ISSUER: str = ""
    OIDC_AUDIENCE: str = "seacommons-api"
    OIDC_JWKS_URL: str = ""
    OIDC_ROLES_CLAIM: str = "realm_access.roles"
    OIDC_DEFAULT_ROLES: list[str] = []
    OIDC_ORGANIZATION_CLAIM: str = "organization_id"
    DEFAULT_RETENTION_DAYS: int = 365
    TELEGRAM_WEBHOOK_SECRET: str = ""
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    META_EMBEDDED_SIGNUP_CONFIG_ID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_WHATSAPP_NUMBER: str = ""
    TWILIO_OPERATIONS_WHATSAPP_TO: str = ""
    PARTNER_WEBHOOK_SECRET: str = ""
    MAX_WEBHOOK_BODY_BYTES: int = 1_000_000
    OBJECT_STORAGE_ENDPOINT: str = ""
    OBJECT_STORAGE_ACCESS_KEY: str = ""
    OBJECT_STORAGE_SECRET_KEY: str = ""
    OBJECT_STORAGE_BUCKET: str = "seacommons"
    OBJECT_STORAGE_SECURE: bool = False
    MAX_ATTACHMENT_BYTES: int = 25_000_000
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_OPERATIONS_CHAT_ID: str = ""
    PUBLIC_API_URL: str = ""
    JOB_EXECUTION_MODE: str = "inline"
    OPENDRIFT_PREWARM_ENABLED: bool = True
    JOB_MAX_ATTEMPTS: int = 3
    JOB_LEASE_SECONDS: int = 900
    JOB_POLL_SECONDS: float = 1.0
    WORKER_HEARTBEAT_SECONDS: int = 15
    LOG_FORMAT: str = "json"
    WITNESS_ENDPOINTS: list[str] = []
    INFRASOUND_ENABLED: bool = False
    INFRASOUND_DEVICE: str = "rboom"
    INFRASOUND_STA_WINDOW: float = 1.0
    INFRASOUND_LTA_WINDOW: float = 30.0
    INFRASOUND_TRIGGER_RATIO: float = 3.5
    SEISMIC_ENABLED: bool = False
    SEISMIC_DEVICE: str = "adxl355"
    HYDRO_ENABLED: bool = False
    SDR_ENABLED: bool = False
    SDR_THRESHOLD_DB: float = 10.0
    # Software-only remote maritime radio. Disabled until an operator provides
    # explicit receiver descriptors with confirmed source terms.
    REMOTE_RADIO_ENABLED: bool = False
    STRUCTURED_RADIO_ENABLED: bool = False
    RADIO_DECODER_ENABLED: bool = False
    RADIO_DECODER_COMMANDS_JSON: str = ""
    RADIO_DECODER_TIMEOUT_S: float = 0.5
    RADIO_DECODER_QUEUE_SIZE: int = 32
    RADIO_LIVE_LISTEN_ENABLED: bool = False
    RADIO_LIVE_LISTEN_MAX_SECONDS: int = 120
    AUDIO_EVIDENCE_ENABLED: bool = False
    AUDIO_EVIDENCE_MAX_CLIP_SECONDS: int = 60
    AUDIO_EVIDENCE_RETENTION_POLICY: str = "7d"
    AUDIO_EVIDENCE_STORAGE_PREFIX: str = ""
    REMOTE_RADIO_MAX_RECEIVERS: int = 8
    REMOTE_RADIO_PUBLIC_POOL_ENABLED: bool = True
    REMOTE_RADIO_PUBLIC_POOL_MAX: int = 16
    REMOTE_RADIO_CONNECT_TIMEOUT_S: float = 10.0
    REMOTE_RADIO_FAILOVER_ENABLED: bool = False
    REMOTE_RADIO_CHANNEL_REPLICAS: int = 3
    REMOTE_RADIO_FAILOVER_STALE_S: float = 30.0
    REMOTE_RADIO_FAILOVER_RETRY_S: float = 120.0
    REMOTE_RADIO_RECEIVERS_JSON: str = ""
    REMOTE_RADIO_RECEIVERS_FILE: str = ""
    ADSB_ENABLED: bool = False
    ADSB_DEVICE: str = "rtlsdr"
    TID_ENABLED: bool = False
    TID_REGION_LAT: float = 35.5
    TID_REGION_LON: float = 18.0
    TID_REGION_RADIUS_KM: int = 2000
    TID_POLL_INTERVAL_S: int = 60
    TID_MIN_STATIONS: int = 3
    TID_IGS_MIRROR: str = "https://cddis.nasa.gov/archive/gnss/data/hourly"
    TID_CORS_REGIONS: list[str] = ["EUREF", "MED"]
    GNSS_ENABLED: bool = True
    CORRELATION_CONFIDENCE_ALERT: float = 0.55
    CORRELATION_CONFIDENCE_URGENT: float = 0.80
    SAR_TRIANGULATION_RADIUS_KM: float = 15.0
    SAR_TRIANGULATION_WINDOW_MIN: float = 90.0
    SAR_TRIANGULATION_THRESHOLD: float = 0.55
    # OSINT cross-source fusion (core.intel.fusion)
    FUSION_ENABLED: bool = True
    FUSION_NOTIFY_COOLDOWN_S: int = 1800
    # dark-fleet / spoofing: two AIS anomalies, same MMSI, within this window
    FUSION_SPOOFING_WINDOW_S: int = 21600
    FUSION_SPOOFING_RADIUS_KM: float = 400.0
    # grey-zone: an AIS gap/loiter within this range of subsea infra / a platform
    FUSION_INFRA_PROXIMITY_KM: float = 12.0
    FUSION_GREY_ZONE_WINDOW_S: int = 10800
    # Case correlation: before auto-opening a new case, look back this many
    # days for an already-OPEN case of the same case_type covering the same
    # vessel (matched by MMSI) or, lacking one -- most humanitarian SAR
    # reports have no AIS -- the same area. A follow-up post about an
    # existing incident days later then updates that case instead of
    # forking a duplicate one (docs/fixes.md).
    FUSION_CASE_RELINK_WINDOW_DAYS: float = 7.0
    FUSION_CASE_RELINK_RADIUS_KM: float = 50.0
    CMEMS_USERNAME: str = ""
    CMEMS_PASSWORD: str = ""
    CMEMS_CURRENT_DATASET: str = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"
    CMEMS_TEMPERATURE_DATASET: str = "cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"
    CMEMS_WAVE_DATASET: str = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
    OPEN_METEO_BASE: str = "https://api.open-meteo.com/v1"
    AIS_FUSION_ENABLED: bool = False  # deprecated compatibility toggle
    AIS_FUSION_MODE: str = "legacy"  # legacy | shadow | fused
    AISCAST_ENABLED: bool = False
    AISCAST_WS_URL: str = "wss://ais.openwaters.io/v1/stream"
    AISCAST_BBOX: str = ""
    AISCAST_NGO_MMSI_LIMIT: int = 10
    AISSTREAM_KEY: str = ""
    # Optional SEPARATE AISStream API key (different account/registration) for
    # a dedicated, globally-scoped subscription tracking the known NGO/SAR
    # fleet by MMSI. AISStream allows only one open connection per key, so
    # reusing AISSTREAM_KEY here would just get the second connection dropped
    # — see core/vessels/aisstream.py. Leave unset to skip this subscription.
    AISSTREAM_NGO_KEY: str = ""
    # AIS track history (core/vessels/track_store.py) — the primitive the
    # dark-vessel / grey-zone MDA detectors run on.
    VESSEL_TRACK_ENABLED: bool = True
    VESSEL_TRACK_MIN_INTERVAL_S: int = 60     # at most one stored row per MMSI per this
    VESSEL_TRACK_RETENTION_DAYS: int = 60
    # SourceObservation sampling off the same live feed (docs/fixes.md M1.2)
    # -- distinct from VESSEL_TRACK_MIN_INTERVAL_S: this is not a throttle,
    # it's the silence duration after which a vessel reporting again is
    # itself recorded as an observation (see core/vessels/ais_source_observation.py).
    AIS_SOURCE_OBSERVATION_GAP_S: int = 1800
    # MDA periodic scans (core/mda/watch.py) — rendezvous / STS, infra loitering,
    # AIS gap, identity screening, duplicate-MMSI.
    MDA_WATCH_ENABLED: bool = True
    MDA_SCAN_INTERVAL_S: int = 300
    MDA_RENDEZVOUS_WINDOW_MIN: float = 30
    MDA_RENDEZVOUS_MAX_SEP_M: float = 600
    MDA_RENDEZVOUS_MAX_SOG_KN: float = 2.0
    MDA_RENDEZVOUS_MIN_DURATION_MIN: float = 30
    MDA_INFRA_BUFFER_KM: float = 2.0
    MDA_INFRA_LOITER_MAX_SOG_KN: float = 1.5
    MDA_INFRA_LOITER_MIN_MIN: float = 90
    MDA_GAP_MIN_S: float = 3600
    ADSB_EXCHANGE_KEY: str = ""
    GPSJAM_URL: str = "https://gpsjam.org/geo.json"
    ACLED_KEY: str = ""
    ACLED_EMAIL: str = ""
    GFW_API_TOKEN: str = ""
    EOG_TOKEN: str = ""
    WDPA_TOKEN: str = ""
    MADRIGAL_URL: str = "https://madrigal.haystack.mit.edu"
    EMSC_WS: str = "wss://www.seismicportal.eu/standing_order/websocket"
    RUNTIME_PROFILE: str = "operational"
    EXTERNAL_DATA_TIMEOUT_S: float = 12.0
    ALERT_DRIFT_DURATION_H: int = 24
    # Offload OpenDrift/CMEMS compute to a dedicated worker VM. Leave blank to
    # keep computing in-process (the default — nothing changes if unset).
    DRIFT_WORKER_URL: str = ""
    DRIFT_WORKER_SECRET: str = ""
    DRIFT_WORKER_TIMEOUT_S: float = 90.0
    # Where intel monitors reach the API's own HTTP routes (e.g. to trigger
    # auto-drift). Defaults to same-host — only needs changing when monitors
    # run as a standalone process on a different VM than the API. Point this
    # at the API host's real IP directly, not its public domain — a public
    # domain that only works for browser/CDN traffic can (and did, verified
    # live) silently time out for server-to-server calls.
    API_INTERNAL_URL: str = "http://127.0.0.1:8100"
    # Set only if API_INTERNAL_URL is a bare IP/port: the reverse proxy in
    # front of the API host does host-based virtual routing, so a request
    # by IP alone needs an explicit Host header naming the intended vhost.
    API_INTERNAL_HOST_HEADER: str = ""
    MOCK: bool = False  # deprecated compatibility flag; operational runtime ignores it
    DEMO_PUBLIC_MODE: bool = False  # isolates the public demo and blocks operational mutations

    # ── Intel layer (Twitter/X + news + AIS spike detection) ─────────────────
    INTEL_ENABLED: bool = True
    # False on an API-only node in a split deployment (see core/intel_worker_main.py) —
    # background sensors/monitors/scheduler run on a separate process/VM instead,
    # and this node periodically syncs from the shared DB (core/bootstrap.py).
    INTEL_MONITORS_ENABLED: bool = True
    # Intel drift jobs are serialized by the shared OpenDrift semaphore;
    # results are persisted and served from the drift store after computation.
    INTEL_AUTO_DRIFT_ENABLED: bool = True
    # Analyze Alarm Phone media independently of V1 text classification. Shadow
    # mode records technical outcomes only and never changes public semantics.
    ALARM_PHONE_IMAGE_V2_ENABLED: bool = True
    ALARM_PHONE_IMAGE_V2_SHADOW: bool = False
    # Extra relay accounts whose Alarm-Phone-style images should be analysed.
    ALARM_PHONE_IMAGE_V2_ACCOUNTS: str = ""
    # Public syndication CDN fallback when twikit object shapes expose no media.
    X_MEDIA_SYNDICATION_FALLBACK: bool = True
    # AIS spike calibration: one speed drop is a cue; promotion requires persistence.
    AIS_SUDDEN_STOP_MIN_SAMPLES: int = 2
    AIS_SUDDEN_STOP_PERSISTENCE_S: int = 300
    # Shared-secret auth for an operator's own external script pushing
    # already-parsed text reports into the intel pipeline (e.g. a personal
    # tool reading some feed the operator runs themselves) — see
    # POST /api/v1/intel/external in routes/intel.py. SeaCommons never
    # touches how that data was produced; same HMAC pattern as
    # PARTNER_WEBHOOK_SECRET. Empty disables the endpoint entirely.
    EXTERNAL_INTEL_INGEST_SECRET: str = ""
    # Official X/Twitter API v2 Bearer token. NOT free since Feb 2023 — the
    # tiers with read/search access (Basic+) are paid and X additionally
    # meters some calls against a prepaid credit balance (search/recent can
    # return HTTP 402 "credits depleted" even with a valid, authenticating
    # token). The twikit monitor's distress feed does not depend on this
    # token — see x_media_utils.py, which uses X's free public syndication
    # CDN for photos only, not this API.
    TWITTER_BEARER_TOKEN: str = ""
    # Twikit: free X client that reads public tweets through a real account
    # session (cookies file exported from the browser — the Google-created
    # account has no password, so login() is impossible). Strictly opt-in:
    # enabled only when TWIKIT_ENABLED=true AND TWIKIT_COOKIES_FILE points at
    # an existing file. Events are labelled source_policy="unofficial", which
    # the "honest live feeds" policy excludes from the public live map.
    TWIKIT_ENABLED: bool = False
    TWIKIT_COOKIES_FILE: str = ""
    # Tiered polling: only accounts whose interval has elapsed are fetched each
    # sweep (no always-on polling of everything). Base interval applies to the
    # non-priority accounts.
    TWIKIT_POLL_INTERVAL_S: int = 300
    TWIKIT_PRIORITY_ACCOUNTS: str = "alarm_phone"
    TWIKIT_PRIORITY_POLL_INTERVAL_S: int = 45
    # Comma-separated X screen names to track (no @). Empty => NGO_TWITTER_HANDLES.
    TWIKIT_ACCOUNTS: str = ""
    # Telegram notification when a tracked account posts (uses TELEGRAM_BOT_TOKEN
    # + TELEGRAM_OPERATIONS_CHAT_ID).
    TWIKIT_ALERTS_ENABLED: bool = False

    # TimeZero Professional bridge
    TIMEZERO_ENABLED: bool = False
    TIMEZERO_HOST: str = "localhost"
    TIMEZERO_PORT: int = 4371
    TIMEZERO_AUTO_PUSH: bool = True
    TIMEZERO_EXPORT_DIR: str | None = None
    TIMEZERO_API_KEY: str | None = None

    @field_validator("WITNESS_ENDPOINTS", "TID_CORS_REGIONS", "OIDC_DEFAULT_ROLES", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed if str(x).strip()]
                except json.JSONDecodeError:
                    pass
            return [x.strip() for x in stripped.split(",") if x.strip()]
        return v

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


config = SuezCanalConfig()

if __name__ == "__main__":
    print("SuezCanalConfig loaded OK")
    print(f"  RUNTIME_PROFILE={config.RUNTIME_PROFILE}")
    print(f"  AISSTREAM_KEY={'SET' if config.AISSTREAM_KEY else 'NOT SET'}")
    print(f"  TID_ENABLED={config.TID_ENABLED}")
