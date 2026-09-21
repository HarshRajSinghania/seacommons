# SPDX-License-Identifier: AGPL-3.0-or-later
"""InvestigationHypothesis lifecycle engine (docs/fixes.md M6).

**Goal:** make Maritime Intelligence a real investigative workflow instead
of a detector feed. A hypothesis stores reason codes and counter-indicators,
not a black-box risk number, and moves through an explicit lifecycle:

    candidate -> collecting -> review_ready -> assessed -> published
              -> rejected
              -> expired

Every transition is recorded in ``audit_history`` (actor, timestamp,
old/new state, a hash of the evidence snapshot at that moment) -- never a
silent state change.

Exit gate: "no single raw AIS observation can create a published
Intelligence allegation." Enforced structurally, not by convention: the
state machine requires passing through collecting and review_ready before
assessed (no shortcut transitions), ``can_publish()`` requires
non-empty ``reason_codes`` AND non-empty ``evidence_links`` (plural), and
every one of the six gate functions below independently demands at least
two distinct pieces of corroborating evidence before a hypothesis is even
eligible to progress -- matching the spec's own wording for every gate
("X requires Y *plus* independent Z").

This module is pure and standalone: it holds no reference to
core.intel.store/IntelEventDB, core.mda.vessel_subject, or any live
detector. A caller assembles the gate-function inputs from whatever
evidence sources it has (SourceObservation, VesselSubject, episode
builder output, ...). core.intel.hypothesis_engine (docs/fixes.md M14.3)
is that caller: it wires four of the six gates above into the live
episode pipeline (core.mda.watch.scan_hypotheses -> core.live.
vessel_episodes -> this module), persisted via core.intel.hypothesis_store.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional

HypothesisType = str  # one of _GATE_NAMES below
HypothesisState = str  # one of _VALID_TRANSITIONS keys

_GATE_NAMES = frozenset(
    {
        "dark_transit", "covert_rendezvous", "identity_deception",
        "position_spoofing", "sanctions_evasion_pattern", "infrastructure_pattern",
    }
)

# docs/fixes.md M6 lifecycle graph. No transition skips a stage (e.g.
# candidate -> assessed is not listed) -- that is the exit gate's
# structural half. review_ready/assessed can also fall back to
# collecting (new counter-evidence reopens collection), which is not a
# "shortcut" since it moves backward, not toward publication.
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"collecting", "rejected", "expired"}),
    "collecting": frozenset({"review_ready", "rejected", "expired"}),
    "review_ready": frozenset({"assessed", "collecting", "rejected", "expired"}),
    "assessed": frozenset({"published", "collecting", "rejected", "expired"}),
    "published": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}

_PUBLISHABLE_STATES = frozenset({"assessed", "published"})
_PUBLISHABLE_EVIDENCE_STAGES = frozenset({"corroborated", "assessed", "confirmed"})


@dataclass(frozen=True)
class AuditEntry:
    actor: str
    timestamp: datetime
    old_state: Optional[str]
    new_state: str
    evidence_snapshot_hash: str


@dataclass(frozen=True)
class InvestigationHypothesis:
    hypothesis_id: str
    hypothesis_type: HypothesisType
    subject_ids: tuple[str, ...]
    episode_id: Optional[str] = None
    state: HypothesisState = "candidate"
    reason_codes: tuple[str, ...] = ()
    counter_indicators: tuple[str, ...] = ()
    evidence_links: tuple[str, ...] = ()
    evidence_stage: str = "observed"  # same ladder as docs/fixes.md's
    # canonical evidence stages: observed|derived|corroborated|assessed|confirmed
    has_unresolved_blocking_identity_conflict: bool = False
    allegation_shaped_wording: bool = False
    explicit_review_done: bool = False
    audit_history: tuple[AuditEntry, ...] = field(default_factory=tuple)
    # Durable Live retention ceiling (core.live.retention), populated only
    # while state == "published" -- see core.intel.hypothesis_store.
    # save_hypothesis(), the sole writer. A published hypothesis is
    # otherwise terminal with no expiry transition, so without this a
    # published Maritime Intelligence case could stay on Live indefinitely.
    live_entered_at: Optional[datetime] = None
    last_qualified_observation_at: Optional[datetime] = None
    live_expires_at: Optional[datetime] = None


def _evidence_snapshot_hash(hypothesis: InvestigationHypothesis) -> str:
    snapshot = {
        "episode_id": hypothesis.episode_id,
        "reason_codes": sorted(hypothesis.reason_codes),
        "counter_indicators": sorted(hypothesis.counter_indicators),
        "evidence_links": sorted(hypothesis.evidence_links),
        "evidence_stage": hypothesis.evidence_stage,
    }
    payload = json.dumps(snapshot, sort_keys=True).encode()
    return hashlib.blake2s(payload, digest_size=16).hexdigest()


def new_hypothesis(
    hypothesis_id: str, hypothesis_type: HypothesisType, subject_ids: tuple[str, ...],
    *, episode_id: Optional[str] = None,
) -> InvestigationHypothesis:
    if hypothesis_type not in _GATE_NAMES:
        raise ValueError(f"unknown hypothesis_type: {hypothesis_type!r}")
    return InvestigationHypothesis(
        hypothesis_id=hypothesis_id, hypothesis_type=hypothesis_type, subject_ids=subject_ids,
        episode_id=episode_id,
    )


def can_publish(hypothesis: InvestigationHypothesis) -> tuple[bool, str]:
    """docs/fixes.md M6 publication gate, applied verbatim."""
    if hypothesis.state not in _PUBLISHABLE_STATES:
        return False, f"state={hypothesis.state!r} not in {sorted(_PUBLISHABLE_STATES)}"
    if hypothesis.evidence_stage not in _PUBLISHABLE_EVIDENCE_STAGES:
        return False, f"evidence_stage={hypothesis.evidence_stage!r} not in {sorted(_PUBLISHABLE_EVIDENCE_STAGES)}"
    if not hypothesis.reason_codes:
        return False, "reason_codes empty"
    if len(set(hypothesis.evidence_links)) < 2:
        return False, "evidence_links requires at least two distinct references"
    if hypothesis.has_unresolved_blocking_identity_conflict:
        return False, "unresolved blocking identity conflict"
    if hypothesis.allegation_shaped_wording and not hypothesis.explicit_review_done:
        return False, "allegation-shaped wording requires explicit review"
    return True, "publishable"


def transition(
    hypothesis: InvestigationHypothesis, new_state: HypothesisState, *, actor: str,
) -> InvestigationHypothesis:
    """Never raises silently into a bad state: an invalid transition or a
    publish attempt that fails can_publish() both raise ValueError with an
    explicit reason. Every successful transition appends one AuditEntry --
    audit_history only ever grows, never rewrites."""
    allowed = _VALID_TRANSITIONS.get(hypothesis.state, frozenset())
    if new_state not in allowed:
        raise ValueError(
            f"invalid transition {hypothesis.state!r} -> {new_state!r} "
            f"(allowed: {sorted(allowed) or 'none, terminal state'})"
        )
    if new_state == "published":
        ok, reason = can_publish(hypothesis)
        if not ok:
            raise ValueError(f"cannot publish {hypothesis.hypothesis_id}: {reason}")

    entry = AuditEntry(
        actor=actor,
        timestamp=datetime.now(timezone.utc),
        old_state=hypothesis.state,
        new_state=new_state,
        evidence_snapshot_hash=_evidence_snapshot_hash(hypothesis),
    )
    return replace(
        hypothesis, state=new_state, audit_history=(*hypothesis.audit_history, entry),
    )


# ── Evidence gates (docs/fixes.md M6) ──────────────────────────────────────
# Every gate independently requires at least two distinct pieces of
# corroborating evidence -- the exit gate's other structural half: no
# single raw observation can satisfy any of these on its own.

def dark_transit_gate(
    *, has_isolated_gap_feature: bool, coverage_confidence: float,
    min_coverage_confidence: float = 0.4,
) -> tuple[bool, str]:
    """Requires isolated gap feature + sufficient coverage confidence.
    Satellite/SAR evidence may strengthen but is not required for
    candidate state."""
    if not has_isolated_gap_feature:
        return False, "no isolated gap feature"
    if coverage_confidence < min_coverage_confidence:
        return False, (
            f"coverage_confidence={coverage_confidence} below {min_coverage_confidence}"
        )
    return True, "eligible"


def covert_rendezvous_gate(
    *, has_sustained_rendezvous_episode: bool, independent_irregularities: tuple[str, ...],
) -> tuple[bool, str]:
    """Requires a sustained rendezvous episode plus independent irregularity
    (gap, identity conflict, concealed movement, unusual operational
    context) before review_ready."""
    if not has_sustained_rendezvous_episode:
        return False, "no sustained rendezvous episode"
    if not independent_irregularities:
        return False, "no independent irregularity alongside the rendezvous"
    return True, "eligible"


def identity_deception_gate(
    *, contradictory_observations: tuple[dict[str, Any], ...],
) -> tuple[bool, str]:
    """Requires contradictory identity observations across time/source. A
    duplicate MMSI alone is a candidate integrity problem until
    contextualized -- so at least two observations from at least two
    distinct sources are required, not merely two timestamps from the
    same feed."""
    distinct_sources = {str(o.get("source") or "") for o in contradictory_observations if o.get("source")}
    if len(contradictory_observations) < 2 or len(distinct_sources) < 2:
        return False, "insufficient contradictory identity observations across time/source"
    return True, "eligible"


def position_spoofing_gate(
    *, implausible_movement: bool, reproducible_inputs: bool, counter_evidence_checked: bool,
) -> tuple[bool, str]:
    """Requires impossible/implausible movement or spatial inconsistency
    with reproducible inputs and counter-evidence checks."""
    if not implausible_movement:
        return False, "no implausible movement or spatial inconsistency"
    if not reproducible_inputs:
        return False, "inputs not reproducible"
    if not counter_evidence_checked:
        return False, "counter-evidence not checked"
    return True, "eligible"


def sanctions_evasion_pattern_gate(
    *, official_list_match: bool, behavioural_evidence: tuple[str, ...],
) -> tuple[bool, str]:
    """Requires an official-list/entity link plus behavioural evidence. A
    sanctions list match alone publishes only the official-list fact,
    never "evasion" (see core.mda.vessel_subject.sanctions_fact_for,
    M5.1, for that fact-only projection)."""
    if not official_list_match:
        return False, "no official-list/entity link"
    if not behavioural_evidence:
        return False, "sanctions list match alone never publishes 'evasion'"
    return True, "eligible"


def infrastructure_pattern_gate(
    *, dwell_or_route_repetition: bool, independent_corroboration: tuple[str, ...],
) -> tuple[bool, str]:
    """Requires more than proximity: dwell/route repetition plus
    independent anomaly/corroboration before review-ready state."""
    if not dwell_or_route_repetition:
        return False, "proximity alone is not a pattern"
    if not independent_corroboration:
        return False, "no independent anomaly/corroboration"
    return True, "eligible"


_GATES = {
    "dark_transit": dark_transit_gate,
    "covert_rendezvous": covert_rendezvous_gate,
    "identity_deception": identity_deception_gate,
    "position_spoofing": position_spoofing_gate,
    "sanctions_evasion_pattern": sanctions_evasion_pattern_gate,
    "infrastructure_pattern": infrastructure_pattern_gate,
}


def evaluate_gate(hypothesis_type: HypothesisType, **evidence: Any) -> tuple[bool, str]:
    """Dispatch to the named gate function. Raises KeyError for an
    unrecognised hypothesis_type -- never a silent pass."""
    return _GATES[hypothesis_type](**evidence)
