# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded episode builder (docs/fixes.md M5.2).

``coalesce_security_vessel_episodes()`` (core.live.vessel_episodes) used to
collapse every Live signal for one MMSI into a single, permanently-open
"episode" for the whole lifetime of that MMSI -- exactly the problem M5's
goal names: "stop equating one MMSI with one lifelong episode."

This module is the replacement logic, built and tested standalone against
the M5.2 exit gate: "two unrelated anomalies on the same MMSI days apart
become two episodes; repeated updates of one continuing event remain one
episode." Wired into ``coalesce_security_vessel_episodes()`` (docs/fixes.md
M14.2), which is now the authoritative live episode builder: it resolves
each signal's subject (core.mda.vessel_subject) and family (family_for()
below) and lets build_episodes() decide the grouping, then runs the same
rich per-episode aggregation (track, severity, source records) it always
did -- once per resulting episode instead of once per MMSI.

Episode boundary rules (docs/fixes.md M5.2), applied per (subject, family):

  - max time gap: a new episode starts once the gap since the family's last
    signal for this subject exceeds ``max_gap_s``;
  - spatial continuity: a new episode starts once consecutive signals are
    farther apart than ``max_spatial_nm`` (skipped when either signal has no
    position -- a missing coordinate is not evidence of discontinuity);
  - active hypothesis continuity: consecutive signals in the same family
    continue one episode regardless of exact anomaly sub-type (e.g. "gap"
    then "long_gap" is one continuing gap_episode, not two);
  - explicit resolution/reappearance: a signal carrying
    ``incident_lifecycle="resolved"`` closes the open episode for that
    (subject, family) -- the NEXT signal in that family, however soon,
    starts a new episode (a resolved incident reappearing is a new event,
    not a continuation of the one that just closed);
  - subject identity continuity: episodes are keyed by subject, never by
    raw MMSI alone -- see core.mda.vessel_subject (M5.1). A caller
    resolves subject_id before calling this; this module never inspects
    the MMSI itself.

A rendezvous/encounter signal can name two or more ``involved_subjects``;
such signals are grouped by the sorted tuple of every subject named across
the group's signals, not by a single subject -- "an episode can involve two
or more subjects for encounters" (M5.2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

DEFAULT_MAX_GAP_S = 3 * 24 * 3600  # 3 days -- see exit gate: "days apart" splits
DEFAULT_MAX_SPATIAL_NM = 20.0

_FAMILY_BY_ANOMALY_TYPE: dict[str, str] = {
    "gap": "gap_episode",
    "long_gap": "gap_episode",
    "vessel_gap": "gap_episode",
    "coverage_gap": "gap_episode",
    "dark_candidate": "gap_episode",  # core.intel.viirs_monitor -- satellite-corroborated AIS-dark
    "rendezvous": "rendezvous_episode",
    "sts": "rendezvous_episode",
    "ais_rendezvous": "rendezvous_episode",  # core.mda.watch._emit_rendezvous / gfw_monitor "encounter"
    "identity_anomaly": "identity_integrity_episode",
    "sdn_match": "identity_integrity_episode",
    "sanctioned_vessel": "identity_integrity_episode",
    "mmsi_duplicate": "identity_integrity_episode",
    "spoofing_candidate": "spoofing_episode",
    "position_anomaly": "spoofing_episode",
    "position_jump": "spoofing_episode",  # core.mda.watch.scan_spoofing "teleport"
    "impossible_speed": "spoofing_episode",  # core.anomaly.ais
    "dark_zone_entry": "spoofing_episode",  # core.anomaly.ais
    "port_call": "port_call_episode",
    "infrastructure_proximity": "infrastructure_proximity_episode",
    "vessel_loiter": "infrastructure_proximity_episode",
    "loiter": "infrastructure_proximity_episode",  # core.mda.watch.scan_infra_loiter / gfw_monitor
    "cable_proximity": "infrastructure_proximity_episode",  # core.mda.watch.scan_infra_loiter
    "sanctions_bunkering_loiter": "infrastructure_proximity_episode",  # core.mda.watch.scan_infra_loiter
    "sanctioned_port_call": "port_call_episode",
    "not_under_command": "safety_episode",
    "sudden_stop": "safety_episode",
}
_KNOWN_FAMILIES = frozenset(
    {
        "gap_episode", "rendezvous_episode", "identity_integrity_episode",
        "spoofing_episode", "port_call_episode",
        "infrastructure_proximity_episode", "safety_episode", "unclassified_episode",
    }
)


def family_for(anomaly_type: Optional[str], *, explicit_family: Optional[str] = None) -> str:
    """The episode family for one signal. An explicit, already-known family
    on the signal always wins; otherwise map the anomaly_type; anything
    unrecognised fails closed to unclassified_episode. Safety is reserved
    for explicitly mapped operational Safety semantics; it is never a
    generic fallback for unknown intelligence signals."""
    if explicit_family and explicit_family in _KNOWN_FAMILIES:
        return explicit_family
    return _FAMILY_BY_ANOMALY_TYPE.get(str(anomaly_type or ""), "unclassified_episode")


@dataclass(frozen=True)
class EpisodeSignal:
    signal_id: str
    subject_ids: tuple[str, ...]  # one subject normally; 2+ for an encounter
    family: str
    observed_at: datetime
    lat: Optional[float] = None
    lon: Optional[float] = None
    resolved: bool = False  # this signal's incident_lifecycle == "resolved"


@dataclass
class Episode:
    """Mutable while build_episodes() assembles it (signal_ids grows,
    last_observed_at/resolved advance as later signals join); callers
    receiving the returned list should treat it as a finished, read-only
    result."""
    episode_id: str
    subject_ids: tuple[str, ...]
    family: str
    signal_ids: list[str] = field(default_factory=list)
    first_observed_at: Optional[datetime] = None
    last_observed_at: Optional[datetime] = None
    resolved: bool = False


def _distance_nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return 3440.065 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def build_episodes(
    signals: list[EpisodeSignal],
    *,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
    max_spatial_nm: float = DEFAULT_MAX_SPATIAL_NM,
) -> list[Episode]:
    """Callers own resolving each signal's subject_id(s) (core.mda.vessel_subject,
    M5.1) and family (family_for() above) before building EpisodeSignal
    instances -- this function only applies the boundary rules across an
    already-normalised signal list. Signals need not be pre-sorted; each
    group is sorted internally before the boundary rules are applied.
    """
    groups: dict[tuple[tuple[str, ...], str], list[EpisodeSignal]] = {}
    for signal in signals:
        key = (tuple(sorted(signal.subject_ids)), signal.family)
        groups.setdefault(key, []).append(signal)

    episodes: list[Episode] = []
    for (subject_ids, family), group_signals in groups.items():
        ordered = sorted(group_signals, key=lambda s: s.observed_at)
        current: Optional[Episode] = None
        episode_seq = 0
        last_positioned: Optional[EpisodeSignal] = None
        for signal in ordered:
            start_new = current is None
            if current is not None:
                gap_s = (signal.observed_at - current.last_observed_at).total_seconds()
                spatial_break = False
                if (
                    signal.lat is not None and signal.lon is not None
                    and last_positioned is not None
                ):
                    distance = _distance_nm(
                        (last_positioned.lat, last_positioned.lon), (signal.lat, signal.lon),
                    )
                    spatial_break = distance > max_spatial_nm
                if current.resolved or gap_s > max_gap_s or spatial_break:
                    start_new = True

            if signal.lat is not None and signal.lon is not None:
                last_positioned = signal

            if start_new:
                episode_seq += 1
                episode_id = (
                    f"episode:{'+'.join(subject_ids)}:{family}:{episode_seq}"
                )
                current = Episode(
                    episode_id=episode_id,
                    subject_ids=subject_ids,
                    family=family,
                    signal_ids=[],
                    first_observed_at=signal.observed_at,
                    last_observed_at=signal.observed_at,
                )
                episodes.append(current)

            current.signal_ids.append(signal.signal_id)
            current.last_observed_at = signal.observed_at
            if signal.resolved:
                current.resolved = True

    episodes.sort(key=lambda e: (e.subject_ids, e.first_observed_at))
    return episodes
