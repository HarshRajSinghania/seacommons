# SPDX-License-Identifier: AGPL-3.0-or-later
from core.api.routes.mda import _derive_recent_port_calls


def test_port_calls_require_slow_or_moored_ais_fix(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.mda.reference.reference.in_port_or_anchorage",
        lambda lat, lon: "Piraeus" if lon < 24 else None,
    )
    track = [
        {"lat": 37.9, "lon": 23.6, "sog": 12.0, "nav_status": 0, "ts": "2026-08-29T10:00:00Z"},
        {"lat": 37.9, "lon": 23.6, "sog": 0.2, "nav_status": 5, "ts": "2026-08-29T11:00:00Z"},
        {"lat": 37.8, "lon": 24.2, "sog": 8.0, "nav_status": 0, "ts": "2026-08-29T13:00:00Z"},
    ]
    assert _derive_recent_port_calls(track) == [{
        "port": "Piraeus",
        "arrived_at": "2026-08-29T10:00:00Z",
        "departed_at": "2026-08-29T13:00:00Z",
        "last_seen_at": "2026-08-29T11:00:00Z",
        "ais_fixes": 2,
        "evidence_level": "derived",
        "method": "ais_port_approach",
        "dwell_minutes": 60.0,
    }]


def test_fast_port_transit_is_not_reported_as_call(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.mda.reference.reference.in_port_or_anchorage",
        lambda lat, lon: "Valletta",
    )
    assert _derive_recent_port_calls([
        {"lat": 35.9, "lon": 14.5, "sog": 13.0, "nav_status": 0, "ts": "2026-08-29T10:00:00Z"},
    ]) == []


def test_single_slow_fix_is_not_a_port_call(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.mda.reference.reference.in_port_or_anchorage",
        lambda lat, lon: "Alexandria",
    )
    assert _derive_recent_port_calls([{
        "lat": 31.2, "lon": 29.9, "sog": 0.0, "nav_status": 5,
        "ts": "2026-09-18T10:00:00Z",
    }]) == []


def test_short_two_fix_stop_is_not_a_port_call(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.mda.reference.reference.in_port_or_anchorage",
        lambda lat, lon: "Alexandria",
    )
    assert _derive_recent_port_calls([
        {"lat": 31.2, "lon": 29.9, "sog": 0.0, "nav_status": 5, "ts": "2026-09-18T10:00:00Z"},
        {"lat": 31.2, "lon": 29.9, "sog": 0.0, "nav_status": 5, "ts": "2026-09-18T10:10:00Z"},
    ]) == []
