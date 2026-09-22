# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLAlchemy ORM models — append-only forensic and operational tables."""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger, Boolean, Column, String, Float, Integer, DateTime, Index, JSON, Text,
    UniqueConstraint, ForeignKey, create_engine
)
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


# docs/prompt.md P0: one lossless width for every column that stores an intel
# event identity. Generated ids like ``spoof:247384100:circular`` overflowed
# the old 16-char intel_events.id; the 16 / 32 / 36 spread across the linked
# tables also meant a cross-table lookup could silently miss. Never hash or
# slice an identity to fit -- widen the column.
EVENT_ID_MAX_LENGTH = 64


def _event_id_column(**kwargs: object) -> "Column[str]":
    return Column(String(EVENT_ID_MAX_LENGTH), **kwargs)


class ForensicEvent(Base):
    """Append-only forensic log — one row per signed event."""
    __tablename__ = "forensic_events"
    event_id         = _event_id_column(primary_key=True)
    timestamp_utc    = Column(String(32), nullable=False)
    classification   = Column(String(64), nullable=False, index=True)
    confidence       = Column(Float, nullable=False)
    position         = Column(JSON)
    vessel_id        = Column(String(32))
    contributing_sensors = Column(JSON)
    sensor_data      = Column(JSON)
    drift_result     = Column(JSON)
    waveform_miniseed_b64 = Column(Text, default="")
    rinex_dtec_b64   = Column(Text, default="")
    public_key       = Column(String(128))
    hash_blake3      = Column(String(64))
    signature_ed25519 = Column(String(128))
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AnomalyEvent(Base):
    """All detected anomalies from any sensor channel."""
    __tablename__ = "anomaly_events"
    event_id      = _event_id_column(primary_key=True)
    timestamp_utc = Column(String(32), nullable=False)
    anomaly_type  = Column(String(64), nullable=False, index=True)
    sensor_source = Column(String(32), nullable=False, index=True)
    confidence    = Column(Float, nullable=False)
    lat           = Column(Float)
    lon           = Column(Float)
    data          = Column(JSON)
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class VesselTrackDB(Base):
    """Time-series of AIS positions per MMSI — the primitive the dark-vessel
    detectors (gap, rendezvous / STS, loiter, spoof-pattern, identity history)
    are built on. Throttled to at most one row per MMSI per ~60 s at write time
    and pruned to a rolling window (config VESSEL_TRACK_RETENTION_DAYS)."""
    __tablename__ = "vessel_tracks"
    id            = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    mmsi          = Column(String(16), nullable=False)
    ts            = Column(DateTime, nullable=False)          # AIS-reported time
    received_at   = Column(DateTime, nullable=False)          # our wall clock at receipt
    lat           = Column(Float, nullable=False)
    lon           = Column(Float, nullable=False)
    sog           = Column(Float)                              # knots
    cog           = Column(Float)                              # degrees
    heading       = Column(Float)                              # degrees, None if 511
    nav_status    = Column(Integer)
    source        = Column(String(24), nullable=False, default="aisstream")

    __table_args__ = (
        Index("ix_vessel_tracks_mmsi_ts", "mmsi", "ts"),
        Index("ix_vessel_tracks_ts", "ts"),
    )


class VesselBehaviouralBaselineDB(Base):
    """Versioned analytical baseline derived from vessel track evidence."""
    __tablename__ = "vessel_behavioural_baselines"
    baseline_id = Column(String(64), primary_key=True)
    subject_id = Column(String(64), nullable=False, index=True)
    primary_mmsi = Column(String(16), nullable=False, index=True)
    primary_imo = Column(String(16), index=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False, index=True)
    sample_count = Column(Integer, nullable=False)
    history_days = Column(Float, nullable=False)
    route_model = Column(JSON, nullable=False, default=dict)
    speed_model = Column(JSON, nullable=False, default=dict)
    port_model = Column(JSON, nullable=False, default=dict)
    silence_model = Column(JSON, nullable=False, default=dict)
    evidence_fingerprint = Column(String(64), nullable=False, index=True)
    method_version = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_vessel_baselines_mmsi_window", "primary_mmsi", "window_end"),
        UniqueConstraint("subject_id", "window_start", "window_end", "method_version", "evidence_fingerprint", name="uq_vessel_baseline_evidence_window"),
    )


class MaritimeEpisodeDB(Base):
    """Persisted derived maritime episode; raw observations remain authoritative."""
    __tablename__ = "maritime_episodes"
    episode_id = Column(String(128), primary_key=True)
    episode_family = Column(String(64), nullable=False, index=True)
    subject_ids = Column(JSON, nullable=False, default=list)
    start_at = Column(DateTime, nullable=False, index=True)
    end_at = Column(DateTime, nullable=False, index=True)
    geometry = Column(JSON)
    observation_ids = Column(JSON, nullable=False, default=list)
    feature_ids = Column(JSON, nullable=False, default=list)
    independence_groups = Column(JSON, nullable=False, default=list)
    verification_status = Column(String(32), nullable=False, index=True)
    behaviour_context = Column(JSON, nullable=False, default=dict)
    alternative_explanations = Column(JSON, nullable=False, default=list)
    evidence_fingerprint = Column(String(64), nullable=False, index=True)
    method_version = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="active", index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_maritime_episodes_family_end", "episode_family", "end_at"),
    )


class SanctionedVesselDB(Base):
    """Aggregated sanctioned-vessel reference (OpenSanctions + OFAC SDN), rebuilt
    daily by core.mda.identity.refresh_sanctions()."""
    __tablename__ = "sanctioned_vessels"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    source_list = Column(String(24), nullable=False, index=True)
    name        = Column(String(200), default="")
    name_upper  = Column(String(200), index=True)
    imo         = Column(String(10), index=True)
    mmsi        = Column(String(12), index=True)
    program     = Column(String(160), default="")
    listed_on   = Column(String(12))
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DriftResultDB(Base):
    """Computed drift trajectories — GeoJSON stored as JSON."""
    __tablename__ = "drift_results"
    drift_id      = Column(String(36), primary_key=True)
    event_id      = _event_id_column(index=True)
    domain        = Column(String(32))
    lat           = Column(Float)
    lon           = Column(Float)
    trajectory    = Column(JSON)
    cone_6h       = Column(JSON)
    cone_12h      = Column(JSON)
    cone_24h      = Column(JSON)
    impact_point  = Column(JSON)
    metadata_json = Column("metadata", JSON)
    status        = Column(String(32), default="completed")
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # docs/fixes.md M3 rule: "Drift result always records origin evidence ID
    # and model version." Dedicated columns rather than metadata_json keys --
    # complete_drift_job() replaces metadata_json wholesale with the
    # engine's own result.metadata, which would silently wipe these if they
    # lived there instead.
    origin_evidence_id = Column(String(64))
    model_version = Column(String(64))


class AlertEvent(Base):
    """Distress alerts from /api/v1/alert."""
    __tablename__ = "alert_events"
    event_id      = _event_id_column(primary_key=True)
    timestamp_utc = Column(String(32), nullable=False)
    lat           = Column(Float)
    lon           = Column(Float)
    persons       = Column(Float)
    vessel_type   = Column(String(64))
    domain        = Column(String(32))
    status        = Column(String(32), default="processing")
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class IntelEventDB(Base):
    """Lightweight log of intel events from all scraping channels."""
    __tablename__ = "intel_events"
    id            = _event_id_column(primary_key=True)
    timestamp_utc = Column(String(32),  nullable=False, index=True)
    type          = Column(String(32),  nullable=False, index=True)
    severity      = Column(String(16),  nullable=False, index=True)
    lat           = Column(Float)
    lon           = Column(Float)
    title         = Column(String(256), nullable=False)
    text          = Column(Text,        default="")
    url           = Column(String(512), default="")
    source        = Column(String(64),  nullable=False, index=True)
    linked_mmsi   = Column(String(16),  default="")
    meta          = Column(JSON,        default=dict)
    created_at    = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    # ── Canonical live classification (docs/fixes.md Phase 2.2 / 3.2) ──────────
    # Dual-written alongside `meta` for one release. All nullable / defaulted so
    # the migration is safe on a populated table; store the enum *.value string.
    schema_version           = Column(Integer,     nullable=False, server_default="1")
    source_timestamp_utc     = Column(String(32))
    received_at              = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    maritime_domain          = Column(String(32),  index=True)
    operational_tier         = Column(String(16),  index=True)
    humanitarian_case_type   = Column(String(32),  index=True)
    incident_lifecycle       = Column(String(16),  index=True)
    location_status          = Column(String(32))
    coordinate_review_status = Column(String(40))
    location_uncertainty_m   = Column(Float)

    # ── Durable Live retention contract ────────────────────────────────────
    # A qualified observation must stay visible on Live for >=24h from its
    # last qualified observation, independent of the bounded in-memory deque,
    # detector re-firing, or process restart. live_case_key groups repeated
    # qualifying observations of the same subject/anomaly-family so a later
    # one can extend (never shorten) the window instead of starting a fresh
    # one. live_expires_at is the sole server-authoritative removal signal.
    live_entered_at               = Column(DateTime, nullable=True)
    last_qualified_observation_at = Column(DateTime, nullable=True)
    live_expires_at               = Column(DateTime, nullable=True, index=True)
    live_case_key                 = Column(String(160), nullable=True, index=True)

    # docs/fixes.md F-14 / Phase 2.2: persisted_events() and the edge
    # publisher's collect() all filter a recent time window by source or type
    # and sort by timestamp_utc desc. Composite indexes serve the filter and
    # the sort in one; the single-column indexes above stay (F-14: do not drop
    # them until query plans justify it).
    __table_args__ = (
        Index("ix_intel_events_source_ts", "source", "timestamp_utc"),
        Index("ix_intel_events_type_ts", "type", "timestamp_utc"),
    )


class IngestedSignalDB(Base):
    """Canonical inbound signal; external delivery key makes webhooks idempotent."""
    __tablename__ = "ingested_signals"
    signal_id = Column(String(36), primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.organization_id"), nullable=True, index=True)
    connector_id = Column(String(36), ForeignKey("connectors.connector_id"), nullable=True, index=True)
    source_channel = Column(String(32), nullable=False, index=True)
    source_id = Column(String(256), nullable=False)
    provider_message_id = Column(String(256), nullable=True)
    payload = Column(JSON, nullable=False)
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        UniqueConstraint(
            "source_channel", "provider_message_id", name="uq_ingested_signal_delivery"
        ),
    )


class CaseDB(Base):
    __tablename__ = "cases"
    case_id = Column(String(36), primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.organization_id"), nullable=True, index=True)
    title = Column(String(256), nullable=False)
    status = Column(String(32), nullable=False, default="open", index=True)
    case_type = Column(
        String(32), nullable=False, default="distress_sar",
        server_default="distress_sar", index=True,
    )
    priority = Column(String(16), nullable=False, default="medium", index=True)
    sensitivity = Column(String(16), nullable=False, default="restricted")
    summary = Column(Text, default="")
    lat = Column(Float)
    lon = Column(Float)
    persons = Column(Float)
    assigned_to = Column(String(256))
    retention_until = Column(DateTime)
    legal_hold = Column(Integer, nullable=False, default=0)
    created_by = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class CaseSignalDB(Base):
    __tablename__ = "case_signals"
    case_id = Column(String(36), ForeignKey("cases.case_id", ondelete="CASCADE"), primary_key=True)
    signal_id = Column(String(36), ForeignKey("ingested_signals.signal_id", ondelete="CASCADE"), primary_key=True)
    linked_by = Column(String(256), nullable=False)
    linked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CaseIntelEventDB(Base):
    """Bridge between a case and the OSINT intel events that triggered / support it.

    ``case_signals`` only links messaging-channel signals (``ingested_signals``);
    OSINT events live in ``intel_events`` and had no path to a case. The
    correlation/fusion engine writes rows here when it auto-opens a case, and
    the operator can link more. No FK to ``intel_events`` — an intel event row
    is persisted on a background thread and may not exist yet when the link is
    made; the id is stable regardless.
    """

    __tablename__ = "case_intel_events"
    case_id = Column(String(36), ForeignKey("cases.case_id", ondelete="CASCADE"), primary_key=True)
    event_id = _event_id_column(primary_key=True)
    role = Column(String(32), nullable=False, default="contributing")
    linked_by = Column(String(256), nullable=False)
    linked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CaseTimelineDB(Base):
    __tablename__ = "case_timeline"
    entry_id = Column(String(36), primary_key=True)
    case_id = Column(String(36), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    actor = Column(String(256), nullable=False)
    body = Column(Text, default="")
    data = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class CaseAttachmentDB(Base):
    __tablename__ = "case_attachments"
    attachment_id = Column(String(36), primary_key=True)
    case_id = Column(String(36), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    object_key = Column(String(512), nullable=False, unique=True)
    filename = Column(String(256), nullable=False)
    content_type = Column(String(128), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    uploaded_by = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class AuditLogDB(Base):
    __tablename__ = "audit_log"
    audit_id = Column(String(36), primary_key=True)
    actor = Column(String(256), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(64), nullable=False, index=True)
    data = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class JobDB(Base):
    __tablename__ = "jobs"
    job_id = Column(String(36), primary_key=True)
    job_type = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="queued", index=True)
    payload = Column(JSON, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    available_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    lease_until = Column(DateTime)
    worker_id = Column(String(128))
    last_error = Column(Text)
    result = Column(JSON)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class WorkerHeartbeatDB(Base):
    __tablename__ = "worker_heartbeats"
    worker_id = Column(String(128), primary_key=True)
    hostname = Column(String(256), nullable=False)
    process_id = Column(Integer, nullable=False)
    current_job_id = Column(String(36))
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class OrganizationDB(Base):
    __tablename__ = "organizations"
    organization_id = Column(String(36), primary_key=True)
    name = Column(String(256), nullable=False, unique=True)
    slug = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class MembershipDB(Base):
    __tablename__ = "memberships"
    organization_id = Column(String(36), ForeignKey("organizations.organization_id", ondelete="CASCADE"), primary_key=True)
    subject = Column(String(256), primary_key=True)
    role = Column(String(32), nullable=False, default="member")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ConnectorDB(Base):
    """Partner-owned inbound channel.

    Provider credentials are never stored here. ``secret_ref`` points to an
    external secret manager entry controlled by the deployment environment.
    """
    __tablename__ = "connectors"
    connector_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(32), nullable=False, index=True)
    display_name = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    external_account_id = Column(String(128))
    external_channel_id = Column(String(128), nullable=False)
    display_address = Column(String(128))
    secret_ref = Column(String(256))
    publication_policy = Column(String(16), nullable=False, default="private")
    configuration = Column(JSON, default=dict)
    created_by = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_seen_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint(
            "provider", "external_channel_id", name="uq_connector_provider_channel"
        ),
    )


class CaseAccessDB(Base):
    __tablename__ = "case_access"
    case_id = Column(String(36), ForeignKey("cases.case_id", ondelete="CASCADE"), primary_key=True)
    subject = Column(String(256), primary_key=True)
    permission = Column(String(16), nullable=False, default="read")
    granted_by = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DeletionRequestDB(Base):
    __tablename__ = "deletion_requests"
    request_id = Column(String(36), primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.organization_id"), nullable=True)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    requested_by = Column(String(256), nullable=False)
    reviewed_by = Column(String(256))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at = Column(DateTime)


class SourceObservationDB(Base):
    """Lossless, immutable, replayable record of what a source actually sent
    (docs/fixes.md M1.1) -- the first durable layer of the canonical data
    flow (SOURCE INPUT -> RAW OBSERVATION -> ... -> PUBLIC PROJECTION),
    written before any classification decision. `IntelEventDB` remains the
    compatibility/public-projection envelope; this table is not a
    replacement for it yet -- adapters are wired onto this incrementally
    (M1.2), in parallel with their existing IntelEventDB write path.

    Idempotent by (source_name, source_id) -- see
    core.intel.source_observation.observation_id(); a re-delivered raw
    fixture resolves to the same observation_id and is a no-op, not a
    duplicate row (docs/fixes.md M1.1 exit gate).
    """

    __tablename__ = "source_observations"
    observation_id = Column(String(64), primary_key=True)
    service = Column(String(32), nullable=False, index=True)
    lane = Column(String(32), nullable=False, index=True)
    observation_type = Column(String(48), nullable=False, index=True)
    source_name = Column(String(64), nullable=False, index=True)
    source_policy = Column(String(32), nullable=False)
    source_id = Column(String(256), nullable=False)
    source_url = Column(String(512), default="")
    # ISO 8601 string, same convention as IntelEventDB.timestamp_utc -- most
    # source timestamps arrive as strings from third-party APIs/feeds.
    observed_at = Column(String(32), nullable=False)
    received_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    raw_payload_hash = Column(String(64), nullable=False)
    raw_payload_ref = Column(Text, default="")
    lat = Column(Float)
    lon = Column(Float)
    location_precision = Column(String(32))
    uncertainty_m = Column(Float)
    subject_refs = Column(JSON, default=list)
    provenance = Column(JSON, default=dict)
    schema_version = Column(Integer, nullable=False, server_default="1")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # docs/updates.md Section 6 (Preservation): a deterministic classification
    # of this observation's preservation policy -- not_applicable/preserved/
    # restricted -- computed once at record_observation() time by
    # core.intel.preservation.classify_preservation_status and never edited
    # afterward, consistent with this row's own immutability. raw_payload_ref
    # above doubles as Section 6's "archive URI/reference when available".
    preservation_status = Column(String(32))

    __table_args__ = (
        UniqueConstraint("source_name", "source_id", name="uq_source_observation_delivery_key"),
        Index("ix_source_observations_source_ts", "source_name", "observed_at"),
        # Hot-path lineage lookup: exact payload hash, earliest prior receipt.
        # Without this, every high-volume observation scans the full table.
        Index(
            "ix_source_observations_payload_received",
            "raw_payload_hash",
            "received_at",
        ),
    )


class AudioEvidenceArtifactDB(Base):
    """Metadata-only immutable reference to one bounded audio evidence clip.

    Audio bytes live in external object storage addressed by ``storage_ref``;
    this row stores only provenance, hash, retention and SourceObservation links.
    """

    __tablename__ = "audio_evidence_artifacts"
    artifact_id = Column(String(64), primary_key=True)
    artifact_type = Column(String(16), nullable=False, default="audio")
    physical_lineage = Column(String(128), nullable=False, index=True)
    receiver_id = Column(String(128), nullable=False)
    frequency_hz = Column(Integer, nullable=False, index=True)
    channel = Column(String(64), nullable=False)
    started_at = Column(String(40), nullable=False, index=True)
    ended_at = Column(String(40), nullable=False)
    duration_seconds = Column(Float, nullable=False)
    content_sha256 = Column(String(64), nullable=False, index=True)
    storage_ref = Column(Text, nullable=False)
    mime_type = Column(String(128), nullable=False)
    codec = Column(String(32), nullable=False)
    source_terms = Column(Text, nullable=False)
    retention_policy = Column(String(16), nullable=False, index=True)
    source_observation_ids = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_audio_artifacts_lineage_start", "physical_lineage", "started_at"),
    )


class SatelliteObservationDB(Base):
    """Provider-neutral satellite evidence attached to one incident.

    Stores metadata and asset references only; large raster products remain at
    the provider and are rendered/fetched on demand by public Play.
    """

    __tablename__ = "satellite_observations"
    observation_id = Column(String(64), primary_key=True)
    incident_id = Column(String(64), nullable=False, index=True)
    provider = Column(String(48), nullable=False, index=True)
    mission = Column(String(64), nullable=False)
    product_id = Column(String(256), nullable=False)
    acquisition_time = Column(String(32), nullable=False, index=True)
    discovered_at = Column(DateTime, nullable=False)
    footprint = Column(JSON)
    bbox = Column(JSON)
    sensor_type = Column(String(32), nullable=False)
    temporal_relation = Column(String(16), nullable=False)
    temporal_delta_s = Column(Float, nullable=False, default=0)
    asset_ref = Column(Text, default="")
    source_url = Column(Text, default="")
    provenance = Column(JSON, default=dict)
    resolution_m = Column(Float)
    cloud_cover = Column(Float)
    polarisation = Column(JSON)
    evidence_status = Column(String(32), nullable=False, default="contextual")
    # Whether this detection has been associated with a specific vessel --
    # separate from evidence_status. "unmatched_candidate" (core.mda.
    # darkship_cue's own vocabulary): an unmatched SAR detection inside a
    # reachable-area calculation is a candidate, never a confirmed vessel
    # match. Null means no association question applies (e.g. a routine
    # quicklook, not a dark-ship cue).
    association_status = Column(String(32), nullable=True)
    # Canonical MaritimeEpisode parent. Null means the satellite row is only
    # contextual to the originating event/incident and must not corroborate it.
    episode_id = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_satellite_observations_incident_time", "incident_id", "acquisition_time"),
    )


class InvestigationHypothesisDB(Base):
    """Persisted core.intel.hypothesis.InvestigationHypothesis (docs/fixes.md
    M6/M14.3). One row per hypothesis; audit_history is append-only JSON,
    never rewritten in place -- core.intel.hypothesis.transition() always
    appends a new AuditEntry to the tuple it returns, this column just
    stores that tuple verbatim.
    """

    __tablename__ = "investigation_hypotheses"
    hypothesis_id = Column(String(128), primary_key=True)
    episode_id = Column(String(128), nullable=True, index=True)
    hypothesis_type = Column(String(64), nullable=False, index=True)
    subject_ids = Column(JSON, nullable=False, default=list)
    state = Column(String(32), nullable=False, default="candidate", index=True)
    reason_codes = Column(JSON, default=list)
    counter_indicators = Column(JSON, default=list)
    evidence_links = Column(JSON, default=list)
    evidence_stage = Column(String(32), nullable=False, default="observed")
    has_unresolved_blocking_identity_conflict = Column(Boolean, nullable=False, default=False)
    allegation_shaped_wording = Column(Boolean, nullable=False, default=False)
    explicit_review_done = Column(Boolean, nullable=False, default=False)
    audit_history = Column(JSON, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Durable Live retention ceiling for the published state (core.live.
    # retention). updated_at is not usable for this: it bumps on every no-op
    # re-save, not only when new qualified evidence arrives, so a stale
    # candidate/collecting hypothesis could otherwise look perpetually fresh.
    live_entered_at               = Column(DateTime, nullable=True)
    last_qualified_observation_at = Column(DateTime, nullable=True)
    live_expires_at               = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        Index("ix_investigation_hypotheses_type_state", "hypothesis_type", "state"),
    )


class HumanitarianIncidentDB(Base):
    """Canonical Humanitarian incident (docs/updates.md P0.3): the
    persisted object that owns current operational state, independent of
    any one source post.

    v0 scope, honestly bounded: ``incident_id`` is 1:1 with the
    IntelEvent id that created it -- no cross-source correlation exists
    yet (docs/updates.md P2.1, a later packet), so this cannot yet merge
    two independently-reported posts about the same real-world case into
    one incident. What it DOES add over the pre-P0.3 state: persisted
    ``state_changed_at``/``resolved_at``/``archived_at`` (none of which
    existed anywhere before -- docs/updates.md P0.1's audit flagged their
    absence), computed from actual lifecycle transitions rather than
    re-derived from scratch on every read.
    """

    __tablename__ = "humanitarian_incidents"
    incident_id = Column(String(64), primary_key=True)
    lifecycle = Column(String(32), nullable=False, index=True)
    # Public real-world outcome. ``lifecycle=archived`` is retained only as a
    # compatibility marker for pre-Live/Play rows; it maps to outcome_unknown.
    incident_status = Column(String(32), nullable=False, default="active", index=True)
    case_type = Column(String(64))
    reported_at = Column(String(32))
    last_update_at = Column(String(32))
    state_changed_at = Column(DateTime)
    resolved_at = Column(DateTime)
    archived_at = Column(DateTime)
    source_observation_ids = Column(JSON, default=list)
    review_status = Column(String(32), nullable=False, default="none")
    revision = Column(Integer, nullable=False, default=1)
    # docs/updates.md P0.7: "exactly zero or one operational current Drift
    # per incident" -- a single nullable pointer makes that true by
    # construction (there is nowhere a second "current" could be stored).
    # Not yet read by core.live.feed.public_drift_collection() (which
    # still selects by rediscovering completed DriftResultDB rows,
    # docs/updates.md's own named anti-pattern) -- swapping that live
    # selection is a later, deliberately separate packet once parity is
    # proven, not this one.
    current_drift_id = Column(String(36))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class IncidentWatchDB(Base):
    """Restart-safe bounded follow-up state for one Humanitarian incident."""

    __tablename__ = "incident_watches"
    watch_id = Column(String(64), primary_key=True)
    incident_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="active", index=True)
    priority = Column(String(16), nullable=False, default="medium", index=True)
    lifecycle_snapshot = Column(String(32), nullable=False)
    profile_json = Column(JSON, nullable=False, default=dict)
    profile_version = Column(Integer, nullable=False, default=1)
    next_run_at = Column(DateTime, nullable=False, index=True)
    last_run_at = Column(DateTime)
    last_success_at = Column(DateTime)
    last_error_at = Column(DateTime)
    last_error_class = Column(String(64))
    consecutive_errors = Column(Integer, nullable=False, default=0)
    run_count = Column(Integer, nullable=False, default=0)
    query_fingerprint = Column(String(64))
    lease_owner = Column(String(128))
    lease_until = Column(DateTime, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("incident_id", name="uq_incident_watch_incident"),
        Index("ix_incident_watches_due", "status", "next_run_at", "priority"),
    )


class ClaimDB(Base):
    """One structured fact extracted from one observation (docs/updates.md
    P0.4): "important facts become claims, not mutable scalar truth."
    Never overwritten in place for a genuinely new value -- a later claim
    of the same claim_type on the same incident is a NEW row, so
    conflicting reports coexist rather than the earlier one being lost.
    Idempotent by ``claim_id`` (deterministic from incident_id +
    claim_type + observation_id): re-syncing the same observation never
    duplicates its claims.
    """

    __tablename__ = "claims"
    claim_id = Column(String(96), primary_key=True)
    incident_id = Column(String(64), nullable=False, index=True)
    claim_type = Column(String(32), nullable=False, index=True)
    value = Column(JSON, nullable=False)
    observation_id = Column(String(64), nullable=False)
    source_id = Column(String(64))
    claimed_at = Column(String(32))
    observed_at = Column(String(32))
    extraction_method = Column(String(64), nullable=False)
    verification_status = Column(String(32), nullable=False, default="unverified")
    supersedes_id = Column(String(96))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AssessmentDB(Base):
    """The selected/bounded value for one incident field, traceable back
    to the claims that support (or, once contradiction detection exists,
    contradict) it (docs/updates.md P0.4). Never a single opaque
    "trust_score" -- the module computing this documents its own
    selection method_version explicitly.
    """

    __tablename__ = "assessments"
    assessment_id = Column(String(96), primary_key=True)
    incident_id = Column(String(64), nullable=False, index=True)
    field_type = Column(String(32), nullable=False)
    value = Column(JSON)
    supporting_claim_ids = Column(JSON, default=list)
    contradicting_claim_ids = Column(JSON, default=list)
    method_version = Column(String(64), nullable=False)
    confidence = Column(Float)
    review_state = Column(String(32), nullable=False, default="unreviewed")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class IncidentTransitionDB(Base):
    """One audited lifecycle transition of a HumanitarianIncident
    (docs/updates.md P0.5): "every transition is data" -- never a silent
    state change. Append-only: a transition row is written once and
    never edited; a later re-evaluation that changes the state again
    writes a NEW row rather than mutating this one.

    v0 scope, honestly bounded: ``to_state`` is still today's 4-state
    lifecycle (active/resolved/archived/needs_review) --
    core.intel.lifecycle.distress_lifecycle() itself, not yet P0.5's
    fuller 7-state evidence-based model (reported/active/needs_review/
    unresolved_stale/resolved/archived/reopened), which needs new
    signal detection (a distinct unresolved_stale-vs-archived split, a
    reopen detector) this packet does not invent. ``reason_code`` is
    derived best-effort from the same signals distress_lifecycle()
    itself already inspects (self-reply outcome, cross-post resolution
    signal, silence) -- an honest label of what was observed, not a
    guarantee of full future-taxonomy precision.
    """

    __tablename__ = "incident_transitions"
    transition_id = Column(String(96), primary_key=True)
    incident_id = Column(String(64), nullable=False, index=True)
    from_state = Column(String(32))
    to_state = Column(String(32), nullable=False)
    transition_at = Column(DateTime, nullable=False)
    effective_at = Column(String(32))
    reason_code = Column(String(64), nullable=False)
    supporting_observation_ids = Column(JSON, default=list)
    contradicting_observation_ids = Column(JSON, default=list)
    method_version = Column(String(64), nullable=False)
    confidence = Column(Float)
    review_required = Column(Boolean, nullable=False, default=False)
    review_decision_id = Column(String(96))




class ReviewRecordDB(Base):
    """Append-only audit record for one explicit human review decision."""
    __tablename__ = "review_records"
    review_id = Column(String(64), primary_key=True)
    target_type = Column(String(32), nullable=False, index=True)
    target_id = Column(String(256), nullable=False, index=True)
    target_version = Column(String(128), nullable=False)
    evidence_snapshot_id = Column(String(256), nullable=False)
    decision = Column(String(32), nullable=False, index=True)
    rationale = Column(Text, nullable=False)
    actor = Column(String(256), nullable=False, index=True)
    reviewed_at = Column(DateTime, nullable=False, index=True)
    requested_transition = Column(String(32))
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_review_records_target_version", "target_type", "target_id", "target_version"),
    )


class SourceCoverageEventDB(Base):
    """Append-only coverage-change log (docs/updates.md P1.3): "record
    coverage break when not feasible", "version the coverage profile" --
    a source's coverage profile changing (added/removed/collection
    method changed/coverage break) is data, written once, never edited.
    ``profile_version`` increments per source with each recorded event,
    so "when did this source's coverage change" is answerable without
    inferring it from unrelated logs.
    """
    __tablename__ = "source_coverage_events"
    id = Column(String(64), primary_key=True)
    source_name = Column(String(64), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)  # added|removed|method_changed|coverage_break
    rationale = Column(Text)
    profile_version = Column(Integer, nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class CorrelationDecisionDB(Base):
    """CorrelationDecision (docs/updates.md P2.1): "Model similarity
    cannot be sole merge evidence" -- a row here is a candidate pairing
    surfaced for review, never an automatic incident merge. Append-only:
    a re-run over the same (observation_id, candidate_incident_id) pair
    writes a new row rather than editing a prior verdict, so the
    decision history for a pair is itself the audit trail.
    """
    __tablename__ = "correlation_decisions"
    id = Column(String(64), primary_key=True)
    observation_id = Column(String(64), nullable=False, index=True)
    candidate_incident_id = Column(String(64), index=True)
    decision = Column(String(16), nullable=False)  # SAME_INCIDENT|RELATED_INCIDENT|NEW_INCIDENT|UNCERTAIN
    supporting_features = Column(JSON, default=list)
    contradicting_features = Column(JSON, default=list)
    source_independence_result = Column(Boolean)
    method_version = Column(String(64), nullable=False)
    confidence = Column(Float, nullable=False)
    review_state = Column(String(32), nullable=False, default="pending_review")
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class LineageEdgeDB(Base):
    """Circular-reporting lineage (docs/updates.md P2.2): "Represent
    derivation/quotation relationships where detectable" -- one directed
    edge, ``from_observation_id`` derived_from ``to_observation_id``.
    Append-only, never edited: a re-detected edge for the same pair
    writes a new row, never updates an existing verdict.
    """
    __tablename__ = "lineage_edges"
    id = Column(String(64), primary_key=True)
    from_observation_id = Column(String(64), nullable=False, index=True)
    to_observation_id = Column(String(64), nullable=False, index=True)
    relation = Column(String(32), nullable=False)  # derived_from|quotes
    confidence = Column(Float, nullable=False)
    method_version = Column(String(64), nullable=False)
    detected_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class EntityDB(Base):
    """Canonical entity (docs/updates.md P2.3 evidence graph). "Start
    with typed relational objects/edges in PostgreSQL; evaluate
    specialized graph infrastructure only if measured queries justify
    it" -- a plain table, not a graph database. Idempotent by
    (entity_type, canonical_key) -- see core.intel.entity_graph.entity_id.
    """
    __tablename__ = "entities"
    entity_id = Column(String(64), primary_key=True)
    entity_type = Column(String(32), nullable=False, index=True)
    canonical_key = Column(String(256), nullable=False)
    display_name = Column(String(256))
    attributes = Column(JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("entity_type", "canonical_key", name="uq_entity_type_canonical_key"),
    )


class EntityRelationshipDB(Base):
    """One typed, time-bounded, provenanced edge between two EntityDB
    rows (docs/updates.md P2.3). Append-only: a relationship that
    changes over time (e.g. supersedes) is a NEW row, never an edit --
    the edge history is itself the evidence graph's audit trail.
    """
    __tablename__ = "entity_relationships"
    id = Column(String(64), primary_key=True)
    from_entity_id = Column(String(64), nullable=False, index=True)
    to_entity_id = Column(String(64), nullable=False, index=True)
    relation_type = Column(String(32), nullable=False, index=True)
    provenance = Column(JSON, default=dict)
    valid_from = Column(String(32))
    valid_to = Column(String(32))
    confidence = Column(Float, nullable=False, default=1.0)
    method_version = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))



class RadioBurstDB(Base):
    """Closed RF burst derived from bounded signal-level observations."""
    __tablename__ = "radio_bursts"
    burst_id = Column(String(64), primary_key=True)
    physical_lineage = Column(String(128), nullable=False, index=True)
    frequency_hz = Column(Integer, nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, index=True)
    ended_at = Column(DateTime, nullable=False)
    sample_count = Column(Integer, nullable=False)
    peak_signal_db = Column(Float, nullable=False)
    mean_signal_db = Column(Float, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class RadioEventDB(Base):
    """Correlated co-frequency RF event across independent physical receivers."""
    __tablename__ = "radio_events"
    event_id = Column(String(64), primary_key=True)
    frequency_hz = Column(Integer, nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, index=True)
    ended_at = Column(DateTime, nullable=False)
    independent_receivers = Column(Integer, nullable=False)
    physical_lineages = Column(JSON, nullable=False)
    burst_ids = Column(JSON, nullable=False)
    confidence = Column(Float, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class RadioAISAssociationDB(Base):
    """Audit record linking decoded DSC evidence to recent AIS history."""
    __tablename__ = "radio_ais_associations"
    observation_id = Column(String(64), primary_key=True)
    mmsi = Column(String(16), nullable=False, index=True)
    match_status = Column(String(32), nullable=False, index=True)
    confidence = Column(Float, nullable=False)
    distance_km = Column(Float)
    ais_observed_at = Column(String(40))
    episode_eligible = Column(Boolean, nullable=False, default=False)
    # Set only after a strong DSC↔AIS match is resolved to an already-existing
    # canonical MaritimeEpisode. No episode is minted from radio alone.
    episode_id = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class ReceiverCatalogDB(Base):
    """Persistent discovered/validated receiver catalog; endpoints remain private."""
    __tablename__ = "receiver_catalog"
    discovery_key = Column(String(256), primary_key=True)
    receiver_id = Column(String(128), index=True)
    public_label = Column(String(128), nullable=False)
    network_family = Column(String(32), nullable=False, index=True)
    physical_lineage = Column(String(128), index=True)
    endpoint = Column(Text, nullable=False)
    directory_source = Column(String(64), nullable=False, index=True)
    license_class = Column(String(32), nullable=False, default="public_access")
    source_terms = Column(Text)
    terms_status = Column(String(32), nullable=False, default="review_required", index=True)
    activation_status = Column(String(32), nullable=False, default="catalogued", index=True)
    country = Column(String(8))
    lat = Column(Float)
    lon = Column(Float)
    capabilities = Column(JSON, default=list)
    reachable = Column(Boolean)
    uptime_ratio = Column(Float, nullable=False, default=0.0)
    failure_rate = Column(Float, nullable=False, default=0.0)
    score = Column(Float, nullable=False, default=0.0, index=True)
    available_slots = Column(Integer)
    last_discovered_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    last_probed_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class AISCoverageSnapshotDB(Base):
    """Public station-health snapshot used only as AIS coverage context.

    A snapshot can support the proposition that an area was being received by
    an AIS station at a given time. It never creates a second evidence lineage
    for a vessel event: all AIS receivers remain part of ais_sensor_lineage.
    """
    __tablename__ = "ais_coverage_snapshots"

    snapshot_id = Column(String(128), primary_key=True)
    network = Column(String(32), nullable=False, index=True)
    station_id = Column(String(64), nullable=False, index=True)
    station_label = Column(String(128), nullable=False)
    source_url = Column(Text, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    station_status = Column(String(24), nullable=False)
    station_last_received_at = Column(DateTime, nullable=True)
    online_24h = Column(Float, nullable=False, default=0.0)
    max_reception_km = Column(Float, nullable=True)
    messages_1h = Column(Integer, nullable=False, default=0)
    vessels_1h = Column(Integer, nullable=False, default=0)
    observed_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_ais_coverage_station_observed", "station_id", "observed_at"),
    )


def create_all(database_url: str) -> None:
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    # Self-test with SQLite in-memory
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ev = ForensicEvent(
            event_id="test-001",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            classification="test",
            confidence=0.9,
            position={"lat": 35.5, "lon": 14.0, "alt": 0, "source": "manual"},
        )
        session.add(ev)
        session.commit()
        count = session.query(ForensicEvent).count()
    print(f"DB models self-test OK: {count} forensic event(s) created")
