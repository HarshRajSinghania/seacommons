from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///./core/data/test_live_feed.db")
os.environ.setdefault("RUNTIME_PROFILE", "operational")

from core.api.main import app
from core.config import config
from core.ingestion.signal import DistressSignal
from core.intel import lifecycle
from core.intel.geoextract import (
    extract_numeric_coords,
    extract_relative_coords,
    is_direct_distress_call,
    is_resolved_distress,
)
from core.intel.news_monitor import RSS_FEEDS
from core.intel.store import IntelEvent, IntelStore, intel_store
from core.intel.twitter_monitor import TwitterMonitor
from core.intel.x_media_utils import consensus_ocr_coordinate
from core.live.feed import public_signal_collection
from core.live.projection import (
    _approximate_public_point,
    _current_trajectory_estimate,
    _public_intel_feature,
)
from fastapi.testclient import TestClient

client = TestClient(app)


def test_public_projection_excludes_sensitive_content() -> None:
    event = IntelEvent(
        id="public01",
        type="distress",
        severity="high",
        lat=35.5,
        lon=14.1,
        title="Reported maritime distress",
        text="Private phone and free-form message",
        author="@private_handle",
        source="Public source",
        url="https://example.org/report",
        metadata={
            "is_distress": True,
            "private_note": "must not leak",
            "source_policy": "official_api",
        },
    )
    feature = _public_intel_feature(event)
    assert feature is not None
    assert feature["properties"]["text"] == ""
    assert "author" not in feature["properties"]
    assert "private_note" not in feature["properties"]
    assert feature["properties"]["publication_status"] == "published"


def test_public_projection_keeps_professional_vessel_identifier(monkeypatch) -> None:
    from core.vessels.registry import registry

    monkeypatch.setattr(
        registry,
        "_cache",
        {
            "352001914": {
                "ship_name": "ST. OLGA",
                "imo": "9493224",
                "ship_type": 79,
                "flag": None,
            }
        },
    )
    event = IntelEvent(
        id="olga-gap",
        type="ais_anomaly",
        severity="medium",
        lat=41.33,
        lon=29.14,
        title="AIS gap — ST. OLGA",
        source="ais",
        linked_mmsi="352001914",
        metadata={"source_policy": "official_api", "maritime_domain": "grey_zone"},
    )

    feature = _public_intel_feature(event, allowed_domains=frozenset({"grey_zone"}))

    # Raw Security AIS is evidence only after the canonical cutover.
    assert feature is None


def test_public_humanitarian_projection_excludes_vessel_identity(monkeypatch) -> None:
    """docs/fixes.md M14.4 exit gate: "Humanitarian public output must
    still exclude MMSI/IMO/tracker dossier data" -- a distress/SAR case
    that happens to carry a linked vessel (its own AIS, or a rescuing
    vessel's) must never leak that vessel's MMSI/IMO/name/flag through the
    default (humanitarian) public projection, even though the same
    identity fields are legitimately kept for Maritime/security-mode
    output (see test_public_projection_keeps_professional_vessel_identifier
    above)."""
    from core.vessels.registry import registry

    monkeypatch.setattr(
        registry,
        "_cache",
        {"209888000": {"ship_name": "RESCUE ONE", "imo": "9123456", "ship_type": 30, "flag": "MT"}},
    )
    event = IntelEvent(
        id="sar-with-vessel",
        type="distress",
        severity="high",
        lat=35.5,
        lon=14.1,
        title="Migrant boat in distress",
        source="Alarm Phone",
        linked_mmsi="209888000",
        metadata={"is_distress": True, "source_policy": "official_api"},
    )

    feature = _public_intel_feature(event)  # default allowed_domains -- humanitarian posture

    assert feature is not None
    assert feature["properties"]["maritime_domain"] == "sar"
    for field in ("linked_mmsi", "mmsi", "vessel_name", "imo", "ship_type", "flag"):
        assert field not in feature["properties"], f"{field} leaked into humanitarian public output"


def test_public_projection_exposes_repost_thread_including_its_own_note() -> None:
    # Unlike the event's own `text` (may originate from a private caller who
    # never consented to publication), a thread_reposts note only ever comes
    # from the tracked account's own public reply/quote to its own tweet —
    # already readable by anyone on X — so it is NOT stripped here.
    event = IntelEvent(
        id="public02",
        type="distress",
        severity="high",
        lat=35.5,
        lon=14.1,
        title="Reported maritime distress",
        source="alarm_phone",
        url="https://x.com/i/web/status/1",
        metadata={
            "is_distress": True,
            "source_policy": "official_api",
            "repost_count": 2,
            "last_repost_at": "2026-08-04T12:00:00+00:00",
            "thread_reposts": [
                {
                    "tweet_id": "2",
                    "posted_at": "2026-08-04T11:00:00+00:00",
                    "url": "https://x.com/i/web/status/2",
                    "kind": "repost",
                },
                {
                    "tweet_id": "3",
                    "posted_at": "2026-08-04T12:00:00+00:00",
                    "url": "https://x.com/i/web/status/3",
                    "kind": "quote",
                    "note": "Rescued to #Lampedusa! Everyone arrived safely.",
                },
            ],
        },
    )
    feature = _public_intel_feature(event)
    assert feature is not None
    props = feature["properties"]
    assert props["repost_count"] == 2
    assert props["last_repost_at"] == "2026-08-04T12:00:00+00:00"
    assert len(props["thread_reposts"]) == 2
    assert props["thread_reposts"][0].get("note") is None
    assert props["thread_reposts"][1]["kind"] == "quote"
    assert props["thread_reposts"][1]["url"] == "https://x.com/i/web/status/3"
    assert (
        props["thread_reposts"][1]["note"]
        == "Rescued to #Lampedusa! Everyone arrived safely."
    )


def test_public_projection_shows_an_area_polygon_when_no_precise_point_exists() -> None:
    polygon = {
        "type": "Polygon",
        "coordinates": [[[14.0, 35.0], [14.1, 35.0], [14.1, 35.1], [14.0, 35.0]]],
    }
    event = IntelEvent(
        id="public03",
        type="twitter",
        severity="high",
        lat=35.05,
        lon=14.05,
        title="Reported maritime distress",
        source="alarm_phone",
        url="https://x.com/i/web/status/1",
        metadata={
            "is_distress": True,
            "source_policy": "official_api",
            "coordinate_source": "region_area",
            "area_geojson": polygon,
            "area_confidence": "area_low_confidence",
        },
    )
    feature = _public_intel_feature(event)
    assert feature is not None
    assert feature["geometry"] == polygon
    assert feature["properties"]["location_precision"] == "area_low_confidence"


def test_manual_event_requires_explicit_publication() -> None:
    private = IntelEvent(
        id="manual01",
        type="manual",
        severity="high",
        lat=35.5,
        lon=14.1,
        title="Operator note",
        source="operator",
    )
    assert _public_intel_feature(private) is None
    private.metadata["publication_status"] = "published"
    assert _public_intel_feature(private) is not None


def test_explicit_private_status_overrides_approved_source_policy() -> None:
    private_rss = IntelEvent(
        id="private-rss-01",
        type="distress",
        severity="high",
        lat=37.5,
        lon=15.09,
        title="Context article mentioning distress",
        source="SOS Méditerranée",
        metadata={
            "source_policy": "official_rss",
            "publication_status": "private",
            "is_distress": True,
        },
    )

    assert _public_intel_feature(private_rss) is None


def test_public_context_signal_surfaces_in_a_public_compartment() -> None:
    # Provenance alone does not verify an article's claims. A corroborated news
    # item can appear; the otherwise identical unverified item cannot.
    news = IntelEvent(
        id="ctx-news-01",
        type="news",
        severity="medium",
        lat=35.4,
        lon=13.9,
        title="Coastguard reports vessel movement off Zawiya",
        source="Official NGO RSS",
        metadata={
            "source_policy": "official_rss",
            "verification_status": "multi_source_corroborated",
        },
    )
    feature = _public_intel_feature(news)
    assert feature is not None
    assert feature["properties"]["kind"] == "context"
    assert feature["properties"]["type"] == "news"

    news.metadata.pop("verification_status")
    assert _public_intel_feature(news) is None


def test_context_publication_does_not_depend_on_severity() -> None:
    # Product policy §4: severity must not decide publication. A high-severity
    # uncorroborated, unpublished news item is still chatter and stays off the
    # public map; a low-"severity" corroborated one still surfaces.
    base = dict(
        type="news", lat=35.4, lon=13.9, title="Report off Zawiya",
        source="Official NGO RSS",
    )
    loud = IntelEvent(id="ctx-loud", severity="critical", metadata={"source_policy": "official_rss"}, **base)
    assert _public_intel_feature(loud) is None
    quiet = IntelEvent(
        id="ctx-quiet", severity="low",
        metadata={"source_policy": "official_rss", "verification_status": "multi_source_corroborated"},
        **base,
    )
    assert _public_intel_feature(quiet) is not None


def test_correlated_alert_is_public_only_in_a_public_compartment() -> None:
    base = dict(type="correlated_alert", severity="high", lat=35.0, lon=13.5,
                title="MMSI 123: spoofing", source="SeaCommons fusion")
    sanctions = IntelEvent(id="ca-sanc", metadata={"maritime_domain": "sanctions"}, **base)
    sar = IntelEvent(id="ca-sar", metadata={"maritime_domain": "sar", "is_distress": True}, **base)
    assert _public_intel_feature(sanctions) is None
    assert _public_intel_feature(sar) is not None


def test_anonymous_infrastructure_cue_stays_operator_only() -> None:
    event = IntelEvent(
        id="anonymous-gfw-infra",
        type="correlated_alert",
        severity="medium",
        lat=35.8,
        lon=14.1,
        title="AIS loiter within 1.3 km of Greenstream pipeline",
        source="SeaCommons fusion",
        metadata={
            "alert_type": "infra_proximity",
            "maritime_domain": "grey_zone",
            "contributing_sources": ["GFW"],
        },
    )
    assert _public_intel_feature(event, allowed_domains=frozenset({"grey_zone"})) is None


def test_nuc_fusion_alert_is_safety_never_drift_eligible() -> None:
    """docs/fixes.md A-02: this fixture's own metadata sets
    maritime_domain=safety explicitly (fusion.py sets it from the alert's
    domain, "safety" since PR #62) -- it must be trusted, not overridden to
    grey_zone, and must never gain cargo-Drift eligibility regardless of
    whether the record predates PR #62's producer-side drift_eligible=False."""
    event = IntelEvent(
        id="legacy-olga",
        type="correlated_alert",
        severity="medium",
        lat=41.41,
        lon=29.43,
        title="Vessel unable to manoeuvre — ST. OLGA",
        source="SeaCommons fusion",
        linked_mmsi="352001914",
        metadata={
            "alert_type": "vessel_casualty",
            "maritime_domain": "safety",
            "contributing": ["aisinc:352001914:not_under_command"],
        },
    )

    feature = _public_intel_feature(
        event, allowed_domains=frozenset({"safety"})
    )

    assert feature is not None
    properties = feature["properties"]
    assert properties["maritime_domain"] == "safety"
    assert properties["ais_nav_status_kind"] == "not_under_command"
    assert properties["visual_category"] == "navigation_casualty"
    assert properties["visual_color"] == "#ff4d5e"
    assert properties["drift_eligible"] is False
    assert properties.get("drift_vessel_type") != "cargo"


def test_nuc_event_projects_a_case_specific_assessment_block() -> None:
    """docs/fixes.md M0.2: EventAssessment reaches the public projection as
    a nested `assessment` object -- not descriptionOf(type) generic prose."""
    event = IntelEvent(
        id="nuc-assessment-01",
        type="vessel_incident",
        severity="medium",
        lat=35.2,
        lon=14.0,
        title="Vessel unable to manoeuvre",
        source="ais",
        linked_mmsi="209888000",
        metadata={
            "ais_nav_status_kind": "not_under_command",
            "maritime_domain": "safety",
            "is_distress": False,
            "drift_eligible": False,
            "publication_status": "published",
            "source_policy": "official_api",
            "detection_reason": "Flagged after 4 report(s) over 780s (rule: ≥3 reports and ≥600s sustained).",
            "in_jamming_zone": False,
        },
    )

    feature = _public_intel_feature(event, allowed_domains=frozenset({"safety"}))

    assert feature is not None
    assessment = feature["properties"].get("assessment")
    assert assessment is not None
    assert assessment["observation"] == (
        "Flagged after 4 report(s) over 780s (rule: ≥3 reports and ≥600s sustained)."
    )
    assert "not under command" in assessment["interpretation"]
    assert assessment["evidence_level"] == "observed"
    assert assessment["classification_version"]
    assert assessment["rule_ids"] == ["not_under_command_sustained"]


def test_event_with_no_assessor_omits_the_assessment_block_entirely() -> None:
    """No generic fallback: an event kind assessment.py has no assessor for
    (e.g. plain news) must not carry an `assessment` key at all."""
    event = IntelEvent(
        id="no-assessor-01",
        type="news",
        severity="medium",
        lat=35.2,
        lon=14.0,
        title="Coastguard reports vessel movement",
        source="Official NGO RSS",
        metadata={
            "source_policy": "official_rss",
            "verification_status": "multi_source_corroborated",
        },
    )

    feature = _public_intel_feature(event)

    assert feature is not None
    assert "assessment" not in feature["properties"]


def test_unlabelled_context_stays_operator_only() -> None:
    # No source_policy, not a derived type, not published -> still private.
    bare = IntelEvent(
        id="ctx-bare-01", type="news", severity="low", lat=35.0, lon=13.0,
        title="Unlabelled article", source="somewhere",
    )
    assert _public_intel_feature(bare) is None


def test_computed_sar_products_never_enter_received_signal_feed() -> None:
    derived = IntelEvent(
        id="sar-model-01",
        type="sar_model",
        severity="medium",
        lat=35.578,
        lon=13.772,
        title="Computed SAR drift product",
        source="SeaCommons engine",
        metadata={"publication_status": "published"},
    )
    assert _public_intel_feature(derived) is None


def test_unofficial_scraper_records_never_enter_live() -> None:
    for metadata in (
        {"source_policy": "unofficial"},
        {"via": "nitter"},
        {"scrape_source": "alarmphone.org"},
    ):
        event = IntelEvent(
            id=f"blocked-{len(metadata)}",
            type="distress",
            severity="high",
            lat=35.5,
            lon=14.1,
            title="Persisted scraper report",
            source="legacy collector",
            metadata=metadata,
        )
        assert _public_intel_feature(event) is None


def test_only_official_social_transport_is_available() -> None:
    monitor = TwitterMonitor()
    assert monitor.configured is False
    assert all("nitter" not in feed["url"].lower() for feed in RSS_FEEDS)
    assert {feed["label"] for feed in RSS_FEEDS} == {
        "Alarm Phone",
        "Sea Watch",
        "SOS Méditerranée",
    }


def test_alarm_phone_official_site_policy_can_enter_live() -> None:
    event = IntelEvent(
        id="alarmphone01",
        type="twitter",
        severity="critical",
        title="Alarm Phone: reported distress",
        source="Alarm Phone",
        metadata={
            "source_policy": "official_site_embed",
            "is_distress": True,
        },
    )
    feature = _public_intel_feature(event)
    assert feature is not None
    assert feature["geometry"] is None
    assert feature["properties"]["verification_status"] == "unverified_public_source"


def test_direct_distress_call_classifier_is_conservative() -> None:
    assert is_direct_distress_call(
        "🆘 from 42 people in distress south of Crete. "
        "They have no fuel left and are drifting at sea."
    )
    assert is_direct_distress_call(
        "21 lives at risk. Rescue to a safe place is needed!"
    )
    assert is_direct_distress_call(
        "10 people are stuck on an islet. Two infants are in critical "
        "condition. We asked authorities for urgent medical assistance."
    )
    assert not is_direct_distress_call(
        "Where is the person stranded on Chafarinas Islands? Three days ago "
        "authorities claimed the person was transferred to the mainland."
    )
    assert not is_direct_distress_call(
        "There is no information about the whereabouts of this group. "
        "We fear another pushback."
    )
    assert not is_direct_distress_call(
        "The group was rescued and everyone is now safe."
    )
    assert not is_direct_distress_call(
        "The post Catania Court Acquits Crew Member appeared first on SOS MEDITERRANEE."
    )
    assert is_direct_distress_call("SOS! 20 people are in distress south of Malta")


def test_resolved_distress_ignores_a_rescue_mention_inside_an_ongoing_pushback() -> (
    None
):
    # Real Alarm Phone report (2026-07-29): a rescue is only one step in a
    # still-active rights violation (forced-return risk, refused
    # disembarkation) — the bare word "rescued" must not short-circuit this
    # to "resolved". This is the exact text that showed as a wrongly-green
    # "resolved" marker on the live map before the fix.
    assert not is_resolved_distress(
        "🚨People at risk of being forced back to #Egypt. This group was over "
        "night rescued by Merchant Vessel Safi Lion. Even though #Crete is "
        "clearly the closest port, @HCoastGuard refuses to disembark the "
        "people in #Greece! This is outrageous!"
    )


def test_resolved_distress_still_recognizes_a_clean_rescue() -> None:
    assert is_resolved_distress(
        "Rescued!! Thank you #OceanViking for rescuing the 14 people who "
        "called us when in distress on a small boat in international waters."
    )
    assert is_resolved_distress("The group was rescued and everyone is now safe.")


def test_latest_real_arrival_reply_resolves_the_incident() -> None:
    event = IntelEvent(
        type="twitter",
        severity="high",
        title="Boat carrying 25 people drifting in the Maltese SAR zone",
        text="Rescue is urgent!",
        source="alarm_phone",
        timestamp_utc="2026-08-05T16:36:54+00:00",
        metadata={
            "is_distress": True,
            "thread_reposts": [
                {
                    "tweet_id": "2085235676618846249",
                    "posted_at": "2026-08-06T05:25:01+00:00",
                    "kind": "reply",
                    "note": (
                        "We received news that the people arrived on #Sicily! "
                        "We hope that everyone is fine after the long and difficult journey."
                    ),
                }
            ],
        },
    )
    now = datetime.fromisoformat("2026-08-06T06:00:00+00:00")
    assert lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "resolved"


def test_unsafe_rescue_reply_does_not_resolve_the_incident() -> None:
    event = IntelEvent(
        type="twitter",
        severity="high",
        title="38 lives at risk south of Crete",
        text="Immediate rescue is needed",
        source="alarm_phone",
        timestamp_utc="2026-08-03T18:35:58+00:00",
        metadata={
            "is_distress": True,
            "thread_reposts": [
                {
                    "posted_at": "2026-08-04T07:19:56+00:00",
                    "note": "The situation is not resolved, the 38 people are still in danger!",
                },
                {
                    "posted_at": "2026-08-04T10:13:13+00:00",
                    "note": (
                        "The commercial vessel THEMIS rescued the people but is heading towards "
                        "Egypt. They need to be disembarked in a country of safety, which Egypt is not!"
                    ),
                },
            ],
        },
    )
    now = datetime.fromisoformat("2026-08-04T12:00:00+00:00")
    assert lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "active"


def test_latest_reply_can_reopen_a_resolved_incident_and_refresh_archive_clock() -> (
    None
):
    event = IntelEvent(
        type="twitter",
        severity="high",
        title="Boat in distress",
        text="Rescue is urgent",
        source="alarm_phone",
        timestamp_utc="2026-08-01T08:00:00+00:00",
        metadata={
            "is_distress": True,
            "thread_reposts": [
                {
                    "posted_at": "2026-08-01T10:00:00+00:00",
                    "note": "The people were rescued and arrived safely.",
                },
                {
                    "posted_at": "2026-08-02T11:30:00+00:00",
                    "note": "Correction: the situation is not resolved and they are still in danger.",
                },
            ],
        },
    )
    now = datetime.fromisoformat("2026-08-02T12:00:00+00:00")
    assert lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "active"


def test_rescue_negations_are_not_resolved() -> None:
    assert not is_resolved_distress(
        "The group has not been rescued and remains in danger."
    )
    assert not is_resolved_distress("They are still waiting to be rescued.")


def test_without_news_reply_keeps_incident_active() -> None:
    event = IntelEvent(
        type="twitter",
        severity="high",
        title="37 people at sea",
        text="The group called us from a boat in distress.",
        source="alarm_phone",
        timestamp_utc="2026-08-06T08:00:00+00:00",
        metadata={
            "thread_reposts": [
                {
                    "posted_at": "2026-08-06T15:00:00+00:00",
                    "note": (
                        "Where are they? We are still without news about the 37 people. "
                        "We have not been able to reach them the whole day."
                    ),
                }
            ],
        },
    )
    now = datetime.fromisoformat("2026-08-06T15:20:00+00:00")
    assert lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "active"


def test_lifecycle_recomputes_from_text_instead_of_trusting_stale_incident_status() -> (
    None
):
    # A stored incident_status="resolved" — the exact value the OLD,
    # over-broad is_resolved_distress() would have baked in at ingestion for
    # the pushback report above — must no longer short-circuit the lifecycle
    # colour. Only the live-recomputed text classification governs it now,
    # so a classifier fix or a same-tweet duplicate from a source that never
    # set the field (twikit does not) can never disagree with it.
    now = datetime.fromisoformat("2026-07-29T12:00:00+00:00")
    event = IntelEvent(
        id="pushback01",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text=(
            "🚨People at risk of being forced back to #Egypt. This group was "
            "over night rescued by Merchant Vessel Safi Lion. Even though "
            "#Crete is clearly the closest port, @HCoastGuard refuses to "
            "disembark the people in #Greece! This is outrageous!"
        ),
        source="Alarm Phone",
        timestamp_utc="2026-07-29T05:58:12+00:00",
        metadata={"incident_status": "resolved"},
    )
    assert lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "active"


def test_resolution_signal_ignores_a_single_shared_generic_word() -> None:
    # Real false positive: this Egypt pushback report and a totally
    # unrelated "Rescued!! Thank you OceanViking..." post from the same
    # source share only the single generic word "rescued" — that alone must
    # not mark the Egypt report resolved.
    egypt = IntelEvent(
        id="pushback02",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text=(
            "🚨People at risk of being forced back to #Egypt. This group was "
            "over night rescued by Merchant Vessel Safi Lion. Even though "
            "#Crete is clearly the closest port, @HCoastGuard refuses to "
            "disembark the people in #Greece! This is outrageous!"
        ),
        source="Alarm Phone",
        timestamp_utc="2026-07-29T05:58:12+00:00",
    )
    unrelated_rescue = IntelEvent(
        id="oceanviking01",
        type="twitter",
        severity="low",
        title="Alarm Phone",
        text=(
            "Rescued!! Thank you #OceanViking for rescuing the 14 people "
            "who called us when in distress on a small boat in "
            "international waters."
        ),
        source="Alarm Phone",
        timestamp_utc="2026-07-31T10:37:23+00:00",
    )
    assert lifecycle.has_resolution_signal(egypt, [unrelated_rescue]) is False


def test_self_reply_marks_the_incident_resolved_without_keyword_overlap() -> None:
    # A structurally-verified self-reply (twikit_monitor._check_self_replies
    # only threads a reply from the SAME author, linked via X's own
    # in_reply_to) needs no keyword-overlap check the way cross-event
    # resolution matching does — it's already proven to be about this exact
    # incident, unlike two independently-worded posts that merely share
    # vocabulary (see test_resolution_signal_ignores_a_single_shared_generic_word).
    event = IntelEvent(
        id="lampedusa01",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text="🆘 52 people in grave distress! We informed authorities in #Italy and #Malta.",
        source="Alarm Phone",
        timestamp_utc="2026-07-30T08:57:00+00:00",
        metadata={
            "thread_reposts": [
                {
                    "tweet_id": "2083234981816512957",
                    "posted_at": "2026-07-31T09:00:00+00:00",
                    "url": "https://x.com/i/web/status/2083234981816512957",
                    "kind": "reply",
                    "note": "Rescued to #Lampedusa! The 52 people have safely arrived in Italy.",
                }
            ]
        },
    )
    assert lifecycle.has_own_reply_resolution(event) is True
    now = datetime.fromisoformat("2026-07-31T12:00:00+00:00")
    assert lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "resolved"


def test_ambiguous_latest_reply_requires_review() -> None:
    event = IntelEvent(
        id="lampedusa02",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text="🆘 52 people in grave distress! We informed authorities in #Italy and #Malta.",
        source="Alarm Phone",
        timestamp_utc="2026-07-30T08:57:00+00:00",
        metadata={
            "thread_reposts": [
                {
                    "tweet_id": "1",
                    "posted_at": "2026-07-30T09:00:00+00:00",
                    "url": "https://x.com/i/web/status/1",
                    "kind": "reply",
                    "note": "Any update on this??",
                },
            ]
        },
    )
    assert lifecycle.has_own_reply_resolution(event) is False
    now = datetime.fromisoformat("2026-07-30T10:00:00+00:00")
    assert (
        lifecycle.distress_lifecycle(event, now=now, same_source=[]) == "needs_review"
    )


def test_resolution_signal_still_fires_on_genuinely_matching_follow_up() -> None:
    original = IntelEvent(
        id="boat01",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text="🆘 47 people in distress aboard the vessel Zenobia near Lampedusa.",
        source="Alarm Phone",
        timestamp_utc="2026-07-29T05:00:00+00:00",
    )
    follow_up = IntelEvent(
        id="boat01-followup",
        type="twitter",
        severity="low",
        title="Alarm Phone",
        text="Update: all 47 people aboard the Zenobia near Lampedusa were rescued and are now safe.",
        source="Alarm Phone",
        timestamp_utc="2026-07-29T09:00:00+00:00",
    )
    assert lifecycle.has_resolution_signal(original, [follow_up]) is True


def test_intel_store_deduplicates_source_ids_and_content() -> None:
    store = IntelStore()
    event = IntelEvent(
        id="tweet01",
        type="twitter",
        severity="low",
        title="Same public report",
        text="Stable content",
        source="Alarm Phone",
        metadata={"tweet_id": "2081334685649526892"},
    )
    assert store.add(event, dedup_key="x:2081334685649526892") is True
    assert store.add(event) is False


def test_intel_store_merges_same_source_url_into_canonical_event(monkeypatch) -> None:
    store = IntelStore()
    monkeypatch.setattr(store, "_persist", lambda _event: None)
    monkeypatch.setattr(store, "_persist_metadata_sync", lambda *_args: None)
    url = "https://x.com/i/web/status/2083992029869051949"
    canonical = IntelEvent(
        id="canonical01",
        type="twitter",
        severity="high",
        title="Official Alarm Phone report",
        text="37 people are missing at sea.",
        url=url,
        source="Alarm Phone",
        metadata={"source_policy": "official_site_embed", "is_distress": True},
    )
    twikit_copy = IntelEvent(
        id="temporary01",
        type="twitter",
        severity="high",
        title="Alarm Phone X report",
        text="Where are the 37 people?",
        url=url,
        source="alarm_phone",
        metadata={
            "source_policy": "operator_published",
            "tracked_account": "alarm_phone",
            "tweet_id": "2083992029869051949",
        },
    )

    assert store.add(canonical) is True
    assert store.add(twikit_copy, dedup_key="x:2083992029869051949") is False
    assert len(store.events()) == 1
    merged = store.find_by_source_url("alarm_phone", url)
    assert merged is canonical
    assert merged.metadata["tracked_account"] == "alarm_phone"
    assert merged.metadata["source_policy"] == "official_site_embed"


def test_intel_store_keeps_distinct_machine_events_with_same_vessel_url(monkeypatch) -> None:
    store = IntelStore()
    monkeypatch.setattr(store, "_persist", lambda _event: None)
    url = "https://www.marinetraffic.com/en/ais/details/ships/mmsi:352001914"
    gap = IntelEvent(
        id="aisgap:352001914",
        type="ais_anomaly",
        severity="medium",
        title="AIS gap — ST. OLGA",
        text="Gap",
        url=url,
        source="ais",
        linked_mmsi="352001914",
    )
    nuc = IntelEvent(
        id="aisinc:352001914:nuc",
        type="vessel_incident",
        severity="medium",
        title="Vessel unable to manoeuvre — ST. OLGA",
        text="NUC",
        url=url,
        source="ais",
        linked_mmsi="352001914",
    )

    assert store.add(gap) is True
    assert store.add(nuc) is True
    assert {event.id for event in store.events()} == {gap.id, nuc.id}


def test_db_duplicate_repoints_live_event_to_canonical_id() -> None:
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    url = "https://x.com/i/web/status/2083992029869051948"
    with session_scope() as db:
        db.add(
            IntelEventDB(
                id="canonical02",
                timestamp_utc="2026-08-06T10:00:00+00:00",
                type="twitter",
                severity="high",
                title="Canonical report",
                text="37 people missing at sea",
                url=url,
                source="alarm_phone",
                meta={"source_policy": "official_site_embed"},
            )
        )

    live_event = IntelEvent(
        id="temporary02",
        timestamp_utc="2026-08-06T10:00:00+00:00",
        type="twitter",
        severity="high",
        title="Twikit report",
        text="Where are the 37 people?",
        url=url,
        source="alarm_phone",
        metadata={"tracked_account": "alarm_phone", "tweet_id": "2083992029869051948"},
    )
    IntelStore()._persist_sync(live_event)

    assert live_event.id == "canonical02"
    assert live_event.metadata["tracked_account"] == "alarm_phone"
    assert live_event.metadata["source_policy"] == "official_site_embed"
    with session_scope() as db:
        persisted = (
            db.query(IntelEventDB).filter(IntelEventDB.id == "canonical02").one()
        )
        assert persisted.meta["tracked_account"] == "alarm_phone"


def test_media_ocr_requires_numeric_consensus() -> None:
    passes = [
        "GPS position N 35°30' E 012°36'",
        "Position: N 35° 30' / E 012° 36'",
        "unrelated map labels Malta",
    ]
    assert consensus_ocr_coordinate(passes) == (35.5, 12.6)
    assert consensus_ocr_coordinate([passes[0], "unrelated map labels Malta"]) is None
    assert extract_numeric_coords("Malta") is None
    assert extract_numeric_coords("N 28° 06' / W 015° 24'") == (28.1, -15.4)


def test_media_ocr_consensus_tolerates_one_bad_pass() -> None:
    # Two Tesseract layouts agree; a third misreads a degree. The clean
    # two-pass agreement should still win instead of being blocked.
    passes = [
        "N 35° 24.0' E 012° 36.0'",
        "Position N 35° 24.1' / E 012° 36.0'",
        "N 37° 10.0' E 019° 05.0'",   # garbled — a different cluster
    ]
    result = consensus_ocr_coordinate(passes)
    assert result is not None
    assert abs(result[0] - 35.4) < 0.05 and abs(result[1] - 12.6) < 0.05


def test_ocr_char_folding_recovers_common_misreads() -> None:
    # I -> 1, O -> 0 on the small text of a map label.
    lat, lon = extract_numeric_coords("N 35° I2.0' E 0I2° 36.0'")
    assert abs(lat - 35.2) < 0.01 and abs(lon - 12.6) < 0.01


def test_relative_alarm_phone_location_is_geolocated_with_declared_offset() -> None:
    lat, lon = extract_relative_coords(
        "🆘 47 people were 50 km south of #Crete, Greece when they last spoke."
    )
    # Crete's gazetteer base is just off the island's south coast (not its
    # landmass centroid — see geoextract.py), so "50 km south" lands further
    # south than the old landmass-centroid-based expectation.
    assert 34.39 < lat < 34.41
    assert 24.80 < lon < 24.82


def test_alarm_phone_screenshot_dmm_and_noisy_dms_are_parsed() -> None:
    assert extract_numeric_coords(
        "35 people in distress N 34° 37.377′, E 012° 35.525′"
    ) == (34.62295, 12.592083)
    assert extract_numeric_coords(
        '26 people N 34° 39° 36.887", E 012° 38° 36.341"'
    ) == (34.660246, 12.643428)
    assert extract_numeric_coords("49 people N 35° Q4' 17.6\", E @11° 12' 08\"") == (
        35.071556,
        11.202222,
    )


def test_relative_location_can_reference_an_island_named_earlier() -> None:
    lat, lon = extract_relative_coords(
        "47 people south of #Crete. The group was 50 km south of the island when last contacted."
    )
    # Crete's gazetteer base is just off the island's south coast (not its
    # landmass centroid — see geoextract.py), so "50 km south" lands further
    # south than the old landmass-centroid-based expectation.
    assert 34.39 < lat < 34.41
    assert 24.80 < lon < 24.82


def test_existing_event_can_be_enriched_with_media_location() -> None:
    store = IntelStore()
    event = IntelEvent(
        id="ocrplace01",
        type="twitter",
        severity="high",
        title="Public report with attached map",
        source="Alarm Phone",
    )
    assert store.add(event) is True
    metadata = {
        "coordinate_source": "media_ocr_consensus",
        "coordinate_review_status": "machine_consensus_unverified",
    }
    assert (
        store.enrich_location(
            event.id,
            lat=35.5,
            lon=12.6,
            metadata=metadata,
        )
        is True
    )
    assert event.lat == 35.5
    assert event.lon == 12.6
    assert (
        store.enrich_location(
            event.id,
            lat=36.0,
            lon=13.0,
            metadata=metadata,
        )
        is False
    )


def test_enrich_location_clears_stale_area_on_upgrade_to_a_real_point() -> None:
    # Real production bug: an event ingested from a bare place match gets a
    # real sea-only area_geojson polygon (core.intel.area_extract). When OCR
    # later finds the actual position and upgrades it via enrich_location,
    # the old polygon must not keep silently overriding the new point --
    # public_geometry_and_precision always prefers area_geojson when
    # present, so a leftover stale one makes an "active without drift"-
    # looking event show a huge obsolete area forever, even though the
    # underlying lat/lon (and drift) are already correct.
    store = IntelStore()
    event = IntelEvent(
        id="ocrarea01",
        type="twitter",
        severity="high",
        title="Boat off #Libya",
        source="alarm_phone",
        lat=31.5,
        lon=17.5,  # the area's own centroid, exactly as _ingest sets it
        metadata={
            "coordinate_source": "region_area",
            "area_geojson": {
                "type": "Polygon",
                "coordinates": [
                    [[17.0, 31.0], [18.0, 31.0], [18.0, 32.0], [17.0, 31.0]]
                ],
            },
            "area_confidence": "area_low_confidence",
        },
    )
    assert store.add(event) is True
    assert (
        store.enrich_location(
            event.id,
            lat=32.5,
            lon=17.8,
            metadata={
                "coordinate_source": "media_ocr_text",
                "location_uncertainty_m": 1500,
            },
        )
        is True
    )
    assert "area_geojson" not in event.metadata
    assert "area_confidence" not in event.metadata
    assert event.lat == 32.5 and event.lon == 17.8


def test_sensitive_public_position_is_stable_and_approximate() -> None:
    original = (35.5, 14.1)
    first = _approximate_public_point("signal-privacy-test", *original)
    second = _approximate_public_point("signal-privacy-test", *original)
    assert first == second
    assert first != original
    assert abs(first[0] - original[0]) < 0.03
    assert abs(first[1] - original[1]) < 0.03


def test_live_feed_merges_durable_alarm_phone_events_after_memory_eviction(
    monkeypatch,
) -> None:
    durable = IntelEvent(
        id="alarm-durable-01",
        timestamp_utc=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        type="twitter",
        severity="high",
        lat=34.79,
        lon=24.81,
        title="Alarm Phone: direct distress",
        source="Alarm Phone",
        metadata={"source_policy": "official_site_embed", "is_distress": True},
    )
    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: [])
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events",
        lambda **_kwargs: [durable],
    )
    collection = public_signal_collection(limit=50, days=1)
    assert [feature["properties"]["id"] for feature in collection["features"]] == [
        "intel:alarm-durable-01"
    ]
    assert collection["meta"]["durable_alarm_phone_candidates"] == 1


def test_durable_alarm_phone_read_matches_both_real_source_spellings() -> None:
    """Regression: persisted_events(source="Alarm Phone") was an exact,
    case-sensitive DB match. core.intel.twikit_monitor writes source=author
    or handle per tweet -- "Alarm Phone" (display name) when the tweet
    carried one, "alarm_phone" (handle) otherwise -- both real, current
    production values for the same account. The old exact match silently
    dropped every "alarm_phone"-sourced row from the durable safety net that
    exists specifically so high-volume MDA churn in the shared in-memory
    deque can never starve the public feed of real distress reports. This
    exercises the actual SQL filter -- no persisted_events mock -- so it
    would have caught the bug the mocked test above cannot."""
    from core.db.models import IntelEventDB
    from core.db.session import session_scope

    with intel_store._lock:
        intel_store._events.clear()
        intel_store._seen.clear()

    with session_scope() as db:
        db.add(IntelEventDB(
            id="durlower1", timestamp_utc=datetime.now(timezone.utc).isoformat(),
            type="twitter", severity="low", lat=41.55, lon=26.52,
            title="Alarm Phone: distress in Evros", text="9 people",
            url="https://x.com/i/web/status/durlower1", source="alarm_phone",
            meta={"source_policy": "operator_published", "publication_status": "published",
                  "is_distress": True, "tweet_id": "durlower1", "tracked_account": "alarm_phone"},
        ))
        db.add(IntelEventDB(
            id="durupper1", timestamp_utc=datetime.now(timezone.utc).isoformat(),
            type="twitter", severity="low", lat=35.5, lon=12.6,
            title="Alarm Phone: distress off Lampedusa", text="13 people",
            url="https://x.com/i/web/status/durupper1", source="Alarm Phone",
            meta={"source_policy": "operator_published", "publication_status": "published",
                  "is_distress": True, "tweet_id": "durupper1", "tracked_account": "alarm_phone"},
        ))

    collection = public_signal_collection(limit=50, days=1)
    ids = {feature["properties"]["id"] for feature in collection["features"]}
    assert "intel:durlower1" in ids
    assert "intel:durupper1" in ids


def test_load_from_db_seeds_dedup_keys_for_capped_out_alarm_phone_incidents() -> None:
    """docs/fixes.md sec 2: the `limit` most-recent rows load_from_db pulls can
    be only a few hours of AIS churn. A still-tracked ~20 h old Alarm Phone
    incident falls outside it, so its dedup key was never seeded -- and the X
    monitor's next catch-up poll then raised a *second* marker for one boat.
    load_from_db now seeds the dedup keys for every recent humanitarian-source
    row regardless of the cap."""
    from core.db.models import IntelEventDB
    from core.db.session import session_scope
    from core.intel.store import IntelStore

    store = IntelStore()
    with session_scope() as db:
        db.add(IntelEventDB(
            id="capped-ap-1", timestamp_utc=datetime.now(timezone.utc).isoformat(),
            type="twitter", severity="high", lat=34.27, lon=11.94,
            title="Alarm Phone: distress in the Central Med", text="~37 people",
            url="https://x.com/i/web/status/2094849490314486246", source="alarm_phone",
            meta={"is_distress": True, "tweet_id": "2094849490314486246",
                  "tracked_account": "alarm_phone"},
        ))

    # limit=0 forces the "capped out" situation: the main recency query loads
    # nothing, so only the humanitarian-source seeding pass can catch it.
    store.load_from_db(limit=0, max_age_days=1)

    assert "x:2094849490314486246" in store._seen
    from core.intel.store import IntelEvent
    reingest = IntelEvent(
        id="fresh-uuid", type="twitter", lat=35.0, lon=15.0,
        title="Alarm Phone: distress in the Central Med",
        text="~37 people", url="https://x.com/i/web/status/2094849490314486246",
        metadata={"tweet_id": "2094849490314486246"},
    )
    assert store.add(reingest, dedup_key="x:2094849490314486246") is False


def test_durable_public_scan_is_not_starved_by_one_noisy_type(monkeypatch) -> None:
    """A burst of newer security rows must not hide another public event family.

    Production can generate thousands of correlated alerts per hour. A single
    mixed durable query capped at 1500 then contains only that noisy family,
    so published Safety/Humanitarian rows disappear before projection.
    """
    now = datetime.now(timezone.utc).isoformat()
    safety = IntelEvent(
        id="durable-fairness-safety-01",
        timestamp_utc=now,
        type="vessel_incident",
        severity="medium",
        lat=35.2,
        lon=14.0,
        title="Vessel unable to manoeuvre",
        source="ais",
        metadata={
            "ais_nav_status_kind": "not_under_command",
            "maritime_domain": "safety",
            "publication_status": "published",
            "source_policy": "official_api",
            "is_distress": False,
        },
    )
    noisy = IntelEvent(
        id="durable-fairness-noisy-01",
        timestamp_utc=now,
        type="correlated_alert",
        severity="low",
        lat=34.0,
        lon=13.0,
        title="Internal correlation",
        source="SeaCommons fusion",
        metadata={"maritime_domain": "grey_zone", "publication_status": "internal"},
    )

    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: [])

    def persisted_events(**kwargs):
        if kwargs.get("source_in"):
            return []
        requested = kwargs.get("types") or []
        if requested == ["vessel_incident"]:
            return [safety]
        if len(requested) > 1:
            return [noisy] * 1500
        return []

    monkeypatch.setattr("core.live.feed.intel_store.persisted_events", persisted_events)
    monkeypatch.setattr("core.live.feed._published_ingested_features", lambda _limit: [])

    collection = public_signal_collection(limit=50, days=1, mode="maritime")
    ids = {feature["properties"]["id"] for feature in collection["features"]}
    assert "intel:durable-fairness-safety-01" in ids


def test_mode_all_reserves_humanitarian_features_from_security_flood() -> None:
    """Regression: mode=all used to merge humanitarian + security then
    truncate to `limit` by a flat recency sort. Security fires far more
    often than a genuine humanitarian report, so a burst of newer security
    features could push every humanitarian feature past the cut -- observed
    in production: mode_counts.humanitarian correctly said 1 while
    `features` contained zero humanitarian items. Reserve every eligible
    humanitarian feature first; security fills whatever budget remains."""
    with intel_store._lock:
        intel_store._events.clear()
        intel_store._seen.clear()

    base = datetime.now(timezone.utc)
    humanitarian = IntelEvent(
        id="floodtest-hum-01",
        timestamp_utc=(base - timedelta(minutes=30)).isoformat(),
        type="distress",
        severity="high",
        lat=34.8,
        lon=14.2,
        title="Reported maritime distress",
        source="Alarm Phone",
        metadata={"is_distress": True, "maritime_domain": "sar",
                  "source_policy": "official_site_embed"},
    )
    assert intel_store.add(humanitarian) is True

    limit = 5
    for i in range(limit + 10):
        security = IntelEvent(
            id=f"floodtest-sec-{i:02d}",
            timestamp_utc=(base - timedelta(minutes=i)).isoformat(),
            type="ais_anomaly",
            severity="high",
            lat=35.1,
            lon=14.5,
            title=f"AIS identity anomaly {i}",
            source="SeaCommons MDA",
            metadata={"maritime_domain": "grey_zone"},
        )
        assert intel_store.add(security) is True

    collection = public_signal_collection(limit=limit, days=1, mode="all")
    assert collection["meta"]["mode_counts"]["humanitarian"] == 1
    ids = {feature["properties"]["id"] for feature in collection["features"]}
    assert "intel:floodtest-hum-01" in ids
    # Raw security anomalies no longer fill the public transport budget.
    assert len(collection["features"]) == 1
    # Public counter is the real eligible Live population, never the transport cap.
    assert collection["meta"]["total"] == collection["meta"]["mode_counts"]["humanitarian"] + collection["meta"]["mode_counts"]["maritime"]
    assert collection["meta"]["total"] == len(collection["features"])


def test_public_feed_modes_return_separate_signals_and_counts(monkeypatch) -> None:
    """Humanitarian eligibility is domain + policy based, not source based --
    a distress report from a non-Alarm-Phone source (MSF Sea here) with an
    approved source_policy must survive mode="humanitarian" on equal footing
    with Alarm Phone. Alarm Phone is a privileged source, not a gate."""
    now = datetime.now(timezone.utc).isoformat()
    humanitarian = IntelEvent(
        id="mode-humanitarian-01",
        timestamp_utc=now,
        type="distress",
        severity="high",
        lat=34.8,
        lon=14.2,
        title="Reported maritime distress",
        source="Alarm Phone",
        metadata={
            "is_distress": True,
            "maritime_domain": "sar",
            "source_policy": "official_site_embed",
        },
    )
    security = IntelEvent(
        id="mode-security-01",
        timestamp_utc=now,
        type="ais_anomaly",
        severity="high",
        lat=35.1,
        lon=14.5,
        title="AIS identity anomaly",
        source="SeaCommons MDA",
        metadata={"maritime_domain": "grey_zone"},
    )
    other_humanitarian = IntelEvent(
        id="mode-other-ngo-01",
        timestamp_utc=now,
        type="distress",
        severity="high",
        lat=34.9,
        lon=14.3,
        title="Other NGO distress report",
        source="MSF Sea",
        metadata={
            "is_distress": True,
            "maritime_domain": "sar",
            "source_policy": "official_site_embed",
        },
    )
    humanitarian_context = IntelEvent(
        id="mode-humanitarian-context-01",
        timestamp_utc=now,
        type="twitter",
        severity="medium",
        lat=34.7,
        lon=14.1,
        title="Alarm Phone humanitarian update",
        source="Alarm Phone",
        metadata={
            "maritime_domain": "sar",
            "source_policy": "official_site_embed",
        },
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.events",
        lambda **_kwargs: [humanitarian, other_humanitarian, security, humanitarian_context],
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr("core.live.feed._published_ingested_features", lambda _limit: [])

    humanitarian_feed = public_signal_collection(limit=50, mode="humanitarian")
    security_feed = public_signal_collection(limit=50, mode="security")
    small_feed = public_signal_collection(limit=1, mode="humanitarian")
    incremental_feed = public_signal_collection(
        limit=1,
        mode="humanitarian",
        since="9999-01-01T00:00:00+00:00",
    )

    assert {feature["properties"]["id"] for feature in humanitarian_feed["features"]} == {
        "intel:mode-humanitarian-01",
        "intel:mode-other-ngo-01",
        "intel:mode-humanitarian-context-01",
    }
    assert security_feed["features"] == []
    expected_counts = {"humanitarian": 3, "maritime": 0}
    assert humanitarian_feed["meta"]["mode_counts"] == expected_counts
    assert security_feed["meta"]["mode_counts"] == expected_counts
    assert small_feed["meta"]["mode_counts"] == expected_counts
    assert incremental_feed["meta"]["mode_counts"] == expected_counts
    assert humanitarian_feed["meta"]["role_counts"] == {
        "humanitarian_case": 3,
        "humanitarian_observation": 0,
        "maritime_episode": 0,
        "maritime_evidence": 0,
        "operational_signal": 0,
        "maritime_signal": 0,
    }
    assert len(small_feed["features"]) == 1
    assert incremental_feed["features"] == []


def test_maritime_mode_shows_nuc_and_never_leaks_into_humanitarian(monkeypatch) -> None:
    """Maritime Safety belongs to the canonical public Maritime compartment.

    Safety remains an internal domain, but the public UI/API must not expose it
    as a third top-level compartment.
    """
    now = datetime.now(timezone.utc).isoformat()
    nuc = IntelEvent(
        id="mode-safety-nuc-01",
        timestamp_utc=now,
        type="vessel_incident",
        severity="medium",
        lat=35.2,
        lon=14.0,
        title="Vessel unable to manoeuvre",
        source="ais",
        linked_mmsi="209888000",
        metadata={
            "ais_nav_status_kind": "not_under_command",
            "maritime_domain": "safety",
            "service": "maritime",
            "lane": "safety",
            "is_distress": False,
            "drift_eligible": False,
            "publication_status": "published",
            "source_policy": "official_api",
        },
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.events", lambda **_kwargs: [nuc]
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events", lambda **_kwargs: []
    )
    monkeypatch.setattr("core.live.feed._published_ingested_features", lambda _limit: [])

    maritime_feed = public_signal_collection(limit=50, mode="maritime")
    humanitarian_feed = public_signal_collection(limit=50, mode="humanitarian")
    legacy_security_feed = public_signal_collection(limit=50, mode="security")

    maritime_ids = {f["properties"]["id"] for f in maritime_feed["features"]}
    assert "intel:mode-safety-nuc-01" in maritime_ids
    assert maritime_feed["meta"]["mode_counts"]["maritime"] == 1
    assert maritime_feed["meta"]["domain_counts"]["safety"] == 1
    assert humanitarian_feed["features"] == []
    assert {f["properties"]["id"] for f in legacy_security_feed["features"]} == maritime_ids
    for feature in maritime_feed["features"]:
        assert feature["properties"]["maritime_domain"] == "safety"
        assert feature["properties"]["drift_eligible"] is False


def test_ocr_suffix_dms_with_seconds_recovers_real_alarm_phone_format() -> None:
    # Real screenshot OCR: hemispheres follow the seconds and Tesseract may
    # render the minute separator as a colon or a space.
    expected = (37.308694, 27.164194)
    first = extract_numeric_coords("37°18:31.3°N\n27°09'51.1°E")
    second = extract_numeric_coords("37°18 31.3 N\n27°09 51.1 E")
    assert first is not None and second is not None
    assert abs(first[0] - expected[0]) < 0.002
    assert abs(first[1] - expected[1]) < 0.002
    assert abs(second[0] - expected[0]) < 0.002
    assert abs(second[1] - expected[1]) < 0.002


def test_current_position_uses_elapsed_time_on_sampled_trajectory() -> None:
    trajectory = {
        "geometry": {"type": "LineString", "coordinates": [[14.0, 35.0], [15.0, 36.0]]},
        "properties": {
            "timestamps_utc": ["2026-08-01T10:00:00Z", "2026-08-01T12:00:00Z"]
        },
    }
    estimate = _current_trajectory_estimate(
        trajectory,
        event_timestamp="2026-08-01T09:00:00Z",
        now=datetime.fromisoformat("2026-08-01T11:00:00+00:00"),
    )
    assert estimate is not None
    assert estimate["geometry"]["coordinates"] == [14.5, 35.5]
    assert estimate["properties"]["elapsed_hours"] == 2.0
    assert estimate["properties"]["trajectory_state"] == "interpolated"


def test_drift_not_shown_for_resolved_or_archived_incidents(monkeypatch) -> None:
    # Once an incident is resolved (or has gone stale/archived), the search
    # is over -- an active-looking pulsing drift cone still on the map reads
    # as "still adrift, still searching", which is exactly wrong.
    from datetime import timezone

    from core.live.feed import public_drift_collection

    now_iso = datetime.now(timezone.utc).isoformat()
    active_event = IntelEvent(
        id="drift-active",
        type="distress",
        severity="high",
        lat=35.0,
        lon=14.0,
        title="MAYDAY still adrift",
        source="alarm_phone",
        timestamp_utc=now_iso,
        metadata={
            "is_distress": True,
            "source_policy": "official_api",
            "drift_job_id": "job-active",
            "coordinate_source": "media_ocr_text",
            "coordinate_review_status": "machine_ocr_unverified",
            "location_status": "positioned",
            "maritime_domain": "sar",
        },
    )
    resolved_event = IntelEvent(
        id="drift-resolved",
        type="distress",
        severity="high",
        lat=35.0,
        lon=14.0,
        title="Rescued! Everyone is safe.",
        source="alarm_phone",
        timestamp_utc=now_iso,
        metadata={
            "is_distress": True,
            "source_policy": "official_api",
            "drift_job_id": "job-resolved",
            "coordinate_source": "media_ocr_text",
            "coordinate_review_status": "machine_ocr_unverified",
            "location_status": "positioned",
            "maritime_domain": "sar",
        },
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.events",
        lambda **_kwargs: [active_event, resolved_event],
    )
    fake_drift = {
        "trajectory": {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[14.0, 35.0], [14.1, 35.1]],
            },
            "properties": {},
        },
        "cone_24h": None,
        "impact_point": {},
        "metadata": {"published": True},
    }
    monkeypatch.setattr("core.db.store.get_drift", lambda job_id: fake_drift)
    monkeypatch.setattr("core.live.feed._is_publishable_live_drift", lambda drift: True)

    # docs/updates.md P0.11: public_drift_collection now reads ONLY the
    # canonical incident's current_drift_id, never event.metadata
    # ["drift_job_id"] directly -- seed the real authority the same way
    # core.intel.drift_service does on a real drift completion.
    from core.db.models import HumanitarianIncidentDB, IncidentTransitionDB
    from core.db.session import engine
    from core.intel.drift_ownership import sync_current_drift_for_incident
    from core.intel.humanitarian_incident import sync_incident_for_event

    HumanitarianIncidentDB.__table__.create(bind=engine(), checkfirst=True)
    IncidentTransitionDB.__table__.create(bind=engine(), checkfirst=True)
    sync_incident_for_event(active_event, lifecycle="active")
    sync_incident_for_event(resolved_event, lifecycle="resolved")
    sync_current_drift_for_incident("drift-active", "job-active")
    sync_current_drift_for_incident("drift-resolved", "job-resolved")  # refused: resolved

    collection = public_drift_collection(limit=50)
    event_ids = {f["properties"]["intel_event_id"] for f in collection["features"]}
    assert "drift-active" in event_ids
    assert "drift-resolved" not in event_ids


def test_needs_review_alarm_phone_point_keeps_operational_drift(monkeypatch) -> None:
    """Product policy §2/§11-A: every eligible Alarm Phone maritime point's
    persisted operational drift reaches public Live. `needs_review` is an OPEN
    lifecycle state (a human still has to confirm the outcome), NOT a reason to
    hide the trajectory. The drift also inherits the red Alarm Phone category,
    never a severity."""
    from datetime import timezone

    from core.live.feed import public_drift_collection

    now_iso = datetime.now(timezone.utc).isoformat()
    event = IntelEvent(
        id="ap-needs-review",
        type="distress",
        severity="low",  # severity must not matter
        lat=35.0,
        lon=14.0,
        title="30 people adrift",
        source="alarm_phone",
        timestamp_utc=now_iso,
        metadata={
            "is_distress": True,
            "source_policy": "official_api",
            "drift_job_id": "job-nr",
            "incident_lifecycle": "needs_review",
            "coordinate_source": "media_ocr_text",
            "coordinate_review_status": "machine_ocr_unverified",
            "location_status": "positioned",
            "maritime_domain": "sar",
            "humanitarian_case_type": "distress",
        },
    )
    region_only = IntelEvent(
        id="ap-region-only",
        type="distress",
        severity="high",
        lat=35.0,
        lon=14.0,
        title="Boat somewhere in the Maltese SAR zone",
        source="alarm_phone",
        timestamp_utc=now_iso,
        metadata={
            "is_distress": True,
            "source_policy": "official_api",
            "drift_job_id": "job-ro",
            "incident_lifecycle": "active",
            "coordinate_source": "region_area",
            "location_status": "region_only",
            "maritime_domain": "sar",
        },
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.events", lambda **_kwargs: [event, region_only]
    )
    fake_drift = {
        "trajectory": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[14.0, 35.0], [14.1, 35.1]]},
            "properties": {},
        },
        "cone_24h": None,
        "impact_point": {},
        "metadata": {"published": True},
    }
    monkeypatch.setattr("core.db.store.get_drift", lambda job_id: fake_drift)
    monkeypatch.setattr("core.live.feed._is_publishable_live_drift", lambda drift: True)

    # docs/updates.md P0.11: seed the real current_drift_id authority.
    from core.db.models import HumanitarianIncidentDB, IncidentTransitionDB
    from core.db.session import engine
    from core.intel.drift_ownership import sync_current_drift_for_incident
    from core.intel.humanitarian_incident import sync_incident_for_event

    HumanitarianIncidentDB.__table__.create(bind=engine(), checkfirst=True)
    IncidentTransitionDB.__table__.create(bind=engine(), checkfirst=True)
    sync_incident_for_event(event, lifecycle="needs_review")
    sync_current_drift_for_incident("ap-needs-review", "job-nr")

    collection = public_drift_collection(limit=50)
    by_id = {f["properties"]["intel_event_id"]: f["properties"] for f in collection["features"]}
    assert "ap-needs-review" in by_id
    assert "ap-region-only" not in by_id  # §11-C: no fabricated trajectory
    props = by_id["ap-needs-review"]
    assert props["visual_category"] == "humanitarian_alarm_phone"
    assert props["visual_color"] == "#ff3b3b"
    assert "intel_severity" not in props


def test_drift_never_shown_for_maritime_security_domain(monkeypatch) -> None:
    """docs/deep-research-report.md #17, hard requirement: SeaCommons Drift is
    a humanitarian SAR model only. A security-domain event must never project
    a drift feature, even if it somehow carries a completed drift_job_id."""
    from datetime import timezone

    from core.live.feed import public_drift_collection

    now_iso = datetime.now(timezone.utc).isoformat()
    security_event = IntelEvent(
        id="drift-security",
        type="ais_anomaly",
        severity="high",
        lat=35.0,
        lon=14.0,
        title="AIS spoofing near sanctioned vessel",
        source="SeaCommons MDA",
        timestamp_utc=now_iso,
        metadata={
            "anomaly_type": "sanctioned_vessel",
            "source_policy": "official_api",
            "drift_job_id": "job-security",
        },
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.events", lambda **_kwargs: [security_event]
    )
    fake_drift = {
        "trajectory": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[14.0, 35.0], [14.1, 35.1]]},
            "properties": {},
        },
        "cone_24h": None,
        "impact_point": {},
        "metadata": {"published": True},
    }
    monkeypatch.setattr("core.db.store.get_drift", lambda job_id: fake_drift)
    monkeypatch.setattr("core.db.store.list_drift_jobs_for_event", lambda event_id: [])
    monkeypatch.setattr("core.live.feed._is_publishable_live_drift", lambda drift: True)

    collection = public_drift_collection(limit=50)
    assert collection["features"] == []


def test_drift_never_shown_for_piracy_domain(monkeypatch) -> None:
    """docs/deep-research-report (2).md's follow-up finding: "not security"
    is not "is humanitarian". piracy is in the default public/humanitarian
    maritime-domain allow-list, so gating drift on domains_for_mode
    ("humanitarian") -- as the first fix did -- would still let a
    piracy-domain event through. HUMANITARIAN_DRIFT_DOMAINS (SAR only) must
    not."""
    from datetime import timezone

    from core.live.feed import public_drift_collection

    now_iso = datetime.now(timezone.utc).isoformat()
    piracy_event = IntelEvent(
        id="drift-piracy",
        type="piracy_incident",
        severity="high",
        lat=35.0,
        lon=14.0,
        title="Boarding reported",
        source="SeaCommons MDA",
        timestamp_utc=now_iso,
        metadata={
            "source_policy": "official_api",
            "drift_job_id": "job-piracy",
        },
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.persisted_events", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        "core.live.feed.intel_store.events", lambda **_kwargs: [piracy_event]
    )
    fake_drift = {
        "trajectory": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[14.0, 35.0], [14.1, 35.1]]},
            "properties": {},
        },
        "cone_24h": None,
        "impact_point": {},
        "metadata": {"published": True},
    }
    monkeypatch.setattr("core.db.store.get_drift", lambda job_id: fake_drift)
    monkeypatch.setattr("core.db.store.list_drift_jobs_for_event", lambda event_id: [])
    monkeypatch.setattr("core.live.feed._is_publishable_live_drift", lambda drift: True)

    collection = public_drift_collection(limit=50)
    assert collection["features"] == []


def test_user_signal_is_private_by_default() -> None:
    signal = DistressSignal(
        source_channel="whatsapp",
        source_id="+390000000",
        raw_text="help",
        lat=35.5,
        lon=14.1,
    )
    assert signal.publication_status == "private"


def test_live_routes_remain_public_when_internal_reads_require_auth() -> None:
    previous = config.AUTH_ENABLED
    config.AUTH_ENABLED = True
    try:
        public_feed = client.get("/api/v1/live/signals")
        public_drifts = client.get("/api/v1/live/drifts")
        public_archives = client.get("/api/v1/live/archives")
        public_sources = client.get("/api/v1/live/sources")
        public_ngo = client.get("/api/v1/live/ngo-vessels")
        public_platforms = client.get("/api/v1/live/platforms")
        internal_feed = client.get("/api/v1/intel")
        internal_ngo = client.get("/api/v1/intel/ngo")
        assert public_feed.status_code == 200
        assert public_feed.json()["meta"]["schema"] == "org.seacommons.live-feed/v1"
        assert public_drifts.status_code == 200
        assert public_drifts.json()["meta"]["schema"] == "org.seacommons.live-drift/v1"
        assert public_archives.status_code == 200
        source_payload = public_sources.json()
        assert source_payload["collector"]["browser_independent"] is True
        assert all(source["type"] != "ais" for source in source_payload["sources"])
        assert all(
            {
                "pipeline_status",
                "source_status",
                "configured",
                "reachable",
                "handles",
            }.issubset(source)
            for source in source_payload["sources"]
        )
        assert public_ngo.status_code == 200
        assert public_ngo.json()["type"] == "FeatureCollection"
        assert public_platforms.status_code == 200
        assert public_platforms.json()["type"] == "FeatureCollection"
        assert internal_feed.status_code == 401
        assert internal_ngo.status_code == 401
    finally:
        config.AUTH_ENABLED = previous


def test_public_vessel_context_route_is_sanitized_and_validates_mmsi(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.api.routes.mda.build_vessel_dossier",
        lambda mmsi, hours, track_limit: {
            "mmsi": mmsi,
            "static": {"name": "ST. OLGA", "imo": "9493224"},
            "identity": {"sanctions": []},
            "track_points": [{"lon": 29.14, "lat": 41.33}],
        },
    )

    response = client.get("/api/v1/live/vessels/352001914/context?hours=24")

    assert response.status_code == 200
    assert response.json()["static"]["imo"] == "9493224"
    assert client.get("/api/v1/live/vessels/not-an-mmsi/context").status_code == 422


def test_legacy_public_mda_anomaly_route_is_removed() -> None:
    assert client.get("/api/v1/live/mda-anomalies").status_code == 404


def test_live_websocket_uses_the_requested_mode(monkeypatch) -> None:
    requested_modes: list[str] = []

    def collection(**kwargs):
        requested_modes.append(kwargs["mode"])
        return {
            "type": "FeatureCollection",
            "features": [],
            "meta": {"mode": kwargs["mode"]},
        }

    monkeypatch.setattr("core.api.routes.live.public_signal_collection", collection)
    with client.websocket_connect("/api/v1/live/stream?mode=security") as websocket:
        assert websocket.receive_json()["meta"]["mode"] == "security"

    assert requested_modes == ["security"]


def test_live_hypotheses_route_returns_the_publication_policy_projection() -> None:
    """docs/fixes.md M14.4: /api/v1/live/hypotheses is public and wired
    through core.intel.publication_policy -- exercised end to end via
    the actual route, not just the underlying function."""
    response = client.get("/api/v1/live/hypotheses")
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert "features" in payload
    assert "count" in payload["meta"]


# ── 24h operational Live surface (2026-09-05) ──────────────────────────


def test_humanitarian_live_excludes_unresolved_signal_after_24_hours(monkeypatch) -> None:
    old = IntelEvent(
        id="live24-old-active",
        timestamp_utc=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
        type="distress", severity="high", lat=34.8, lon=14.2,
        title="Distress reported 25 hours ago", text="Rescue is urgent",
        source="Alarm Phone",
        metadata={"is_distress": True, "maritime_domain": "sar", "source_policy": "operator_published"},
    )
    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: [old])
    monkeypatch.setattr("core.live.feed.intel_store.persisted_events", lambda **_kwargs: [])

    collection = public_signal_collection(limit=50, days=7, mode="humanitarian")
    assert "intel:live24-old-active" not in {
        feature["properties"]["id"] for feature in collection["features"]
    }


def test_humanitarian_live_excludes_canonical_resolved_signal_immediately(monkeypatch) -> None:
    from core.intel.humanitarian_incident import sync_incident_for_event

    event = IntelEvent(
        id="live24-resolved", timestamp_utc=datetime.now(timezone.utc).isoformat(),
        type="distress", severity="high", lat=34.8, lon=14.2,
        title="Recent distress", text="Rescue is urgent", source="Alarm Phone",
        metadata={"is_distress": True, "maritime_domain": "sar", "source_policy": "operator_published"},
    )
    sync_incident_for_event(event, lifecycle="resolved")
    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: [event])
    monkeypatch.setattr("core.live.feed.intel_store.persisted_events", lambda **_kwargs: [])

    collection = public_signal_collection(limit=50, days=1, mode="humanitarian")
    assert "intel:live24-resolved" not in {
        feature["properties"]["id"] for feature in collection["features"]
    }


def test_needs_review_drift_leaves_live_after_24h(monkeypatch) -> None:
    """Live's 24h surface boundary applies to derived Drift products too."""
    from datetime import timedelta, timezone
    from core.live.feed import public_drift_collection

    old_iso = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    event = IntelEvent(
        id="ap-needs-review-old", type="distress", severity="high",
        lat=35.0, lon=14.0, title="Older unresolved distress",
        source="alarm_phone", timestamp_utc=old_iso,
        metadata={"is_distress": True, "source_policy": "official_api",
                  "incident_lifecycle": "needs_review", "maritime_domain": "sar",
                  "coordinate_source": "media_ocr_text",
                  "coordinate_review_status": "machine_ocr_unverified",
                  "location_status": "positioned",
                  "thread_reposts": [{
                      "tweet_id": "later-ambiguous-update",
                      "posted_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
                      "kind": "reply",
                      "note": "Still no rescue came.",
                  }]},
    )
    monkeypatch.setattr("core.live.feed.intel_store.persisted_events", lambda **_kwargs: [])
    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: [event])
    fake = {"trajectory": {"type": "Feature", "geometry": {"type": "LineString",
            "coordinates": [[14.0, 35.0], [14.1, 35.1]]}, "properties": {}},
            "cone_24h": None, "impact_point": {}, "metadata": {"published": True}}
    monkeypatch.setattr("core.db.store.get_drift", lambda _job_id: fake)
    monkeypatch.setattr("core.live.feed._is_publishable_live_drift", lambda _drift: True)
    from core.db.models import HumanitarianIncidentDB, IncidentTransitionDB
    from core.db.session import engine
    from core.intel.drift_ownership import sync_current_drift_for_incident
    from core.intel.humanitarian_incident import sync_incident_for_event

    HumanitarianIncidentDB.__table__.create(bind=engine(), checkfirst=True)
    IncidentTransitionDB.__table__.create(bind=engine(), checkfirst=True)
    sync_incident_for_event(event, lifecycle="needs_review")
    assert sync_current_drift_for_incident(event.id, "job-old-needs-review") == "job-old-needs-review"
    from core.intel.drift_service import is_auto_drift_eligible
    assert is_auto_drift_eligible(event)[0] is True

    collection = public_drift_collection(limit=50)
    ids = {f["properties"]["intel_event_id"] for f in collection["features"]}
    assert event.id not in ids


def test_mode_all_never_caps_humanitarian_even_when_it_exceeds_transport_limit(monkeypatch) -> None:
    now = datetime.now(timezone.utc).isoformat()
    events = [
        IntelEvent(
            id=f"always-human-{i}", timestamp_utc=now, type="distress", severity="high",
            lat=34.0 + i * 0.01, lon=14.0, title="Distress", source="Alarm Phone",
            metadata={"is_distress": True, "maritime_domain": "sar", "source_policy": "official_site_embed"},
        ) for i in range(3)
    ]
    monkeypatch.setattr("core.live.feed.intel_store.events", lambda **_kwargs: events)
    monkeypatch.setattr("core.live.feed.intel_store.persisted_events", lambda **_kwargs: [])
    monkeypatch.setattr("core.live.feed._published_ingested_features", lambda _limit: [])
    collection = public_signal_collection(limit=2, days=1, mode="all")
    assert len(collection["features"]) == 3
    assert collection["meta"]["total"] == 3


def test_internal_single_lineage_correlated_alert_is_not_public_in_security_mode() -> None:
    event = IntelEvent(
        id="internal-multi-indicator",
        type="correlated_alert",
        severity="high",
        lat=35.9,
        lon=14.5,
        title="AIS gap + infrastructure proximity",
        source="SeaCommons fusion",
        linked_mmsi="229113000",
        metadata={
            "maritime_domain": "grey_zone",
            "alert_type": "infra_proximity",
            "publication_status": "internal",
            "verification_status": "single_source_multi_indicator",
            "contributing_independence_groups": ["ais_sensor_lineage"],
            "independent_source_count": 1,
            "evidence_count": 2,
        },
    )

    feature = _public_intel_feature(event, allowed_domains=frozenset({"grey_zone"}))

    assert feature is None


def test_security_correlated_alert_without_explicit_publication_is_not_public() -> None:
    for verification in ("single_source_observed", "multi_source_corroborated"):
        event = IntelEvent(
            id=f"legacy-security-{verification}",
            type="correlated_alert",
            severity="high",
            lat=35.9,
            lon=14.5,
            title="Historical security fusion alert",
            source="SeaCommons fusion",
            linked_mmsi="229113000",
            metadata={
                "maritime_domain": "sanctions",
                "alert_type": "sdn_match",
                "verification_status": verification,
            },
        )

        assert _public_intel_feature(
            event, allowed_domains=frozenset({"sanctions", "grey_zone"})
        ) is None


def test_public_correlated_alert_exposes_evidence_lineage_summary() -> None:
    event = IntelEvent(
        id="public-corroborated-lineage",
        type="correlated_alert",
        severity="high",
        lat=35.9,
        lon=14.5,
        title="Independently corroborated maritime episode",
        source="SeaCommons fusion",
        linked_mmsi="229113000",
        metadata={
            "maritime_domain": "grey_zone",
            "alert_type": "infrastructure_threat",
            "publication_status": "published",
            "verification_status": "multi_source_corroborated",
            "contributing_independence_groups": ["ais_sensor_lineage", "official_report"],
            "independent_source_count": 2,
            "evidence_count": 3,
            "verification_explanation": "3 evidence items across 2 independent evidence lineages",
        },
    )

    feature = _public_intel_feature(event, allowed_domains=frozenset({"grey_zone"}))
    # Correlated alerts remain evidence; public Maritime Intelligence is
    # emitted only from a published InvestigationHypothesis.
    assert feature is None


def test_maritime_safety_projection_exposes_operational_label_and_input_modality() -> None:
    cases = [
        ("aground", "Aground"),
        ("not_under_command", "Not Under Command"),
        ("restricted_manoeuvrability", "Restricted Manoeuvrability"),
    ]
    for nav_kind, expected_label in cases:
        event = IntelEvent(
            id=f"safety-label-{nav_kind}", type="vessel_incident", severity="medium",
            lat=35.2, lon=14.0, title=expected_label, source="ais", linked_mmsi="209888000",
            metadata={
                "ais_nav_status_kind": nav_kind, "maritime_domain": "safety",
                "publication_status": "published", "source_policy": "official_api",
            },
        )
        feature = _public_intel_feature(event, allowed_domains=frozenset({"safety"}))
        assert feature is not None
        assert feature["properties"]["operational_label"] == expected_label
        assert feature["properties"]["input_modality"] == "ais"
        assert feature["properties"]["source"] == "ais"


def test_dsc_safety_projection_is_radio_modality() -> None:
    event = IntelEvent(
        id="safety-label-dsc", type="dsc_distress", severity="critical",
        lat=35.2, lon=14.0, title="DSC distress received", source="radio_receiver:med_rx",
        metadata={
            "dsc_category": "distress", "receiver_id": "med_rx",
            "physical_lineage": "rx_med_1", "frequency_hz": 2187500,
            "maritime_domain": "safety", "publication_status": "published",
            "source_policy": "operator_published",
        },
    )
    feature = _public_intel_feature(event, allowed_domains=frozenset({"safety"}))
    assert feature is not None
    assert feature["properties"]["operational_label"] == "DSC distress"
    assert feature["properties"]["input_modality"] == "radio"


def test_public_signal_collection_excludes_unverified_ais_navigation_casualty_but_keeps_beacon() -> None:
    from core.intel.store import intel_store
    now = datetime.now(timezone.utc).isoformat()
    casualty = IntelEvent(
        id="audit-ais-aground", type="vessel_incident", severity="medium",
        lat=35.2, lon=14.0, title="Vessel ran aground — AUDIT", source="ais",
        linked_mmsi="209888000", timestamp_utc=now,
        metadata={
            "ais_nav_status_kind": "aground", "maritime_domain": "safety",
            "publication_status": "published", "source_policy": "official_api",
            "verification_status": "ais_transponder",
        },
    )
    beacon = IntelEvent(
        id="audit-ais-sart", type="distress", severity="high",
        lat=35.3, lon=14.1, title="Distress beacon activated", source="ais_sart",
        timestamp_utc=now, metadata={
            "maritime_domain": "safety", "publication_status": "published",
            "source_policy": "official_api", "verification_status": "ais_transponder",
        },
    )
    intel_store.add(casualty, dedup_key=casualty.id)
    intel_store.add(beacon, dedup_key=beacon.id)
    collection = public_signal_collection(mode="maritime", days=1, limit=500)
    ids = {f["properties"]["id"] for f in collection["features"]}
    assert "intel:audit-ais-aground" not in ids
    assert "intel:audit-ais-sart" in ids


def test_public_signal_collection_collapses_near_simultaneous_alarm_phone_translation() -> None:
    from core.intel.store import intel_store
    base = datetime.now(timezone.utc)
    for event_id, seconds, title in (
        ("audit-ap-en", 0, "27 people left Boumerdes and are missing"),
        ("audit-ap-fr", 33, "27 personnes parties de Boumerdes sont portees disparues"),
    ):
        event = IntelEvent(
            id=event_id, type="twitter", severity="high", lat=36.79492, lon=3.5,
            title=title, source="alarm_phone", timestamp_utc=(base + timedelta(seconds=seconds)).isoformat(),
            metadata={
                "is_distress": True, "maritime_domain": "sar",
                "publication_status": "published", "source_policy": "operator_published",
            },
        )
        intel_store.add(event, dedup_key=event.id)
    collection = public_signal_collection(mode="humanitarian", days=1, limit=500)
    audit = [f for f in collection["features"] if f["properties"]["id"] in {"intel:audit-ap-en", "intel:audit-ap-fr"}]
    assert [f["properties"]["id"] for f in audit] == ["intel:audit-ap-en"]


def test_sosmed_mayday_report_with_completed_interception_is_not_live_distress() -> None:
    text = (
        "Yesterday, following a Mayday relay, the #OceanViking altered course "
        "towards a rubber boat carrying around 80 people in distress. As our "
        "team approached, we witnessed the Libyan Coast Guard intercept the "
        "boat and return those on board to Libya."
    )
    assert is_direct_distress_call(text) is False
    assert lifecycle.is_concluded_incident(text) is True


def test_concluded_sosmed_report_is_not_reopened_by_ambiguous_self_reply() -> None:
    text = (
        "Yesterday, following a Mayday relay, the #OceanViking altered course "
        "towards a rubber boat carrying around 80 people in distress. As our "
        "team approached, we witnessed the Libyan Coast Guard intercept the "
        "boat and return those on board to Libya."
    )
    event = IntelEvent(
        id="sosmed-interception-regression",
        type="twitter",
        severity="critical",
        title=text[:100],
        text=text,
        source="SOSMedIntl",
        timestamp_utc="2026-09-18T09:46:44+00:00",
        metadata={
            "is_distress": True,
            "thread_reposts": [{
                "posted_at": "2026-09-18T09:46:47+00:00",
                "kind": "reply",
                "note": (
                    "We were only minutes away. Those few minutes marked the "
                    "difference between rescue and safety, and a forced return "
                    "to a place where people face detention, violence and abuse. "
                    "The #OceanViking is sailing towards its assigned Place of Safety."
                ),
            }],
        },
    )
    state = lifecycle.distress_lifecycle(
        event,
        now=datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc),
        same_source=[],
    )
    assert state == "resolved"
    assert lifecycle.is_directly_concluded(event) is True

def test_normalized_sar_responder_activity_is_observation_not_case(monkeypatch) -> None:
    event = IntelEvent(
        id="saractivity:test",
        type="ngo_activity",
        severity="medium",
        lat=35.5,
        lon=12.6,
        title="SAR responder search-pattern activity — Ocean Viking",
        source="SeaCommons AIS analysis",
        linked_mmsi="258479000",
        metadata={
            "maritime_domain": "sar",
            "publication_status": "published",
            "source_policy": "official_api",
            "verification_status": "single_source_observed",
            "coordinate_source": "ais_position",
            "evidence_stage": "derived",
            "source_lineage": "ais_sensor_lineage",
            "independent_source_count": 1,
            "evidence_count": 1,
            "activity_kind": "search_pattern_observed",
            "observation_type": "sar_responder_activity",
            "operator_type": "civil_ngo",
            "org": "SOS Méditerranée",
            "vessel_role": "SAR",
            "vessel_name": "Ocean Viking",
            "public_summary": "AIS track shows a search pattern; no rescue is confirmed from AIS alone.",
        },
    )
    monkeypatch.setattr(intel_store, "events", lambda **_kwargs: [event])
    monkeypatch.setattr(intel_store, "persisted_events", lambda **_kwargs: [])
    collection = public_signal_collection(limit=50, mode="humanitarian")
    assert collection["meta"]["total"] == 1
    assert collection["meta"]["role_counts"]["humanitarian_observation"] == 1
    props = collection["features"][0]["properties"]
    assert props["live_role"] == "humanitarian_observation"
    assert props["evidence_stage"] == "derived"
    assert props["verification_status"] == "single_source_observed"
    assert props["visual_category"] == "civil_sar"
    assert "linked_mmsi" not in props
    assert "mmsi" not in props
    assert "vessel_name" not in props

def test_stale_sar_responder_activity_expires_before_24h_live_window(monkeypatch) -> None:
    stale = IntelEvent(
        id="saractivity:stale",
        timestamp_utc=(datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),
        type="ngo_activity",
        severity="medium",
        lat=37.5,
        lon=12.0,
        title="SAR responder multi-vessel convergence — Ocean Viking",
        source="SeaCommons AIS analysis",
        linked_mmsi="258479000",
        metadata={
            "maritime_domain": "sar",
            "publication_status": "published",
            "source_policy": "official_api",
            "verification_status": "single_source_observed",
            "coordinate_source": "ais_position",
            "evidence_stage": "derived",
            "source_lineage": "ais_sensor_lineage",
            "independent_source_count": 1,
            "activity_kind": "responder_convergence_observed",
            "observation_type": "sar_responder_activity",
            "operator_type": "civil_ngo",
        },
    )
    monkeypatch.setattr(intel_store, "events", lambda **_kwargs: [stale])
    monkeypatch.setattr(intel_store, "persisted_events", lambda **_kwargs: [])
    collection = public_signal_collection(limit=50, mode="humanitarian")
    assert collection["features"] == []
    assert collection["meta"]["total"] == 0
    assert collection["meta"]["role_counts"]["humanitarian_observation"] == 0
