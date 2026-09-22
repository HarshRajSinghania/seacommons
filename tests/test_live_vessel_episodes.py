from __future__ import annotations

from core.live.vessel_episodes import (
    add_nearby_humanitarian_context,
    coalesce_security_vessel_episodes,
)


def _feature(event_id, timestamp, lon, lat, **properties):
    return {
        "type": "Feature",
        "id": event_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "id": event_id,
            "timestamp_utc": timestamp,
            "severity": "medium",
            "source": "ais",
            **properties,
        },
    }


def test_repeated_updates_in_the_same_family_become_one_episode() -> None:
    """docs/fixes.md M14.2: build_episodes() combines same-subject,
    same-family, close-in-time signals into one episode -- the pre-M14.2
    per-MMSI rich aggregation (track, signal_count, source records, the
    NUC-resolved special case) still runs, just per episode now. Both
    items here declare the same explicit `episode_family` -- the lever a
    correlation layer uses when it already knows two differently-typed
    signals are one real incident (docs/live/episode_builder.family_for:
    "an explicit, already-known family on the signal always wins")."""
    gap = _feature(
        "intel:gap", "2026-08-30T19:00:00+00:00", 29.14, 41.33,
        type="ais_anomaly", linked_mmsi="352001914", anomaly_type="gap",
        maritime_domain="sanctions", title="AIS gap — ST. OLGA",
        episode_family="safety_episode",
    )
    incident = _feature(
        "intel:nuc", "2026-08-30T20:00:00+00:00", 29.16, 41.34,
        type="vessel_incident", linked_mmsi="352001914",
        ais_nav_status_kind="not_under_command", maritime_domain="grey_zone",
        title="Vessel unable to manoeuvre — ST. OLGA", drift_eligible=True,
        url="https://example.test/olga", episode_family="safety_episode",
    )

    result = coalesce_security_vessel_episodes(
        [gap, incident],
        track_history={
            "352001914": [
                {"lon": 29.12, "lat": 41.32, "ts": "2026-08-30T18:00:00+00:00", "nav_status": 2, "sog": 0.2},
                {"lon": 29.18, "lat": 41.35, "ts": "2026-08-30T21:00:00+00:00", "nav_status": 0, "sog": 7.2},
            ]
        },
    )

    assert len(result) == 1
    episode = result[0]
    assert episode["id"].startswith("episode:subj:mmsi:352001914:safety_episode:")
    assert episode["geometry"]["coordinates"] == [29.18, 41.35]
    assert episode["properties"]["maritime_domain"] == "grey_zone"
    assert episode["properties"]["signal_count"] == 2
    assert len(episode["properties"]["observed_track"]) == 4
    assert episode["properties"]["drift_event_id"] == "intel:nuc"
    assert episode["properties"]["incident_lifecycle"] == "resolved"
    assert episode["properties"]["latest_nav_status"] == 0
    assert episode["properties"]["source_records"][0]["url"] == "https://example.test/olga"


def test_exit_gate_two_unrelated_anomaly_families_become_two_episodes() -> None:
    """docs/fixes.md M14.2 exit gate, live-wiring form: two unrelated
    anomalies on the same vessel (here, different families: a reporting
    gap vs. a not-under-command incident) become two separate episodes,
    not one lifelong per-MMSI blob."""
    gap = _feature(
        "intel:gap2", "2026-08-30T19:00:00+00:00", 29.14, 41.33,
        type="ais_anomaly", linked_mmsi="352001915", anomaly_type="gap",
        maritime_domain="sanctions", title="AIS gap — vessel B",
    )
    incident = _feature(
        "intel:nuc2", "2026-08-30T20:00:00+00:00", 29.16, 41.34,
        type="vessel_incident", linked_mmsi="352001915",
        ais_nav_status_kind="not_under_command", maritime_domain="grey_zone",
        title="Vessel unable to manoeuvre — vessel B",
    )

    result = coalesce_security_vessel_episodes([gap, incident])

    assert len(result) == 2
    ids = {f["properties"]["id"] for f in result}
    assert any(":gap_episode:" in i for i in ids)
    assert any(":safety_episode:" in i for i in ids)
    for feature in result:
        assert feature["properties"]["mmsi"] == "352001915"
        assert feature["properties"]["signal_count"] == 1


def test_exit_gate_two_unrelated_gaps_days_apart_become_two_episodes() -> None:
    """Same family, but separated by more than build_episodes()'s 3-day
    default boundary -- a genuinely new occurrence, not a continuation."""
    old_gap = _feature(
        "intel:gap-old", "2026-08-20T10:00:00+00:00", 14.0, 35.5,
        type="ais_anomaly", linked_mmsi="352001916", anomaly_type="gap",
        title="AIS gap — vessel C (old)",
    )
    new_gap = _feature(
        "intel:gap-new", "2026-08-30T10:00:00+00:00", 14.0, 35.5,
        type="ais_anomaly", linked_mmsi="352001916", anomaly_type="gap",
        title="AIS gap — vessel C (new)",
    )

    result = coalesce_security_vessel_episodes([old_gap, new_gap])

    assert len(result) == 2
    for feature in result:
        assert feature["properties"]["signal_count"] == 1


def test_real_sanctions_match_enriches_episode_domain() -> None:
    result = coalesce_security_vessel_episodes([
        _feature(
            "intel:sdn", "2026-08-30T20:00:00+00:00", 20.0, 35.0,
            type="vessel_identity", linked_mmsi="273999000", anomaly_type="sdn_match",
            maritime_domain="sanctions", title="Sanctioned vessel",
        )
    ])
    assert result[0]["properties"]["sanctions_matched"] is True
    assert result[0]["properties"]["maritime_domain"] == "sanctions"


def test_humanitarian_proximity_is_context_not_a_merge() -> None:
    security = [_feature(
        "vessel-episode:1", "2026-08-30T20:00:00+00:00", 14.0, 35.0,
        type="ais_anomaly", title="AIS gap",
    )]
    humanitarian = [_feature(
        "intel:sos", "2026-08-30T19:30:00+00:00", 14.1, 35.0,
        type="distress", title="Distress alert",
    )]

    add_nearby_humanitarian_context(security, humanitarian)

    props = security[0]["properties"]
    assert props["nearby_humanitarian_count"] == 1
    assert props["nearby_humanitarian"][0]["title"] == "Distress alert"


def _seed_intel_event(event_id: str, *, event_type: str, source: str, **metadata) -> None:
    from core.intel.store import IntelEvent, intel_store

    intel_store.add(IntelEvent(
        id=event_id,
        type=event_type,
        severity="medium",
        lat=35.5,
        lon=14.1,
        title=f"seed:{event_id}",
        source=source,
        linked_mmsi="211879870",
        metadata=metadata,
    ), dedup_key=event_id)


def test_episode_verification_same_ais_lineage_is_multi_indicator() -> None:
    from core.intel.store import intel_store

    with intel_store._lock:
        intel_store._events.clear(); intel_store._seen.clear()
    _seed_intel_event("lineage:a", event_type="ais_anomaly", source="AISStream", anomaly_type="gap")
    _seed_intel_event("lineage:b", event_type="ais_anomaly", source="mda", anomaly_type="gap")
    result = coalesce_security_vessel_episodes([
        _feature("lineage:a", "2026-09-06T08:00:00+00:00", 14.1, 35.5, linked_mmsi="211879870", anomaly_type="gap"),
        _feature("lineage:b", "2026-09-06T08:20:00+00:00", 14.1, 35.5, linked_mmsi="211879870", anomaly_type="gap"),
    ])
    props = result[0]["properties"]
    assert props["independence_groups"] == ["ais_sensor_lineage"]
    assert props["verification_status"] == "single_source_multi_indicator"


def test_episode_verification_two_independent_lineages_is_corroborated() -> None:
    from core.intel.store import intel_store

    with intel_store._lock:
        intel_store._events.clear(); intel_store._seen.clear()
    _seed_intel_event("lineage:c", event_type="ais_anomaly", source="mda", anomaly_type="gap")
    _seed_intel_event(
        "lineage:d",
        event_type="news",
        source="Independent report",
        anomaly_type="gap",
        transport="rss",
    )
    result = coalesce_security_vessel_episodes([
        _feature("lineage:c", "2026-09-06T08:00:00+00:00", 14.1, 35.5, linked_mmsi="211879870", anomaly_type="gap"),
        _feature("lineage:d", "2026-09-06T08:20:00+00:00", 14.1, 35.5, linked_mmsi="211879870", anomaly_type="gap"),
    ])
    props = result[0]["properties"]
    assert props["independent_source_count"] == 2
    assert set(props["independence_groups"]) == {"ais_sensor_lineage", "secondary_news_reporting"}
    assert props["verification_status"] == "multi_source_corroborated"


def test_episode_preserves_behaviour_context_and_alternative_explanations() -> None:
    contextual = _feature(
        "intel:behaviour", "2026-09-06T08:00:00+00:00", 14.1, 35.5,
        type="ais_anomaly", linked_mmsi="211879870", anomaly_type="gap",
        behaviour_context={
            "status": "expected",
            "reason_codes": [],
            "baseline_id": "baseline:test",
        },
        alternative_explanations=["COASTAL_RECEIVER_COVERAGE", "SCHEDULED_SERVICE_PATTERN"],
    )
    later_update = _feature(
        "intel:behaviour-update", "2026-09-06T08:20:00+00:00", 14.1, 35.5,
        type="ais_anomaly", linked_mmsi="211879870", anomaly_type="gap",
        alternative_explanations=["COASTAL_RECEIVER_COVERAGE"],
    )

    result = coalesce_security_vessel_episodes([contextual, later_update])
    props = result[0]["properties"]

    assert props["behaviour_context"]["status"] == "expected"
    assert props["behaviour_context"]["baseline_id"] == "baseline:test"
    assert props["alternative_explanations"] == [
        "COASTAL_RECEIVER_COVERAGE",
        "SCHEDULED_SERVICE_PATTERN",
    ]


def test_rendezvous_feature_uses_explicit_multi_subject_identity():
    feature = _feature(
        "intel:rdv-pair", "2026-09-17T12:00:00+00:00", 17.0, 35.0,
        type="ais_rendezvous", linked_mmsi="247123456",
        anomaly_type="ais_rendezvous",
        subject_ids=["subj:mmsi:247123456", "subj:mmsi:255987654"],
        title="Encounter pair",
    )

    result = coalesce_security_vessel_episodes([feature])

    assert len(result) == 1
    assert result[0]["properties"]["subject_ids"] == [
        "subj:mmsi:247123456", "subj:mmsi:255987654"
    ]
    assert "subj:mmsi:247123456+subj:mmsi:255987654" in result[0]["id"]


def test_public_maritime_episode_role_is_explicit() -> None:
    result = coalesce_security_vessel_episodes([
        _feature(
            "intel:episode-role", "2026-09-22T08:00:00+00:00", 14.1, 35.5,
            type="ais_anomaly", linked_mmsi="211879870", anomaly_type="gap",
            publication_state="published", analysis_state="evidence_candidate",
        )
    ])
    assert len(result) == 1
    assert result[0]["properties"]["live_role"] == "maritime_episode"
