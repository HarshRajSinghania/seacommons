# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collapse vessel-centric Live signals into stable, updateable episodes.

docs/fixes.md M14.2: grouping is now decided by
core.live.episode_builder.build_episodes() (M5.2) over subjects resolved via
core.mda.vessel_subject (M5.1), not by raw MMSI alone. A vessel's signals
split into one episode per (subject, family) -- and, within a family, per
the time/spatial/resolution boundary rules build_episodes() applies -- so
two unrelated anomalies on the same vessel (different family, or the same
family separated by a stale/resolved gap) become separate episodes instead
of one lifelong per-MMSI blob. The rich per-episode aggregation below
(track, severity, source records, drift/infrastructure context) is
unchanged; it now runs once per *episode* rather than once per MMSI.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any

from core.live.episode_builder import EpisodeSignal, build_episodes, family_for
from core.mda.vessel_subject import subject_id_for

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_SANCTIONS_TYPES = {"sdn_match", "sanctioned_vessel"}


def _timestamp(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, tz=UTC)


def _mmsi(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") or {}
    value = str(properties.get("linked_mmsi") or properties.get("mmsi") or "").strip()
    return value if len(value) == 9 and value.isdigit() else ""


def _point(feature: dict[str, Any]) -> list[float] | None:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates")
    if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        return [float(coordinates[0]), float(coordinates[1])]
    except (TypeError, ValueError):
        return None


def _track_points(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for feature in features:
        properties = feature.get("properties") or {}
        for raw in properties.get("observed_track") or []:
            if not isinstance(raw, dict):
                continue
            try:
                lon, lat = float(raw["lon"]), float(raw["lat"])
            except (KeyError, TypeError, ValueError):
                continue
            points.append(
                {
                    "lon": round(lon, 6),
                    "lat": round(lat, 6),
                    "ts": raw.get("ts") or properties.get("timestamp_utc"),
                    **({"sog": raw.get("sog")} if raw.get("sog") is not None else {}),
                    **(
                        {"nav_status": raw.get("nav_status")}
                        if raw.get("nav_status") is not None
                        else {}
                    ),
                }
            )
        point = _point(feature)
        if point:
            points.append(
                {
                    "lon": round(point[0], 6),
                    "lat": round(point[1], 6),
                    "ts": properties.get("timestamp_utc"),
                }
            )

    points.sort(key=lambda item: _timestamp(item.get("ts")))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[float, float, str]] = set()
    for point in points:
        key = (point["lon"], point["lat"], str(point.get("ts") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(point)
    return unique[-120:]


def coalesce_security_vessel_episodes(
    features: list[dict[str, Any]],
    *,
    track_history: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Return one current marker per bounded vessel episode, retaining its
    evidence timeline.

    Non-vessel signals remain independent. A vessel's signals are grouped by
    subject (core.mda.vessel_subject) and family (core.live.episode_builder)
    before core.live.episode_builder.build_episodes() decides where one
    episode ends and the next begins -- see module docstring. Sanctions is
    selected as an episode's domain only for an actual sanctions match;
    legacy AIS gaps that were stored with ``maritime_domain=sanctions`` are
    normalised to generic maritime-security context here.
    """
    ungrouped: list[dict[str, Any]] = []
    by_signal_id: dict[str, dict[str, Any]] = {}
    signals: list[EpisodeSignal] = []
    for feature in features:
        mmsi = _mmsi(feature)
        if not mmsi:
            ungrouped.append(feature)
            continue
        props = feature.get("properties") or {}
        signal_id = str(props.get("id") or "") or f"anon:{id(feature)}"
        if signal_id in by_signal_id:
            continue  # duplicate id in one snapshot -- keep the first
        by_signal_id[signal_id] = feature
        explicit_subject_ids = tuple(sorted({
            str(value).strip() for value in (props.get("subject_ids") or ())
            if str(value).strip()
        }))
        subject_id = subject_id_for(imo=props.get("imo"), mmsi=mmsi) or f"subj:mmsi:{mmsi}"
        subject_ids = explicit_subject_ids or (subject_id,)
        point = _point(feature)
        signals.append(EpisodeSignal(
            signal_id=signal_id,
            subject_ids=subject_ids,
            family=family_for(
                props.get("anomaly_type") or props.get("ais_nav_status_kind"),
                explicit_family=props.get("episode_family"),
            ),
            observed_at=_parse_datetime(props.get("timestamp_utc")),
            lat=point[1] if point else None,
            lon=point[0] if point else None,
            resolved=props.get("incident_lifecycle") == "resolved",
        ))

    episodes: list[dict[str, Any]] = []
    for group in build_episodes(signals):
        items = [by_signal_id[sid] for sid in group.signal_ids if sid in by_signal_id]
        if not items:
            continue
        episodes.append(_build_episode_feature(
            group.episode_id, group.subject_ids, group.family, items, track_history))

    return ungrouped + episodes


def _build_episode_feature(
    episode_id: str,
    subject_ids: tuple[str, ...],
    family: str,
    items: list[dict[str, Any]],
    track_history: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """The rich per-episode marker (current point, track, severity, source
    records, drift/infrastructure context) for one build_episodes() group's
    own items -- unchanged from the pre-M14.2 per-MMSI aggregation, just
    scoped to one bounded episode's items instead of every item ever seen
    for that MMSI."""
    mmsi = _mmsi(items[-1]) or next((m for item in items if (m := _mmsi(item))), "")
    items.sort(key=lambda item: _timestamp((item.get("properties") or {}).get("timestamp_utc")))
    positioned = [item for item in items if _point(item)]
    primary = deepcopy(positioned[-1] if positioned else items[-1])
    props = primary.setdefault("properties", {})
    history_feature = {
        "properties": {"observed_track": (track_history or {}).get(mmsi, [])}
    }
    track = _track_points([*items, history_feature])
    if track:
        latest = track[-1]
        primary["geometry"] = {
            "type": "Point",
            "coordinates": [latest["lon"], latest["lat"]],
        }

    item_props = [item.get("properties") or {} for item in items]
    behaviour_context = next(
        (dict(p["behaviour_context"]) for p in reversed(item_props)
         if isinstance(p.get("behaviour_context"), dict) and p.get("behaviour_context")),
        {},
    )
    alternative_explanations = list(dict.fromkeys(
        str(reason)
        for p in item_props
        for reason in (p.get("alternative_explanations") or ())
        if reason
    ))
    anomaly_types = sorted(
        {
            str(p.get("anomaly_type") or p.get("ais_nav_status_kind") or "")
            for p in item_props
            if p.get("anomaly_type") or p.get("ais_nav_status_kind")
        }
    )
    has_sanctions_match = any(
        str(p.get("anomaly_type") or "") in _SANCTIONS_TYPES
        or bool(p.get("sanctions_matched"))
        for p in item_props
    )
    most_severe = max(
        (str(p.get("severity") or "low") for p in item_props),
        key=lambda severity: _SEVERITY_RANK.get(severity, 0),
    )
    episode_signal_ids = [str(p.get("id") or "") for p in item_props if p.get("id")]
    observation_ids: list[str] = []
    feature_ids: list[str] = []
    for item in item_props:
        parents = [str(v) for v in (item.get("observation_ids") or ()) if v]
        if parents:
            observation_ids.extend(parents)
        elif item.get("id"):
            observation_ids.append(str(item["id"]))
        feature_ids.extend(str(v) for v in (item.get("feature_ids") or ()) if v)
    observation_ids = list(dict.fromkeys(observation_ids))
    feature_ids = list(dict.fromkeys(feature_ids))
    from core.intel.fusion import verification_for_event_ids
    verification_status, independence_groups, evidence_count = verification_for_event_ids(observation_ids)
    # Only producer-supplied, already-validated independence groups may
    # override the resolver. Generic lineage_ids are provenance labels, not
    # proof of independence (e.g. GFW AIS gaps and local AIS gaps share the
    # same underlying AIS lineage).
    explicit_groups = sorted({
        str(group)
        for p in item_props
        for group in (p.get("contributing_independence_groups") or ())
        if str(group).strip()
    })
    if len(explicit_groups) >= 2:
        independence_groups = explicit_groups
        verification_status = "multi_source_corroborated"
        evidence_count = max(evidence_count, len(explicit_groups))
    analysis_states = {str(p.get("analysis_state") or "observation") for p in item_props}
    episode_analysis_state = (
        "evidence"
        if verification_status == "multi_source_corroborated" or "evidence" in analysis_states
        else "evidence_candidate"
        if analysis_states & {"anomaly", "evidence_candidate"}
        else "observation"
    )
    resolution_states = {str(p.get("resolution_state") or "open") for p in item_props}
    episode_resolution_state = "open" if "open" in resolution_states else (
        "explained" if "explained" in resolution_states else "superseded"
    )
    publication_states = {str(p.get("publication_state") or "internal") for p in item_props}
    episode_publication_state = (
        "published" if "published" in publication_states
        else "reviewed" if "reviewed" in publication_states
        else "internal"
    )
    update_count = max(
        len(track),
        sum(max(1, int(p.get("episode_update_count") or 1)) for p in item_props),
    )
    drift_source = next(
        (p for p in reversed(item_props) if p.get("drift_eligible")),
        None,
    )
    infrastructure_source = next(
        (p for p in reversed(item_props) if isinstance(p.get("infrastructure"), dict)),
        None,
    )
    port_call_source = next(
        (p for p in reversed(item_props) if isinstance(p.get("port_call"), dict)),
        None,
    )
    sanctions_source = next(
        (p for p in reversed(item_props) if isinstance(p.get("sanctions"), list) and p.get("sanctions")),
        None,
    )
    source_records: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str, str]] = set()
    for p in reversed(item_props):
        source_record = {
            "source": str(p.get("source") or "SeaCommons analysis"),
            "title": str(p.get("title") or "Maritime signal"),
            "url": str(p.get("url") or ""),
            "timestamp_utc": p.get("timestamp_utc"),
            "type": p.get("type"),
            "verification_status": p.get("verification_status"),
        }
        source_key = (
            source_record["source"],
            source_record["url"],
            source_record["title"],
        )
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        source_records.append(source_record)
    updates = [
        {
            "id": p.get("id"),
            "timestamp_utc": p.get("timestamp_utc"),
            "type": p.get("type"),
            "anomaly_type": p.get("anomaly_type") or p.get("ais_nav_status_kind"),
            "title": p.get("title"),
            "severity": p.get("severity"),
            "source": p.get("source"),
        }
        for p in reversed(item_props)
    ][:12]
    first_signal_at = item_props[0].get("timestamp_utc")
    last_signal_at = item_props[-1].get("timestamp_utc")
    first_observed_at = (
        track[0].get("ts")
        if track and _timestamp(track[0].get("ts")) < _timestamp(first_signal_at)
        else first_signal_at
    )
    last_observed_at = (
        track[-1].get("ts")
        if track and _timestamp(track[-1].get("ts")) > _timestamp(last_signal_at)
        else last_signal_at
    )
    props.update(
        {
            "id": episode_id,
            "episode_id": episode_id,
            "episode_family": family,
            "subject_ids": list(subject_ids),
            "linked_mmsi": mmsi,
            "mmsi": mmsi,
            "severity": most_severe,
            "maritime_domain": "sanctions" if has_sanctions_match else "grey_zone",
            "sanctions_matched": has_sanctions_match,
            "signal_count": len(items),
            "episode_update_count": update_count,
            "first_observed_at": first_observed_at,
            "last_observed_at": last_observed_at,
            "timestamp_utc": last_observed_at,
            "related_signal_ids": observation_ids,
            "observation_ids": observation_ids,
            "feature_ids": feature_ids,
            "episode_signal_ids": episode_signal_ids,
            "evidence_count": evidence_count,
            "independence_groups": independence_groups,
            "independent_source_count": len(independence_groups),
            "verification_status": verification_status,
            "analysis_state": episode_analysis_state,
            "publication_state": episode_publication_state,
            "resolution_state": episode_resolution_state,
            "lineage_ids": independence_groups,
            "behaviour_context": behaviour_context,
            "alternative_explanations": alternative_explanations,
            "alert_types": sorted({str(p.get("type")) for p in item_props if p.get("type")}),
            "anomaly_types": anomaly_types,
            "contributing_sources": sorted(
                {str(p.get("source")) for p in item_props if p.get("source")}
            ),
            "source_records": source_records[:12],
            "observed_track": track,
            "track_kind": "observed_ais",
            "track_last_seen": track[-1].get("ts") if track else None,
            "latest_sog": track[-1].get("sog") if track else None,
            "latest_nav_status": track[-1].get("nav_status") if track else None,
            "updates": updates,
            "drift_eligible": bool(drift_source),
            **(
                {"port_call": dict(port_call_source.get("port_call") or {})}
                if port_call_source
                else {}
            ),
            **(
                {"sanctions": list(sanctions_source.get("sanctions") or [])}
                if sanctions_source
                else {}
            ),
            **(
                {
                    "infrastructure": infrastructure_source.get("infrastructure"),
                    "loiter_minutes": infrastructure_source.get("loiter_minutes"),
                }
                if infrastructure_source
                else {}
            ),
            **(
                {
                    "drift_event_id": drift_source.get("id"),
                    "drift_vessel_type": drift_source.get("drift_vessel_type") or "cargo",
                    "drift_status": drift_source.get("drift_status"),
                    "drift_job_id": drift_source.get("drift_job_id"),
                }
                if drift_source
                else {}
            ),
        }
    )
    # A later normal AIS navigation status is direct evidence that a
    # previous NUC episode ended.  It does not invalidate other anomalies
    # (for example a subsequent reporting gap), so only the incident state
    # and its explanatory note are changed.
    if (
        "not_under_command" in anomaly_types
        and track
        and track[-1].get("nav_status") in {0, 1, 5, 8}
    ):
        props["incident_lifecycle"] = "resolved"
        props["status_note"] = "Latest AIS navigation status returned to normal."
    primary["id"] = episode_id
    return primary


def _distance_nm(a: list[float], b: list[float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    h = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 3440.065 * 2 * asin(sqrt(max(0.0, min(1.0, h))))


def add_nearby_humanitarian_context(
    security: list[dict[str, Any]],
    humanitarian: list[dict[str, Any]],
    *,
    radius_nm: float = 20.0,
    window_hours: float = 24.0,
) -> None:
    """Annotate, never merge: proximity is context, not proof of relation."""
    for feature in security:
        origin = _point(feature)
        if not origin:
            continue
        properties = feature.get("properties") or {}
        observed = _timestamp(properties.get("timestamp_utc"))
        nearby: list[dict[str, Any]] = []
        for candidate in humanitarian:
            target = _point(candidate)
            other = candidate.get("properties") or {}
            if not target or abs(observed - _timestamp(other.get("timestamp_utc"))) > window_hours * 3600:
                continue
            distance = _distance_nm(origin, target)
            if distance <= radius_nm:
                nearby.append(
                    {
                        "id": other.get("id"),
                        "title": other.get("title"),
                        "timestamp_utc": other.get("timestamp_utc"),
                        "distance_nm": round(distance, 1),
                    }
                )
        nearby.sort(key=lambda item: item["distance_nm"])
        properties["nearby_humanitarian_count"] = len(nearby)
        properties["nearby_humanitarian"] = nearby[:3]
