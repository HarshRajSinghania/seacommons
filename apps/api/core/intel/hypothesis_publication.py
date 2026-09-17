# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public Maritime Intelligence projection for InvestigationHypothesis
(docs/fixes.md M14.4).

core.intel.publication_policy.project_public_maritime_assessed() is the
sole authority for what leaves this module: it independently re-verifies
core.intel.hypothesis.can_publish() itself (docs/fixes.md M14.4: "Maritime
Intelligence public output must require the hypothesis publication
gate"), so nothing here duplicates that decision -- a hypothesis is never
shaped for public output on any other basis.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.intel.hypothesis import InvestigationHypothesis
from core.intel.hypothesis_store import list_hypotheses
from core.intel.publication_policy import project_public_maritime_assessed
from core.intel.store import intel_store

_HYPOTHESIS_SUMMARY = {
    "dark_transit": "Vessel-specific AIS reporting gap consistent with intentional dark transit.",
    "covert_rendezvous": "Sustained rendezvous with an independent irregularity alongside it.",
    "position_spoofing": "AIS position data inconsistent with plausible vessel movement.",
    "infrastructure_pattern": (
        "Repeated dwell/route pattern near sensitive infrastructure, independently corroborated."
    ),
}


def _record_for_hypothesis(hypothesis: InvestigationHypothesis) -> dict[str, Any]:
    """The minimal canonical record core.intel.publication_policy needs --
    built from the hypothesis's own evidence_links, never from vessel
    identity: a published hypothesis is about a behaviour pattern, not a
    public dossier on the vessel (same principle as project_public_safety
    /project_public_maritime_assessed's own _VESSEL_IDENTITY_FIELDS strip)."""
    events = [e for e in (intel_store.get_durable(sid) for sid in hypothesis.evidence_links) if e is not None]
    events.sort(key=lambda e: e.timestamp_utc)
    latest = events[-1] if events else None
    return {
        "id": hypothesis.hypothesis_id,
        "title": (latest.title if latest else hypothesis.hypothesis_type.replace("_", " ").title()),
        "lat": latest.lat if latest else None,
        "lon": latest.lon if latest else None,
        "timestamp_utc": latest.timestamp_utc if latest else None,
        "observation_text": (latest.text or latest.title) if latest else "",
        "interpretation_text": _HYPOTHESIS_SUMMARY.get(hypothesis.hypothesis_type, ""),
        "evidence_stage": hypothesis.evidence_stage,
        "caveats": hypothesis.counter_indicators,
        "sanctions": (),
    }


def public_hypothesis_collection(limit: int = 100) -> dict[str, Any]:
    """Every persisted hypothesis currently in the "published" state,
    shaped through core.intel.publication_policy's Maritime Intelligence
    target. A hypothesis only ever reaches "published" through
    core.intel.hypothesis.transition()'s own can_publish() enforcement
    (docs/fixes.md M6); this function's own project_public_maritime_
    assessed() call re-verifies that independently rather than trusting
    the persisted state column alone.
    """
    features = []
    for hypothesis in list_hypotheses(state="published", limit=limit):
        record = _record_for_hypothesis(hypothesis)
        projected = project_public_maritime_assessed(record, hypothesis=hypothesis)
        if projected is None or projected.get("lat") is None or projected.get("lon") is None:
            continue
        features.append({
            "type": "Feature",
            "id": projected["id"],
            "geometry": {"type": "Point", "coordinates": [projected["lon"], projected["lat"]]},
            "properties": projected,
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {"count": len(features), "generated_at": datetime.now(timezone.utc).isoformat()},
    }
