# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geographic reference data for the MDA engine.

Subsea cables, pipelines, offshore platforms, ports, anchorages / STS zones,
chokepoints, EEZ boundaries and marine protected areas — the geometry every
grey-zone detector needs (loitering near a cable, STS not in a port, a gap that
starts at an EEZ line, fishing inside an MPA).

Ships with a bundled fallback (`core/data/reference/med_reference.json`,
approximate but real) so it works offline and in tests. `refresh()` pulls the
authoritative open datasets (EMODnet Human Activities, Marine Regions EEZ,
WDPA/Protected Planet, submarinecablemap) when the network is available — it is
called on an interval by the scheduler and is a no-op if a download fails.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from shapely.geometry import LineString, Point, Polygon, shape  # type: ignore[import-untyped]
from shapely.strtree import STRtree  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parents[1] / "data" / "reference"
_BUNDLE = _DIR / "med_reference.json"
_LAND_BUNDLE = _DIR / "med_land_lowres.geojson"
_REFRESHED = _DIR / "refreshed"  # datasets pulled by refresh() land here as *.geojson

_NM_PER_DEG = 60.0


def _nm(km: float) -> float:
    return km / 1.852


@dataclass
class InfraHit:
    kind: str          # cable | pipeline | platform | mpa | sts_zone
    name: str
    distance_km: float
    detail: dict[str, Any] = field(default_factory=dict)
    # distance_km is 0.0 both when the point sits exactly on a line/point
    # geometry AND when it's anywhere inside an area geometry (sts_zone, mpa)
    # -- those read very differently to a human ("within 0.0 km" vs.
    # "inside"), so callers building user-facing text need this instead of
    # inferring it from distance_km == 0.0.
    inside: bool = False


@dataclass
class _Feature:
    kind: str
    name: str
    geom: Any
    detail: dict[str, Any]


class ReferenceIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._features: list[_Feature] = []
        self._infra_features: list[_Feature] = []   # cables + pipelines + platforms + sts_zones
        self._tree: Optional[STRtree] = None
        self._infra_tree: Optional[STRtree] = None
        self._ports: list[tuple[float, float, str, float]] = []   # lon, lat, name, radius_nm
        self._chokepoints: list[dict[str, Any]] = []
        self._land_geom: Any = None
        self.load()

    # ── build ────────────────────────────────────────────────────────────────

    def load(self) -> None:
        feats: list[_Feature] = []
        try:
            data = json.loads(_BUNDLE.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover
            logger.warning("reference: bundle load failed: %s", exc)
            data = {}

        for c in data.get("cables", []):
            geom = LineString([(lon, lat) for lat, lon in c["waypoints"]])
            feats.append(_Feature("cable", c["name"], geom, {"operator": c.get("operator")}))
        for p in data.get("pipelines", []):
            geom = LineString([(lon, lat) for lat, lon in p["waypoints"]])
            feats.append(_Feature("pipeline", p["name"], geom,
                                  {"operator": p.get("operator"), "product": p.get("product")}))
        for lon, lat, name, country in data.get("platforms", []):
            feats.append(_Feature("platform", name, Point(lon, lat), {"country": country}))
        for z in data.get("sts_zones", []):
            geom = Polygon([(lon, lat) for lat, lon in z["poly"]])
            feats.append(_Feature("sts_zone", z["name"], geom, {"note": z.get("note")}))

        ports = [(lon, lat, name, float(r_nm)) for lon, lat, name, r_nm in data.get("ports", [])]
        chokepoints = list(data.get("chokepoints", []))
        land_geom = None
        try:
            land_data = json.loads(_LAND_BUNDLE.read_text(encoding="utf-8"))
            land_features = [shape(feat["geometry"]) for feat in land_data.get("features", []) if feat.get("geometry")]
            if land_features:
                from shapely.ops import unary_union  # type: ignore[import-untyped]
                land_geom = unary_union(land_features)
        except Exception as exc:  # pragma: no cover
            logger.warning("reference: coastline bundle load failed: %s", exc)

        # merge any refreshed datasets (EEZ, MPA, real cables/pipelines)
        refreshed = self._load_refreshed()

        with self._lock:
            self._features = feats + refreshed
            self._infra_features = [f for f in self._features
                                    if f.kind in ("cable", "pipeline", "platform", "sts_zone", "mpa")]
            self._ports = ports
            self._chokepoints = chokepoints
            self._land_geom = land_geom
            self._tree = STRtree([f.geom for f in self._features]) if self._features else None
            self._infra_tree = (STRtree([f.geom for f in self._infra_features])
                                if self._infra_features else None)
        logger.info("reference: %d features (%d infra), %d ports, %d chokepoints",
                    len(self._features), len(self._infra_features), len(ports), len(chokepoints))

    def _load_refreshed(self) -> list[_Feature]:
        out: list[_Feature] = []
        if not _REFRESHED.is_dir():
            return out
        for path in _REFRESHED.glob("*.geojson"):
            kind = path.stem.split("_")[0]  # eez_*.geojson -> "eez"
            try:
                gj = json.loads(path.read_text(encoding="utf-8"))
                for feat in gj.get("features", []):
                    try:
                        geom = shape(feat["geometry"])
                    except Exception:
                        continue
                    props = feat.get("properties", {}) or {}
                    name = props.get("name") or props.get("NAME") or props.get("GEONAME") or path.stem
                    out.append(_Feature(kind, str(name), geom, props))
            except Exception as exc:  # pragma: no cover
                logger.warning("reference: refreshed %s load failed: %s", path.name, exc)
        return out

    # ── queries ──────────────────────────────────────────────────────────────

    def nearest_infrastructure(self, lat: float, lon: float, max_km: float = 25.0) -> Optional[InfraHit]:
        with self._lock:
            tree, feats = self._infra_tree, list(self._infra_features)
        if tree is None:
            return None
        pt = Point(lon, lat)
        best: Optional[InfraHit] = None
        for idx in tree.query(pt.buffer(_deg_for_km(max_km, lat))):
            f = feats[int(idx)]
            d_km = _geom_distance_km(pt, f.geom, lat)
            if d_km <= max_km and (best is None or d_km < best.distance_km):
                best = InfraHit(f.kind, f.name, round(d_km, 2), f.detail, inside=f.geom.contains(pt))
        return best

    def infrastructure_within(self, lat: float, lon: float, km: float) -> list[InfraHit]:
        with self._lock:
            tree, feats = self._infra_tree, list(self._infra_features)
        if tree is None:
            return []
        pt = Point(lon, lat)
        hits: list[InfraHit] = []
        for idx in tree.query(pt.buffer(_deg_for_km(km, lat))):
            f = feats[int(idx)]
            d_km = _geom_distance_km(pt, f.geom, lat)
            if d_km <= km:
                hits.append(InfraHit(f.kind, f.name, round(d_km, 2), f.detail, inside=f.geom.contains(pt)))
        return sorted(hits, key=lambda h: h.distance_km)

    def in_sts_zone(self, lat: float, lon: float) -> Optional[str]:
        pt = Point(lon, lat)
        with self._lock:
            for f in self._features:
                if f.kind == "sts_zone" and f.geom.contains(pt):
                    return f.name
        return None

    def in_mpa(self, lat: float, lon: float) -> Optional[str]:
        pt = Point(lon, lat)
        with self._lock:
            for f in self._features:
                if f.kind == "mpa" and f.geom.contains(pt):
                    return f.name
        return None

    def nearest_port_km(self, lat: float, lon: float) -> tuple[Optional[str], float]:
        with self._lock:
            ports = list(self._ports)
        best_name, best_km = None, float("inf")
        for p_lon, p_lat, name, _r in ports:
            d = _haversine_km(lat, lon, p_lat, p_lon)
            if d < best_km:
                best_name, best_km = name, d
        return best_name, round(best_km, 1)

    def distance_from_coast_km(self, lat: float, lon: float) -> Optional[float]:
        """Approximate distance to the nearest coastline from bundled Natural Earth land.

        This is contextual evidence only. Low-resolution land geometry is intentionally
        deterministic/offline and must never be treated as a navigational product.
        """
        with self._lock:
            land = self._land_geom
        if land is None:
            return None
        pt = Point(lon, lat)
        if land.contains(pt):
            return 0.0
        return round(_geom_distance_km(pt, land.boundary, lat), 1)

    def is_land(self, lat: float, lon: float) -> bool:
        with self._lock:
            land = self._land_geom
        return bool(land is not None and land.contains(Point(lon, lat)))

    def in_port_or_anchorage(self, lat: float, lon: float) -> Optional[str]:
        """True (port name) when the point is inside a port's approach radius —
        used to exclude normal anchorage ops from STS / loiter alerts."""
        with self._lock:
            ports = list(self._ports)
        for p_lon, p_lat, name, r_nm in ports:
            if _haversine_km(lat, lon, p_lat, p_lon) <= r_nm * 1.852:
                return name
        return None

    def chokepoint_of(self, lat: float, lon: float) -> Optional[dict[str, Any]]:
        with self._lock:
            for cp in self._chokepoints:
                min_lon, min_lat, max_lon, max_lat = cp["bbox"]
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    return cp
        return None

    def chokepoints(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._chokepoints)

    def to_geojson(self, kinds: Optional[set[str]] = None) -> dict[str, Any]:
        from shapely.geometry import mapping
        with self._lock:
            feats = list(self._features)
        out = []
        for f in feats:
            if kinds and f.kind not in kinds:
                continue
            out.append({"type": "Feature", "geometry": mapping(f.geom),
                        "properties": {"kind": f.kind, "name": f.name, **f.detail}})
        return {"type": "FeatureCollection", "features": out}

    # ── refresh from open datasets ───────────────────────────────────────────

    def refresh(self) -> dict[str, Any]:
        """Best-effort pull of the authoritative open layers. Never raises."""
        _REFRESHED.mkdir(parents=True, exist_ok=True)
        results: dict[str, Any] = {}
        for name, fn in (("eez", _fetch_eez), ("mpa", _fetch_mpa),
                         ("pipeline", _fetch_pipelines), ("platform", _fetch_platforms)):
            try:
                gj = fn()
                if gj and gj.get("features"):
                    (_REFRESHED / f"{name}_open.geojson").write_text(json.dumps(gj), encoding="utf-8")
                    results[name] = len(gj["features"])
            except Exception as exc:
                logger.info("reference.refresh %s skipped: %s", name, exc)
                results[name] = f"skip: {exc}"
        self.load()
        return results


# ── distance helpers (planar-ish, fine at Med scale) ─────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2)
    return r * 2 * math.asin(math.sqrt(max(0.0, a)))


def _deg_for_km(km: float, lat: float) -> float:
    return km / (111.0 * max(math.cos(math.radians(lat)), 0.2))


def _geom_distance_km(pt: Point, geom: Any, lat: float) -> float:
    if geom.contains(pt):
        return 0.0
    deg = pt.distance(geom)  # planar degrees
    cos_lat = max(math.cos(math.radians(lat)), 0.2)
    # rough: scale by an average of the lon/lat metre-per-degree
    return deg * 111.0 * ((1 + cos_lat) / 2)


# ── open-dataset fetchers (used by refresh(); may fail offline) ──────────────

def _fetch_eez() -> dict[str, Any]:
    import httpx
    # Marine Regions EEZ v12 WFS — Med + Black Sea bbox
    url = ("https://geo.vliz.be/geoserver/MarineRegions/wfs"
           "?service=WFS&version=2.0.0&request=GetFeature"
           "&typeName=MarineRegions:eez&outputFormat=application/json&count=120"
           "&bbox=-10,28,45,48,EPSG:4326")
    r = httpx.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def _fetch_mpa() -> dict[str, Any]:
    import os

    import httpx
    token = os.environ.get("WDPA_TOKEN", "")
    if not token:
        raise RuntimeError("no WDPA_TOKEN")
    url = ("https://api.protectedplanet.net/v3/protected_areas/search"
           f"?token={token}&marine=true&with_geometry=true&per_page=50"
           "&geo_type=region&region=mediterranean")
    r = httpx.get(url, timeout=60)
    r.raise_for_status()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": pa.get("geojson", {}).get("geometry"),
         "properties": {"name": pa.get("name"), "wdpa_id": pa.get("wdpa_id")}}
        for pa in r.json().get("protected_areas", []) if pa.get("geojson")
    ]}


def _fetch_pipelines() -> dict[str, Any]:
    import httpx
    url = ("https://ows.emodnet-humanactivities.eu/wfs"
           "?service=WFS&version=2.0.0&request=GetFeature&typeName=pipelines"
           "&outputFormat=application/json&count=2000&srsName=EPSG:4326"
           "&bbox=-10,28,45,48,EPSG:4326")
    r = httpx.get(url, timeout=90)
    r.raise_for_status()
    return r.json()


def _fetch_platforms() -> dict[str, Any]:
    import httpx
    url = ("https://ows.emodnet-humanactivities.eu/wfs"
           "?service=WFS&version=2.0.0&request=GetFeature&typeName=platforms"
           "&outputFormat=application/json&count=2000&srsName=EPSG:4326"
           "&bbox=-10,28,45,48,EPSG:4326")
    r = httpx.get(url, timeout=90)
    r.raise_for_status()
    return r.json()


reference = ReferenceIndex()
