from core.intel.store import IntelEvent, IntelStore


def test_deterministic_machine_event_refreshes_without_duplication(monkeypatch):
    store = IntelStore()
    persisted = []
    broadcasts = []
    notified = []
    monkeypatch.setattr(store, "_persist", lambda event: persisted.append(event))
    monkeypatch.setattr(store, "_fire_broadcast", lambda event: broadcasts.append(event.id))
    monkeypatch.setattr(store, "_notify_subscribers", lambda event: notified.append(event.id))

    first = IntelEvent(
        id="aisgap:247123456",
        type="ais_anomaly",
        severity="medium",
        lat=34.0,
        lon=18.0,
        title="AIS gap",
        text="initial",
        source="mda",
        linked_mmsi="247123456",
        metadata={
            "anomaly_type": "gap",
            "publication_status": "internal",
            "analysis_state": "anomaly",
        },
    )
    assert store.add(first) is True
    assert broadcasts == [first.id]
    assert notified == [first.id]

    refresh = IntelEvent(
        id=first.id,
        type="ais_anomaly",
        severity="high",
        lat=34.1,
        lon=18.2,
        title="AIS gap",
        text="refreshed",
        source="mda",
        linked_mmsi="247123456",
        metadata={
            "anomaly_type": "gap",
            "offshore_context": {"offshore": True, "distance_from_coast_km": 72.0},
            "offshore_anomaly_qualified": True,
            "analysis_state": "evidence_candidate",
            "publication_status": "published",
        },
    )
    assert store.add(refresh) is False

    events = store.events(limit=10)
    assert len(events) == 1
    current = events[0]
    assert current.id == first.id
    assert current.lat == 34.1
    assert current.lon == 18.2
    assert current.severity == "high"
    assert current.metadata["offshore_anomaly_qualified"] is True
    assert current.metadata["analysis_state"] == "evidence_candidate"
    assert current.metadata["publication_status"] == "published"

    # Refresh is persisted, but never presented as a second event.
    assert len(persisted) == 2
    assert broadcasts == [first.id]
    assert notified == [first.id]


def test_dedup_window_evicts_oldest_keys_deterministically(monkeypatch):
    import core.intel.store as store_module

    monkeypatch.setattr(store_module, "DEDUP_WINDOW", 3)
    store = IntelStore()
    with store._lock:
        store._remember_seen_locked(["a", "b", "c", "d"])
    assert store._seen == {"b", "c", "d"}
    assert list(store._seen_order) == ["b", "c", "d"]
