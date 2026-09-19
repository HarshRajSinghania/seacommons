# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md M5.2: bounded episode builder.

Exit gate, verbatim: "two unrelated anomalies on the same MMSI days apart
become two episodes; repeated updates of one continuing event remain one
episode."
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.live.episode_builder import (
    DEFAULT_MAX_GAP_S,
    EpisodeSignal,
    build_episodes,
    family_for,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _sig(signal_id, *, subject="subj:mmsi:211879870", family="gap_episode",
         hours_ago=0, lat=35.5, lon=14.1, resolved=False):
    return EpisodeSignal(
        signal_id=signal_id,
        subject_ids=(subject,) if isinstance(subject, str) else tuple(subject),
        family=family,
        observed_at=_NOW - timedelta(hours=hours_ago),
        lat=lat, lon=lon, resolved=resolved,
    )


def test_family_for_maps_known_anomaly_types():
    assert family_for("gap") == "gap_episode"
    assert family_for("sdn_match") == "identity_integrity_episode"
    assert family_for("spoofing_candidate") == "spoofing_episode"
    assert family_for("vessel_loiter") == "infrastructure_proximity_episode"


def test_family_for_unknown_type_fails_closed_to_unclassified_episode():
    """Observation→Episode→Hypothesis v1 removes the legacy Safety catch-all.
    Unknown intelligence semantics must stay unclassified, not become Safety.
    """
    assert family_for("something_never_seen_before") == "unclassified_episode"


def test_family_for_maps_the_real_live_emitter_vocabulary():
    """docs/fixes.md M14.2: the anomaly_type strings core.mda.watch and the
    other live emitters actually produce -- not just the fixture-shaped
    ones above -- must route to their real family, not fall through to the
    unclassified_episode fail-closed fallback."""
    assert family_for("ais_rendezvous") == "rendezvous_episode"  # watch._emit_rendezvous
    assert family_for("loiter") == "infrastructure_proximity_episode"
    assert family_for("cable_proximity") == "infrastructure_proximity_episode"
    assert family_for("sanctions_bunkering_loiter") == "infrastructure_proximity_episode"
    assert family_for("position_jump") == "spoofing_episode"  # scan_spoofing "teleport"
    # Low-specificity circular/static AIS integrity cues stay as evidence;
    # they do not create a spoofing episode without a stronger independent cue.
    assert family_for("circular_pattern") == "unclassified_episode"
    assert family_for("static_position_inconsistency") == "unclassified_episode"
    assert family_for("impossible_speed") == "spoofing_episode"  # core.anomaly.ais
    assert family_for("dark_zone_entry") == "spoofing_episode"  # core.anomaly.ais
    assert family_for("dark_candidate") == "gap_episode"  # core.intel.viirs_monitor


def test_exit_gate_repeated_updates_of_one_continuing_event_remain_one_episode():
    signals = [
        _sig("s1", family="gap_episode", hours_ago=5),
        _sig("s2", family="gap_episode", hours_ago=3),
        _sig("s3", family="gap_episode", hours_ago=1),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 1
    assert episodes[0].signal_ids == ["s1", "s2", "s3"]


def test_exit_gate_two_unrelated_anomalies_days_apart_become_two_episodes():
    signals = [
        _sig("s1", family="gap_episode", hours_ago=200),  # >3 days ago
        _sig("s2", family="gap_episode", hours_ago=1),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 2
    assert episodes[0].signal_ids == ["s1"]
    assert episodes[1].signal_ids == ["s2"]


def test_signals_do_not_need_to_be_pre_sorted():
    signals = [
        _sig("later", family="gap_episode", hours_ago=1),
        _sig("earlier", family="gap_episode", hours_ago=5),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 1
    assert episodes[0].signal_ids == ["earlier", "later"]


def test_different_families_never_share_an_episode():
    signals = [
        _sig("s1", family="gap_episode", hours_ago=1),
        _sig("s2", family="identity_integrity_episode", hours_ago=1),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 2
    assert {e.family for e in episodes} == {"gap_episode", "identity_integrity_episode"}


def test_a_resolved_signal_closes_the_episode_even_for_a_fast_reappearance():
    """docs/fixes.md M5.2: 'explicit resolution/reappearance' is its own
    boundary rule -- a resolved incident reappearing minutes later is a
    new event, not a continuation of the one that just closed."""
    signals = [
        _sig("s1", family="gap_episode", hours_ago=2, resolved=True),
        _sig("s2", family="gap_episode", hours_ago=1),  # same family, 1h later
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 2
    assert episodes[0].resolved is True
    assert episodes[0].signal_ids == ["s1"]
    assert episodes[1].signal_ids == ["s2"]


def test_spatial_discontinuity_splits_an_episode_even_within_the_time_window():
    signals = [
        _sig("s1", family="gap_episode", hours_ago=2, lat=35.5, lon=14.1),
        _sig("s2", family="gap_episode", hours_ago=1, lat=41.9, lon=12.5),  # ~450nm away
    ]
    episodes = build_episodes(signals, max_spatial_nm=20.0)
    assert len(episodes) == 2


def test_missing_position_never_causes_a_spatial_split():
    signals = [
        _sig("s1", family="gap_episode", hours_ago=2, lat=None, lon=None),
        _sig("s2", family="gap_episode", hours_ago=1, lat=35.5, lon=14.1),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 1


def test_different_subjects_never_share_an_episode():
    signals = [
        _sig("s1", subject="subj:mmsi:111000111", family="gap_episode", hours_ago=1),
        _sig("s2", subject="subj:mmsi:222000222", family="gap_episode", hours_ago=1),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 2
    assert {e.subject_ids for e in episodes} == {
        ("subj:mmsi:111000111",), ("subj:mmsi:222000222",),
    }


def test_a_rendezvous_signal_can_involve_two_or_more_subjects():
    """docs/fixes.md M5.2: 'an episode can involve two or more subjects
    for encounters.'"""
    signals = [
        _sig(
            "s1", subject=["subj:mmsi:111000111", "subj:mmsi:222000222"],
            family="rendezvous_episode", hours_ago=1,
        ),
    ]
    episodes = build_episodes(signals)
    assert len(episodes) == 1
    assert episodes[0].subject_ids == ("subj:mmsi:111000111", "subj:mmsi:222000222")


def test_max_gap_boundary_is_configurable():
    signals = [
        _sig("s1", family="gap_episode", hours_ago=10),
        _sig("s2", family="gap_episode", hours_ago=1),
    ]
    # Default (3 days) keeps these one episode; a tight custom gap splits them.
    assert len(build_episodes(signals)) == 1
    assert len(build_episodes(signals, max_gap_s=3600)) == 2


def test_default_max_gap_is_three_days():
    assert DEFAULT_MAX_GAP_S == 3 * 24 * 3600


def test_unknown_anomaly_is_unclassified_not_safety() -> None:
    from core.live.episode_builder import family_for

    assert family_for("mystery_signal") == "unclassified_episode"


def test_explicit_not_under_command_remains_safety() -> None:
    from core.live.episode_builder import family_for

    assert family_for("not_under_command") == "safety_episode"
