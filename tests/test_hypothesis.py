# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md M6: InvestigationHypothesis lifecycle engine.

Exit gate, verbatim: "no single raw AIS observation can create a
published Intelligence allegation."
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.intel.hypothesis import (
    can_publish,
    covert_rendezvous_gate,
    dark_transit_gate,
    evaluate_gate,
    identity_deception_gate,
    infrastructure_pattern_gate,
    new_hypothesis,
    position_spoofing_gate,
    sanctions_evasion_pattern_gate,
    transition,
)


def _ready_to_publish():
    """A hypothesis that satisfies every can_publish() condition, for
    tests that need to isolate one specific failing condition."""
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "collecting", actor="system")
    h = transition(h, "review_ready", actor="system")
    h = replace(
        h,
        reason_codes=("isolated_gap", "coverage_confidence_sufficient"),
        evidence_links=("obs:aaa", "obs:bbb"),
        evidence_stage="corroborated",
    )
    return transition(h, "assessed", actor="system")


# ── lifecycle state machine ─────────────────────────────────────────────

def test_new_hypothesis_starts_in_candidate_state():
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    assert h.state == "candidate"
    assert h.audit_history == ()


def test_new_hypothesis_rejects_an_unknown_type():
    with pytest.raises(ValueError):
        new_hypothesis("hyp-1", "not_a_real_gate", ())


def test_exit_gate_candidate_cannot_skip_straight_to_published():
    """No single raw observation can create a published allegation --
    part one: the state machine itself has no shortcut."""
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    with pytest.raises(ValueError):
        transition(h, "published", actor="system")


def test_exit_gate_assessed_without_evidence_links_cannot_publish():
    """Part two: even once a hypothesis legitimately reaches 'assessed',
    can_publish() itself still requires plural evidence_links -- a
    single-observation hypothesis can never satisfy it."""
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "collecting", actor="system")
    h = transition(h, "review_ready", actor="system")
    h = replace(h, reason_codes=("isolated_gap",), evidence_stage="corroborated")
    h = transition(h, "assessed", actor="system")
    ok, reason = can_publish(h)
    assert ok is False
    assert "evidence_links" in reason
    with pytest.raises(ValueError):
        transition(h, "published", actor="system")


def test_exit_gate_single_evidence_link_cannot_publish():
    h = new_hypothesis("hyp-single", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "collecting", actor="system")
    h = transition(h, "review_ready", actor="system")
    h = replace(
        h, reason_codes=("isolated_gap",), evidence_links=("obs:only",),
        evidence_stage="corroborated",
    )
    h = transition(h, "assessed", actor="system")
    ok, reason = can_publish(h)
    assert ok is False
    assert "at least two" in reason


def test_exit_gate_duplicate_evidence_link_does_not_count_twice():
    h = new_hypothesis("hyp-duplicate", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "collecting", actor="system")
    h = transition(h, "review_ready", actor="system")
    h = replace(
        h, reason_codes=("isolated_gap",), evidence_links=("obs:same", "obs:same"),
        evidence_stage="corroborated",
    )
    h = transition(h, "assessed", actor="system")
    assert can_publish(h)[0] is False


def test_a_fully_evidenced_hypothesis_can_reach_published():
    h = _ready_to_publish()
    ok, _ = can_publish(h)
    assert ok is True
    published = transition(h, "published", actor="reviewer")
    assert published.state == "published"


def test_every_transition_appends_an_audit_entry_never_rewrites():
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "collecting", actor="alice")
    h = transition(h, "review_ready", actor="bob")
    assert len(h.audit_history) == 2
    assert h.audit_history[0].actor == "alice"
    assert h.audit_history[0].old_state == "candidate"
    assert h.audit_history[0].new_state == "collecting"
    assert h.audit_history[1].actor == "bob"
    assert h.audit_history[1].old_state == "collecting"
    assert h.audit_history[1].new_state == "review_ready"
    assert h.audit_history[0].evidence_snapshot_hash


def test_rejected_and_expired_are_terminal():
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "rejected", actor="system")
    with pytest.raises(ValueError):
        transition(h, "collecting", actor="system")


def test_review_ready_can_fall_back_to_collecting():
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    h = transition(h, "collecting", actor="system")
    h = transition(h, "review_ready", actor="system")
    h = transition(h, "collecting", actor="system")  # new counter-evidence reopens it
    assert h.state == "collecting"


# ── publication gate ────────────────────────────────────────────────────

def test_can_publish_requires_assessed_or_published_state():
    h = new_hypothesis("hyp-1", "dark_transit", ("subj:mmsi:111000111",))
    ok, reason = can_publish(h)
    assert ok is False
    assert "state" in reason


def test_can_publish_requires_a_publishable_evidence_stage():
    h = _ready_to_publish()
    h = replace(h, evidence_stage="derived")
    ok, reason = can_publish(h)
    assert ok is False
    assert "evidence_stage" in reason


def test_can_publish_blocks_on_an_unresolved_identity_conflict():
    h = _ready_to_publish()
    h = replace(h, has_unresolved_blocking_identity_conflict=True)
    ok, reason = can_publish(h)
    assert ok is False
    assert "identity conflict" in reason


def test_allegation_shaped_wording_requires_explicit_review():
    h = _ready_to_publish()
    h = replace(h, allegation_shaped_wording=True, explicit_review_done=False)
    ok, reason = can_publish(h)
    assert ok is False
    assert "explicit review" in reason

    reviewed = replace(h, explicit_review_done=True)
    ok, _ = can_publish(reviewed)
    assert ok is True


# ── evidence gates: every one needs 2+ independent pieces ──────────────

def test_dark_transit_gate_needs_gap_and_sufficient_coverage():
    assert dark_transit_gate(has_isolated_gap_feature=False, coverage_confidence=0.9)[0] is False
    assert dark_transit_gate(has_isolated_gap_feature=True, coverage_confidence=0.1)[0] is False
    assert dark_transit_gate(has_isolated_gap_feature=True, coverage_confidence=0.9)[0] is True


def test_covert_rendezvous_gate_needs_independent_irregularity():
    assert covert_rendezvous_gate(
        has_sustained_rendezvous_episode=True, independent_irregularities=(),
    )[0] is False
    assert covert_rendezvous_gate(
        has_sustained_rendezvous_episode=True, independent_irregularities=("identity_conflict",),
    )[0] is True


def test_identity_deception_gate_needs_two_sources_not_just_two_timestamps():
    same_source = ({"source": "aisstream"}, {"source": "aisstream"})
    assert identity_deception_gate(contradictory_observations=same_source)[0] is False
    two_sources = ({"source": "aisstream"}, {"source": "sanctions_list"})
    assert identity_deception_gate(contradictory_observations=two_sources)[0] is True


def test_identity_deception_gate_a_single_observation_is_never_enough():
    """A duplicate MMSI alone is a candidate integrity problem until
    contextualized -- one observation can never pass this gate."""
    assert identity_deception_gate(contradictory_observations=({"source": "aisstream"},))[0] is False


def test_position_spoofing_gate_needs_reproducibility_and_counter_check():
    assert position_spoofing_gate(
        implausible_movement=True, reproducible_inputs=False, counter_evidence_checked=True,
    )[0] is False
    assert position_spoofing_gate(
        implausible_movement=True, reproducible_inputs=True, counter_evidence_checked=False,
    )[0] is False
    assert position_spoofing_gate(
        implausible_movement=True, reproducible_inputs=True, counter_evidence_checked=True,
    )[0] is True


def test_sanctions_evasion_pattern_gate_a_list_match_alone_is_not_enough():
    """docs/fixes.md: a sanctions list match alone publishes only the
    official-list fact, never 'evasion'."""
    assert sanctions_evasion_pattern_gate(
        official_list_match=True, behavioural_evidence=(),
    )[0] is False
    assert sanctions_evasion_pattern_gate(
        official_list_match=True, behavioural_evidence=("unusual_route_to_sanctioned_port",),
    )[0] is True


def test_infrastructure_pattern_gate_proximity_alone_is_not_a_pattern():
    assert infrastructure_pattern_gate(
        dwell_or_route_repetition=False, independent_corroboration=("other_anomaly",),
    )[0] is False
    assert infrastructure_pattern_gate(
        dwell_or_route_repetition=True, independent_corroboration=(),
    )[0] is False
    assert infrastructure_pattern_gate(
        dwell_or_route_repetition=True, independent_corroboration=("other_anomaly",),
    )[0] is True


def test_evaluate_gate_dispatches_by_hypothesis_type():
    ok, _ = evaluate_gate(
        "dark_transit", has_isolated_gap_feature=True, coverage_confidence=0.9,
    )
    assert ok is True


def test_evaluate_gate_raises_for_an_unknown_type():
    with pytest.raises(KeyError):
        evaluate_gate("not_a_real_gate")
