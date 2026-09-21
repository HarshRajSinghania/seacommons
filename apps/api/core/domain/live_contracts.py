# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical vocabulary and boundary models for public Live data."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LIVE_SIGNAL_SCHEMA = "org.seacommons.live-signal/v1"
FEDERATED_EVENT_SCHEMA = "seacommons-event-v1"


class PublicationStatus(StrEnum):
    PRIVATE = "private"
    INTERNAL = "internal"
    PUBLISHED = "published"


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class IncidentLifecycle(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    NEEDS_REVIEW = "needs_review"
    ARCHIVED = "archived"


class IncidentStatus(StrEnum):
    """Real-world incident outcome, independent from Live/Play surface."""

    ACTIVE = "active"
    RESOLVED = "resolved"
    NEEDS_REVIEW = "needs_review"
    OUTCOME_UNKNOWN = "outcome_unknown"


class LiveSignalKind(StrEnum):
    DISTRESS = "distress"
    CONTEXT = "context"


class MaritimeDomain(StrEnum):
    """Which maritime-awareness compartment an intel event belongs to.

    ``sar`` is the primary operational lane (migrant and general distress) and
    the only compartment published to the public Live map by default; every
    other compartment is operator-only unless it appears in
    ``PUBLIC_MARITIME_DOMAINS`` or the event is explicitly published.
    """

    SAR = "sar"
    SANCTIONS = "sanctions"
    GREY_ZONE = "grey_zone"
    IUU_FISHING = "iuu_fishing"
    PIRACY = "piracy"
    SMUGGLING = "smuggling"
    ENVIRONMENTAL = "environmental"
    SAFETY = "safety"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IntelTier(StrEnum):
    OPERATIONAL = "operational"
    NEWS = "news"
    SIGNAL = "signal"


# docs/fixes.md Phase 2.1: the operational tier of a humanitarian record is
# the same three-value vocabulary the intel feed already uses. Alias, not a
# second enum, so a value can never mean different things on the two.
OperationalTier = IntelTier


class HumanitarianCaseType(StrEnum):
    """Canonical, finite humanitarian case taxonomy (docs/fixes.md sec 3.3).

    Distinct from ``source`` and from ``MaritimeDomain``. No ingestion path may
    invent an alternative value; unclassifiable reports use
    ``UNKNOWN_HUMANITARIAN`` (a review lane, never auto-Drift).
    """

    DISTRESS = "distress"                # active/urgent maritime distress
    MISSING = "missing"                  # people overdue / no contact
    INTERCEPTION = "interception"        # interception / return event
    PUSHBACK = "pushback"                # pushback allegation / report
    RESCUE_UPDATE = "rescue_update"      # rescue-operation update, non-originating
    RESOLUTION = "resolution"            # follow-up that resolves an existing incident
    LAND_HUMANITARIAN = "land_humanitarian"  # land / border humanitarian case
    ADVOCACY = "advocacy"               # non-operational public communication
    UNKNOWN_HUMANITARIAN = "unknown_humanitarian"  # review lane, never auto-Drift


class LocationStatus(StrEnum):
    """Where a public humanitarian record's position stands (docs/fixes.md
    Question D)."""

    POSITIONED = "positioned"
    REGION_ONLY = "region_only"
    PROCESSING = "processing"          # image received, coordinate extraction in progress
    DISPUTED = "disputed"             # OCR engines disagree
    WITHHELD_FROM_MARITIME_MAP = "withheld_from_maritime_map"  # e.g. a land event
    UNPOSITIONED = "unpositioned"


class CoordinateReviewStatus(StrEnum):
    """Trust level of an extracted coordinate. The single vocabulary shared by
    ingestion, backfill, the drift gate and the evidence comparator."""

    NOT_REQUIRED = "not_required"                # coordinate read from post text
    NOT_APPLICABLE = "not_applicable"            # coarse place/region fallback, nothing to review
    MACHINE_OCR_UNVERIFIED = "machine_ocr_unverified"
    MACHINE_OCR_CONSENSUS_VERIFIED = "machine_ocr_consensus_verified"
    MACHINE_OCR_DISPUTED_NEEDS_REVIEW = "machine_ocr_disputed_needs_review"
    HUMAN_VERIFIED = "human_verified"
    REPORTED_EXACT = "reported_exact"


class LocationPrecision(StrEnum):
    UNPOSITIONED = "unpositioned"
    APPROXIMATE = "approximate"
    REGIONAL_CENTROID = "regional_centroid"
    REPORTED_OR_DERIVED = "reported_or_derived"
    AREA = "area"
    AREA_LOW_CONFIDENCE = "area_low_confidence"


class SourcePolicy(StrEnum):
    OFFICIAL_API = "official_api"
    OFFICIAL_RSS = "official_rss"
    OFFICIAL_SITE_EMBED = "official_site_embed"
    TRUSTED_PARTNER = "trusted_partner"
    OPERATOR_PUBLISHED = "operator_published"
    ARCHIVE = "archive"
    UNOFFICIAL = "unofficial"
    NITTER = "nitter"
    SCRAPE = "scrape"
    TWSCRAPE = "twscrape"


class VerificationStatus(StrEnum):
    UNVERIFIED_PUBLIC_SOURCE = "unverified_public_source"
    OPERATOR_ASSERTED = "operator_asserted"
    MACHINE_EXTRACTED_UNVERIFIED = "machine_extracted_unverified"
    MULTI_SOURCE_CORROBORATED = "multi_source_corroborated"
    PARTNER_REPORTED = "partner_reported"
    USER_REPORTED = "user_reported"
    DERIVED = "derived"
    MODELLED_SPATIOTEMPORAL = "modelled_spatiotemporal"
    MODELLED_LIVE_FIELDS = "modelled_live_fields"


APPROVED_SOURCE_POLICIES = frozenset(
    {
        SourcePolicy.OFFICIAL_API.value,
        SourcePolicy.OFFICIAL_RSS.value,
        SourcePolicy.OFFICIAL_SITE_EMBED.value,
        SourcePolicy.TRUSTED_PARTNER.value,
    }
)
BLOCKED_SOURCE_POLICIES = frozenset(
    {
        SourcePolicy.NITTER.value,
        SourcePolicy.SCRAPE.value,
        SourcePolicy.TWSCRAPE.value,
        SourcePolicy.UNOFFICIAL.value,
    }
)

# Compartments eligible for the public Live map without an explicit per-event
# publish decision. ``sar`` is always public (the primary lane). Operators may
# widen this via the PUBLIC_MARITIME_DOMAINS env var (see core.intel.public_policy).
DEFAULT_PUBLIC_MARITIME_DOMAINS = frozenset(
    {
        MaritimeDomain.SAR.value,
        MaritimeDomain.PIRACY.value,
        # docs/fixes.md P0.1/P6.4: Maritime Safety (not_under_command,
        # aground, restricted_manoeuvrability, navwarning) is its own
        # visible product surface, not Security/Intelligence and not
        # hidden. core.live.projection._public_intel_feature's own
        # docstring already documented this as the intended default.
        MaritimeDomain.SAFETY.value,
    }
)


def _validate_geometry(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    geometry_type = value.get("type")
    if geometry_type not in {"Point", "Polygon", "MultiPolygon"}:
        raise ValueError("public geometry must be Point, Polygon, MultiPolygon, or null")
    if "coordinates" not in value:
        raise ValueError("public geometry requires coordinates")
    return value


class LiveSignalProperties(BaseModel):
    """Stable public properties; provider-specific safe metadata remains allowed."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_id: Literal["org.seacommons.live-signal/v1"] = Field(alias="schema")
    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    kind: LiveSignalKind
    severity: Severity
    tier: IntelTier
    verification_status: str = Field(min_length=1)
    publication_status: Literal["published"]
    source_policy: SourcePolicy
    timestamp_utc: datetime
    location_precision: LocationPrecision
    incident_lifecycle: IncidentLifecycle | None = None
    title: str = Field(default="", max_length=255)
    text: Literal[""] = ""
    source: str = Field(default="", max_length=64)
    url: str = ""
    # Durable Live retention contract (core.live.retention). Present only for
    # a qualified observation that has earned a floor; absence does not mean
    # "not qualified", only "predates this contract" or "not this family".
    live_entered_at: datetime | None = None
    last_qualified_observation_at: datetime | None = None
    live_expires_at: datetime | None = None


class LiveSignalFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Feature"]
    id: str = Field(min_length=1)
    geometry: dict[str, Any] | None
    properties: LiveSignalProperties

    _geometry = field_validator("geometry")(_validate_geometry)

    @model_validator(mode="after")
    def identifiers_match(self) -> LiveSignalFeature:
        if self.id != self.properties.id:
            raise ValueError("feature id and properties.id must match")
        return self


class FederatedEventInput(BaseModel):
    """Publisher payload before the edge adds receipt/hash-chain fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["seacommons-event-v1"] = Field(alias="schema")
    id: str = Field(min_length=16)
    type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    node: str = Field(min_length=1)
    observed_at: datetime
    visibility: Visibility
    confidence: float | None = Field(default=None, ge=0, le=1)
    geometry: dict[str, Any] | None
    properties: dict[str, Any]
    source_url: str | None = None

    _geometry = field_validator("geometry")(_validate_geometry)


def validate_live_signal(feature: dict[str, Any]) -> dict[str, Any]:
    """Validate without rewriting the compatibility-preserving response object."""
    LiveSignalFeature.model_validate(feature)
    return feature


def validate_federated_event_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the VM-to-edge event before it enters the durable outbox."""
    FederatedEventInput.model_validate(payload)
    return payload
