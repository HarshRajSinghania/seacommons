"""Conservative evidence lineage for OSINT fusion.

Different detectors over one sensor lineage are not independent corroboration.
Unknown lineage never counts as independent by default.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.intel.source_catalog import get_source_profile


@dataclass(frozen=True)
class EvidenceLineage:
    source_name: str
    source_family: str
    independence_group: str
    sensor_family: str


_AIS_INTERNAL_ALIASES = frozenset({
    "mda",
    "ais",
    "ais incidents",
    "aisstream",
})

_AIS_SOURCE_FAMILIES = frozenset({"ais", "ais_derived_event"})

def _sensor_family(profile: dict | None, source_name: str) -> str:
    if source_name.strip().lower() in _AIS_INTERNAL_ALIASES:
        return "ais"
    if profile is None:
        return "unknown"
    if profile.get("source_family") in _AIS_SOURCE_FAMILIES:
        return "ais"
    if profile.get("source_type") == "ais":
        return "ais"
    return "official_report" if profile.get("source_type") in {"api", "rss"} else str(
        profile.get("source_type") or "unknown"
    )


def lineage_for_event(event) -> EvidenceLineage:
    source_name = str(getattr(event, "source", "") or "").strip()
    raw_metadata = getattr(event, "metadata", None)
    if not isinstance(raw_metadata, Mapping):
        # SQLAlchemy ORM rows expose declarative MetaData as .metadata; the
        # persisted event JSON lives in .meta. Dataclass IntelEvent objects use
        # .metadata directly.
        raw_metadata = getattr(event, "meta", {})
    metadata = dict(raw_metadata or {})
    platform = str(metadata.get("platform") or "").strip().lower()
    transport = str(metadata.get("transport") or "").strip().lower()
    event_type = str(getattr(event, "type", "") or "").strip().lower()

    if platform in {"x", "twitter"}:
        return EvidenceLineage(source_name, "social_public_reports", "x_twitter_platform", "public_report")
    if platform == "mastodon":
        return EvidenceLineage(source_name, "social_public_reports", "mastodon_platform", "public_report")
    if platform == "bluesky":
        return EvidenceLineage(source_name, "social_public_reports", "bluesky_platform", "public_report")
    if event_type in {"ais_anomaly", "ais_spike", "ais_rendezvous"}:
        return EvidenceLineage(source_name, "ais", "ais_sensor_lineage", "ais")

    profile = get_source_profile(source_name)
    if profile is None and (event_type == "news" or transport == "rss"):
        return EvidenceLineage(source_name, "secondary_reporting", "secondary_news_reporting", "public_report")
    sensor_family = _sensor_family(profile, source_name)
    if sensor_family == "ais":
        return EvidenceLineage(
            source_name=source_name,
            source_family=(profile or {}).get("source_family") or "ais",
            independence_group="ais_sensor_lineage",
            sensor_family="ais",
        )
    if profile is None:
        return EvidenceLineage(source_name, "unknown", "unknown", "unknown")
    return EvidenceLineage(
        source_name=source_name,
        source_family=str(profile.get("source_family") or "unknown"),
        independence_group=str(profile.get("independence_group") or "unknown"),
        sensor_family=sensor_family,
    )
