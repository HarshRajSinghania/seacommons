# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Known SAR NGO and coastguard vessel database.

MMSIs verified against MarineTraffic / VesselFinder / vesseltracker /
Wikipedia public data (2024-2026). Mark any vessel with MMSI in this dict
as a priority track.

Field semantics:
  name   — vessel name
  org    — operating organisation
  flag   — current flag state (ISO 3166-1 alpha-3)
  role   — SAR / SAR-support / coastguard / surveillance
  operator_type — civil_ngo | state_authority (docs/deep-research-report.md
                  #28, docs/deep-research-report (2).md's "Civil SAR
                  Registry": a state coastguard/navy vessel must never be
                  presented to a viewer as an "NGO", however useful it is
                  to treat it as a known SAR responder internally)
  imo    — IMO number (where known)
  callsign — AIS callsign (where known)
  status — active | retired | unverified

is_ngo(mmsi) intentionally stays broader than "civil NGO" -- it means "known
SAR responder registry membership", and every detector that calls it (AIS
spike rescue-cluster grouping, NGO search-pattern rule, distress-response
intercept scoring) is correct to also weight a coastguard vessel the same
way a civil NGO vessel is weighted; narrowing it would silently regress
those. Use is_civil_ngo(mmsi) instead wherever "NGO" is a public identity
claim about the vessel, not just "responder worth tracking" -- currently
only ngo_vessel_geojson()'s vessel_class/org labelling, the one place both
audits found a coastguard vessel rendered on a public/operator panel
tagged exactly like a civil NGO asset.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ── NGO SAR Fleet (verified 2026) ─────────────────────────────────────────────
NGO_VESSELS: dict[str, dict[str, Any]] = {
    # SOS Méditerranée
    "258479000": {"name": "Ocean Viking",  "org": "SOS Méditerranée",     "flag": "NOR", "role": "SAR",
                  "imo": "8506854", "callsign": "JXIW3", "status": "active"},
    # MSF — Geo Barents chartered 2021–2024, SAR ops halted Dec 2024 (Piantedosi decree)
    "258826000": {"name": "Geo Barents",   "org": "MSF",                  "flag": "NOR", "role": "SAR",
                  "imo": "9252503", "callsign": "LAKK6", "status": "retired"},
    # MSF — new vessel since Nov 2025 (replaces Geo Barents); ex-Norwegian ambulance boat.
    # VesselFinder still lists the ex-Norwegian MMSI 257326900 — monitor both.
    "211180740": {"name": "Oyvon",         "org": "MSF",                  "flag": "DEU", "role": "SAR",
                  "status": "active", "note": "ex-NOR MMSI 257326900"},
    "257326900": {"name": "Oyvon (ex-NOR)", "org": "MSF",                 "flag": "NOR", "role": "SAR",
                  "status": "unverified"},
    # Open Arms
    "224772000": {"name": "Open Arms",     "org": "Proactiva Open Arms",  "flag": "ESP", "role": "SAR",
                  "imo": "7325887", "callsign": "EGGX", "status": "active"},
    "235105994": {"name": "Astral",        "org": "Proactiva Open Arms",  "flag": "GBR", "role": "SAR-support",
                  "status": "unverified"},
    # Sea Watch
    "211879870": {"name": "Sea Watch 5",   "org": "Sea Watch",            "flag": "DEU", "role": "SAR",
                  "imo": "9421556", "callsign": "DCDR2", "status": "active"},
    "211883350": {"name": "Aurora",        "org": "Sea Watch",            "flag": "DEU", "role": "SAR",
                  "callsign": "DJVX2", "status": "active"},
    # Sea-Eye
    "218049720": {"name": "Sea-Eye 5",     "org": "Sea-Eye",              "flag": "DEU", "role": "SAR",
                  "callsign": "DIZS2", "status": "active"},
    "211428870": {"name": "Mediterranea",  "org": "Mediterranea Saving Humans", "flag": "DEU", "role": "SAR",
                  "imo": "7214753", "callsign": "DHUQ2", "status": "active", "note": "ex-Sea-Eye 4"},
    "247536000": {"name": "Mare Jonio",    "org": "Mediterranea Saving Humans", "flag": "ITA", "role": "SAR",
                  "imo": "7222669", "status": "active"},
    # Salvamento Marítimo Humanitario
    "224069840": {"name": "Aita Mari",     "org": "Salvamento Marítimo Humanitario", "flag": "ESP", "role": "SAR",
                  "imo": "9248851", "callsign": "EBXC", "status": "active"},
    # SOS Humanity (ex-Sea-Watch 4, same hull IMO 9704918)
    "257091000": {"name": "Humanity 1",    "org": "SOS Humanity",         "flag": "NOR", "role": "SAR",
                  "imo": "9704918", "status": "unverified"},
    # Louise Michel (Banksy-funded collective)
    "211322990": {"name": "Louise Michel", "org": "Louise Michel",        "flag": "DEU", "role": "SAR",
                  "callsign": "DCXD", "status": "active"},
    # EMERGENCY ONG
    "352001404": {"name": "Life Support",  "org": "EMERGENCY",            "flag": "PAN", "role": "SAR",
                  "imo": "9250206", "callsign": "HOA7461", "status": "active"},
    # ResQship (sailing monitoring vessel)
    "211472620": {"name": "Nadir",         "org": "ResQship",             "flag": "DEU", "role": "SAR-support",
                  "callsign": "DFQD2", "status": "active"},

    # ── Coastguards (major responders) ────────────────────────────────────────
    # Guardia Costiera (Italian Coast Guard) flagship vessels
    "247330700": {"name": "Diciotti",         "org": "Guardia Costiera ITA", "flag": "ITA", "role": "coastguard",
                  "imo": "9690420", "callsign": "IHEW", "status": "active"},
    "247330500": {"name": "Dattilo",          "org": "Guardia Costiera ITA", "flag": "ITA", "role": "coastguard",
                  "callsign": "IGUB", "status": "active"},
    "247329600": {"name": "Bruno Gregoretti", "org": "Guardia Costiera ITA", "flag": "ITA", "role": "coastguard",
                  "imo": "9655523", "callsign": "IGSD", "status": "active"},
    # Malta Armed Forces
    "249165000": {"name": "P61 Diciotti",     "org": "Armed Forces of Malta", "flag": "MLT", "role": "coastguard",
                  "imo": "4594920", "status": "unverified"},
}

# operator_type is derived from role rather than hand-set per entry above --
# every current "coastguard"-role record is a state asset, and deriving it
# means a future entry can't be added with role="coastguard" while still
# defaulting to civil_ngo by omission.
for _info in NGO_VESSELS.values():
    _info.setdefault(
        "operator_type",
        "state_authority" if _info.get("role") == "coastguard" else "civil_ngo",
    )

# ── Vessels to monitor but with unconfirmed MMSI (kept out of NGO_VESSELS so
#    they never cause a false NGO tag). Tracked manually / via registry name. ──
UNCONFIRMED_MMSI: dict[str, dict[str, Any]] = {
    # Italian Navy Cassiopea-class OPVs (pennant P401–P404) — Mare Nostrum-era
    # migration patrol, no reliable public MMSI.
    "Cassiopea (P401)": {"org": "Marina Militare ITA", "role": "surveillance"},
    "Libra (P402)":     {"org": "Marina Militare ITA", "role": "surveillance"},
    "Spica (P403)":     {"org": "Marina Militare ITA", "role": "surveillance"},
    "Vega (P404)":      {"org": "Marina Militare ITA", "role": "surveillance"},
    # Frontex surveillance assets (chartered, rotating) — no fixed MMSI.
    "Derfflinger":      {"org": "Frontex",             "role": "surveillance"},
}

# Twitter handles for social monitoring (no @ prefix)
NGO_TWITTER_HANDLES = [
    "alarm_phone",       # Alarm Phone — public distress and SAR reports
    "MSF_Sea",           # MSF Sea Rescue
    "openarms_fund",     # Open Arms
    "SOSMedIntl",        # SOS Méditerranée international
    "seawatchcrew",      # Sea Watch crew reports
    "SOShumanity",       # SOS Humanity
    "seaeyeorg",         # Sea-Eye
    "ResQship",          # ResQship
    "emergency_ong",     # Emergency ONG
    "watchthemed",       # Watch The Med (civilian monitoring network)
    "InfoMigrants",      # InfoMigrants (multilingual news)
    "UNmigration",       # IOM / UN Migration
    "UNHCRItalia",       # UNHCR Italy
]

_MMSI_SET = set(NGO_VESSELS.keys())


def is_ngo(mmsi: str) -> bool:
    """Known SAR responder registry membership -- civil NGO AND coastguard.

    Deliberately broad: every caller (rescue-cluster grouping, the NGO
    search-pattern rule, distress-response intercept scoring) treats a
    coastguard vessel as just as meaningful a responder as a civil NGO one.
    Use is_civil_ngo() where "NGO" is a public identity claim, not a
    responder-relevance check.
    """
    return str(mmsi) in _MMSI_SET


def is_civil_ngo(mmsi: str) -> bool:
    """True only for a civil NGO asset -- never a state coastguard/navy one."""
    info = NGO_VESSELS.get(str(mmsi))
    return info is not None and info.get("operator_type") == "civil_ngo"


def get_ngo_info(mmsi: str) -> dict[str, Any] | None:
    return NGO_VESSELS.get(str(mmsi))


def ngo_mmsi_set() -> frozenset[str]:
    return frozenset(_MMSI_SET)


def _position_age_seconds(last_seen: Any, *, now: datetime | None = None) -> float | None:
    if not last_seen:
        return None
    try:
        observed = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(
        0.0,
        (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds(),
    )


def _position_status(last_seen: Any, *, now: datetime | None = None) -> str:
    if not last_seen:
        return "offline"
    try:
        observed = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "offline"
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age_s = max(0.0, (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds())
    if age_s <= 10 * 60:
        return "live"
    if age_s <= 60 * 60:
        return "stale"
    return "offline"


def ngo_vessel_geojson() -> dict[str, Any]:
    """Operational NGO/coastguard vessel inventory enriched from the registry.

    This projection is for authenticated operator surfaces. Public Live stays
    case/activity-first and does not expose the complete fleet inventory.
    """
    from core.vessels.registry import registry  # lazy to avoid circular import

    geojson = registry.get_last_known_geojson(ngo_mmsi_set())
    ngo_features = []
    positioned_mmsi: set[str] = set()
    live_mmsi: set[str] = set()
    stale_mmsi: set[str] = set()

    for feat in geojson.get("features", []):
        props = feat.get("properties") or {}
        mmsi = str(props.get("mmsi", ""))
        if not is_ngo(mmsi):
            continue
        info = get_ngo_info(mmsi) or {}
        positioned_mmsi.add(mmsi)
        operator_type = info.get("operator_type", "civil_ngo")
        position_age_s = _position_age_seconds(props.get("last_seen"))
        ais_status = _position_status(props.get("last_seen"))
        if ais_status == "live":
            live_mmsi.add(mmsi)
        elif ais_status == "stale":
            stale_mmsi.add(mmsi)
        ngo_features.append({
            **feat,
            # Old AIS fixes remain in the fleet inventory as history/status,
            # but only genuinely fresh positions are allowed onto a map.
            "geometry": feat.get("geometry") if ais_status == "live" else None,
            "properties": {
                **props,
                # Canonical registry identity wins over a stale AIS display
                # name. The persisted coordinates remain explicitly timestamped.
                "ship_name": info.get("name") or props.get("ship_name") or mmsi,
                "flag": info.get("flag", ""),
                "intel_type": "ngo_vessel",
                "org": info.get("org", ""),
                "role": info.get("role", ""),
                "ais_status": ais_status,
                "position_age_s": (
                    round(position_age_s, 1) if position_age_s is not None else None
                ),
                "position_policy": (
                    "current" if ais_status == "live" else "stale_withheld"
                ),
                "operator_type": operator_type,
                "vessel_class": "ngo" if operator_type == "civil_ngo" else "coastguard",
            },
        })

    # Vessels never observed by our registry remain in the fleet list, but
    # without invented coordinates.
    for mmsi, info in NGO_VESSELS.items():
        if mmsi in positioned_mmsi:
            continue
        operator_type = info.get("operator_type", "civil_ngo")
        ngo_features.append({
            "type": "Feature",
            "geometry": None,
            "properties": {
                "mmsi": mmsi,
                "ship_name": info.get("name", ""),
                "org": info.get("org", ""),
                "role": info.get("role", ""),
                "flag": info.get("flag", ""),
                "intel_type": "ngo_vessel",
                "ais_status": "offline",
                "position_policy": "never_seen",
                "operator_type": operator_type,
                "vessel_class": "ngo" if operator_type == "civil_ngo" else "coastguard",
            },
        })

    civil_ngo_count = sum(
        1 for info in NGO_VESSELS.values() if info.get("operator_type") == "civil_ngo"
    )
    try:
        from core.vessels.aisstream import get_ngo_client

        tracker = get_ngo_client()
        tracker_health = tracker.health() if tracker else None
        tracking_stream = {
            "connected": bool(tracker and tracker.connected),
            "messages_received": int(tracker.messages_received) if tracker else 0,
            "last_message_at": (
                tracker_health.last_message_at.isoformat()
                if tracker_health and tracker_health.last_message_at
                else None
            ),
            "scope": "global_mmsi",
        }
    except Exception:
        tracking_stream = {
            "connected": False,
            "messages_received": 0,
            "last_message_at": None,
            "scope": "global_mmsi",
        }

    return {
        "type": "FeatureCollection",
        "features": ngo_features,
        "meta": {
            "total_registered": len(NGO_VESSELS),
            "civil_ngo_registered": civil_ngo_count,
            "state_authority_registered": len(NGO_VESSELS) - civil_ngo_count,
            "live_ais": len(live_mmsi),
            "stale_ais": len(stale_mmsi),
            "last_known_position": len(positioned_mmsi),
            "never_seen": len(NGO_VESSELS) - len(positioned_mmsi),
            "offline": len(NGO_VESSELS) - len(live_mmsi) - len(stale_mmsi),
            "map_position_policy": "live_only_10m",
            "tracking_stream": tracking_stream,
        },
    }
