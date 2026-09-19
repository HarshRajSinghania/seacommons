from __future__ import annotations

from datetime import datetime

from core.intel import lifecycle
from core.intel.store import IntelEvent


def _now() -> datetime:
    return datetime.fromisoformat("2026-08-06T10:00:00+00:00")


def test_alarm_phone_self_reply_resolution_leaves_live_immediately() -> None:
    event = IntelEvent(
        id="alarm-phone-52",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text="52 people in grave distress south of Lampedusa.",
        source="Alarm Phone",
        timestamp_utc="2026-08-06T08:00:00+00:00",
        metadata={
            "thread_reposts": [
                {
                    "tweet_id": "resolution-1",
                    "posted_at": "2026-08-06T09:00:00+00:00",
                    "url": "https://x.com/i/web/status/resolution-1",
                    "kind": "reply",
                    "note": "Rescued to Lampedusa! Everyone arrived safely.",
                }
            ]
        },
    )

    assert lifecycle.distress_lifecycle(event, now=_now(), same_source=[]) == "resolved"
    assert lifecycle.is_within_live_window(event, now=_now()) is False
    assert lifecycle.is_within_live_retention_window(event, now=_now()) is True


def _ap_case(text: str, *, reposts: list | None = None) -> IntelEvent:
    return IntelEvent(
        id="ap-lifecycle",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text=text,
        source="Alarm Phone",
        timestamp_utc="2026-08-06T08:00:00+00:00",
        metadata={"is_distress": True, **({"thread_reposts": reposts} if reposts else {})},
    )


def test_reception_centre_followup_resolves_the_incident() -> None:
    """docs/fixes.md Phase 3.3: "the people have been found and taken to a
    reception centre" resolves the parent, not a second active marker."""
    event = _ap_case(
        "40 people missing in the Aegean",
        reposts=[{
            "tweet_id": "r1", "posted_at": "2026-08-06T09:30:00+00:00", "kind": "reply",
            "note": "The people have been found and taken to a reception centre on Lesvos.",
        }],
    )
    assert lifecycle.distress_lifecycle(event, now=_now(), same_source=[]) == "resolved"


def test_a_newer_danger_reply_reopens_a_resolved_case() -> None:
    event = _ap_case(
        "30 people adrift off Libya",
        reposts=[
            {"tweet_id": "r1", "posted_at": "2026-08-06T08:30:00+00:00", "kind": "reply",
             "note": "The people were rescued."},
            {"tweet_id": "r2", "posted_at": "2026-08-06T09:15:00+00:00", "kind": "reply",
             "note": "Update: the boat is still in distress and taking on water!"},
        ],
    )
    assert lifecycle.distress_lifecycle(event, now=_now(), same_source=[]) == "active"


def test_ambiguous_followup_requests_review_not_silent_resolution() -> None:
    event = _ap_case(
        "25 people in distress near Crete",
        reposts=[{
            "tweet_id": "r1", "posted_at": "2026-08-06T09:00:00+00:00", "kind": "reply",
            "note": "We are trying to reach the authorities about this case.",
        }],
    )
    assert lifecycle.distress_lifecycle(event, now=_now(), same_source=[]) == "needs_review"


def test_concluded_report_is_not_active_but_is_retained_in_live_timeline() -> None:
    event = IntelEvent(
        id="alarm-phone-resolved-post",
        type="twitter",
        severity="low",
        title="Alarm Phone",
        text="The 38 people were rescued and are now safe in port.",
        source="Alarm Phone",
        timestamp_utc="2026-08-06T09:30:00+00:00",
    )

    assert lifecycle.is_within_live_window(event, now=_now()) is False
    assert lifecycle.is_within_live_retention_window(event, now=_now()) is True


def test_unresolved_recent_report_remains_live() -> None:
    event = IntelEvent(
        id="alarm-phone-active",
        type="twitter",
        severity="high",
        title="Alarm Phone",
        text="38 people in distress without fuel. Rescue is urgently needed.",
        source="Alarm Phone",
        timestamp_utc="2026-08-06T09:30:00+00:00",
    )

    assert lifecycle.distress_lifecycle(event, now=_now(), same_source=[]) == "active"
    assert lifecycle.is_within_live_window(event, now=_now()) is True
