import { publicBasemapSource } from '../map/publicBasemap.js';

import { isVesselArchiveIncident } from '../map/vesselMarker.js';

function parseTime(value) {
  const ms = Date.parse(String(value || ''));
  return Number.isFinite(ms) ? ms : Number.POSITIVE_INFINITY;
}

export function normalizeTimeline(items = []) {
  return [...items]
    .filter((item) => item && item.at)
    .sort((a, b) => parseTime(a.at) - parseTime(b.at))
    .map((item, frameIndex) => ({ ...item, frameIndex }));
}

export function selectFrame(timeline = [], index = 0) {
  if (!timeline.length) return { index: -1, item: null, geometry: null };
  const clamped = Math.max(0, Math.min(timeline.length - 1, Number(index) || 0));
  const item = timeline[clamped];
  let geometry = item.geometry || null;
  if (!geometry) {
    for (let i = clamped - 1; i >= 0; i -= 1) {
      if (timeline[i]?.geometry) {
        geometry = timeline[i].geometry;
        break;
      }
    }
  }
  return { index: clamped, item, geometry };
}

export function selectSatelliteObservation(timeline = [], at) {
  const target = parseTime(at);
  const candidates = timeline.filter((item) => item?.type === 'satellite');
  if (!candidates.length) return null;
  const before = candidates
    .filter((item) => parseTime(item.at) <= target)
    .sort((a, b) => parseTime(b.at) - parseTime(a.at));
  if (before.length) return before[0];
  return [...candidates].sort((a, b) => parseTime(a.at) - parseTime(b.at))[0];
}


export function selectPreferredSatelliteObservation(timeline = [], at = null, preferredMission = 'auto') {
  const target = parseTime(at);
  const candidates = timeline
    .filter((item) => item?.type === 'satellite')
    .filter((item) => !Number.isFinite(target) || parseTime(item?.at) <= target);
  if (!candidates.length) return null;

  const newest = (items) => [...items].sort((a, b) => parseTime(b?.at) - parseTime(a?.at))[0] || null;
  if (preferredMission && preferredMission !== 'auto') {
    return newest(candidates.filter((item) => item?.properties?.mission === preferredMission));
  }

  const priorities = [
    (mission) => mission === 'Sentinel-1',
    (mission) => mission === 'Sentinel-2',
    (mission) => mission === 'Sentinel-3',
    (mission) => String(mission || '').startsWith('VIIRS'),
  ];
  for (const matches of priorities) {
    const selected = newest(candidates.filter((item) => matches(item?.properties?.mission)));
    if (selected) return selected;
  }
  return newest(candidates);
}

export function statusLabel(status) {
  const value = String(status || '').toLowerCase();
  if (value === 'needs_review') return 'NEEDS REVIEW';
  if (value === 'outcome_unknown' || value === 'archived') return 'OUTCOME UNKNOWN';
  if (value === 'resolved') return 'RESOLVED';
  if (value === 'active') return 'ACTIVE';
  return value ? value.replaceAll('_', ' ').toUpperCase() : 'UNKNOWN';
}

export function satelliteRasterDescriptor(observation) {
  const props = observation?.properties || {};
  const asset = String(props.asset_ref || '');
  if (!asset || (!asset.startsWith('https://') && !asset.startsWith('http://'))) return null;
  if (asset.includes('{z}') && asset.includes('{x}') && asset.includes('{y}')) {
    return { type: 'raster', tiles: [asset], tileSize: 256 };
  }
  const bbox = Array.isArray(props.bbox) ? props.bbox.map(Number) : null;
  const looksLikeImage = /\.(?:png|jpe?g|webp)(?:\?|$)/i.test(asset);
  const copernicusThumbnail = observation?.source === 'copernicus_dataspace'
    && /\/odata\/v1\/Assets\([^)]*\)\/\$value(?:\?|$)/i.test(asset);
  if ((!looksLikeImage && !copernicusThumbnail) || bbox?.length !== 4 || bbox.some((value) => !Number.isFinite(value))) return null;
  const [west, south, east, north] = bbox;
  return {
    type: 'image',
    url: asset,
    coordinates: [[west, north], [east, north], [east, south], [west, south]],
  };
}

export function incidentsAtCutoff(incidents = [], cutoff = null) {
  if (!cutoff) return [...incidents];
  const cutoffMs = parseTime(cutoff);
  return incidents.filter((incident) => parseTime(incident?.reported_at) <= cutoffMs);
}

export function timelineAtCutoff(timeline = [], cutoff = null) {
  if (!cutoff) return [...timeline];
  const cutoffMs = parseTime(cutoff);
  return timeline.filter((item) => parseTime(item?.at) <= cutoffMs);
}

export function resolveGlobalTimelinePosition(incidents = [], position = 1000, max = 1000) {
  const times = incidents.map((incident) => parseTime(incident?.reported_at)).filter(Number.isFinite);
  if (!times.length || Number(position) >= Number(max)) return { mode: 'all', cutoff: null };
  const min = Math.min(...times);
  const latest = Math.max(...times);
  const ratio = Math.max(0, Math.min(1, Number(position) / Number(max || 1)));
  return { mode: 'temporal', cutoff: new Date(min + ((latest - min) * ratio)).toISOString() };
}

export function incidentCollection(incidents = [], cutoff = null) {
  return {
    type: 'FeatureCollection',
    features: incidentsAtCutoff(incidents, cutoff)
      .filter((incident) => incident?.geometry?.type === 'Point')
      .map((incident) => ({
        type: 'Feature',
        geometry: incident.geometry,
        properties: {
          incident_id: incident.incident_id,
          incident_status: incidentStatusAtCutoff(incident, cutoff),
          case_type: incident.case_type || 'incident',
          source: incident.source || '',
          domain: incident.domain || 'humanitarian',
          reported_at: incident.reported_at || '',
          title: incident.title || '',
          marker_kind: isVesselArchiveIncident(incident) ? 'vessel' : 'incident',
          course: Number.isFinite(Number(incident.course)) ? Number(incident.course) : 0,
        },
      })),
  };
}


export function incidentStatusAtCutoff(incident, cutoff = null) {
  const current = incident?.incident_status || 'outcome_unknown';
  if (!cutoff) return current;
  const cutoffMs = parseTime(cutoff);
  const history = [...(incident?.status_history || [])]
    .filter((item) => Number.isFinite(parseTime(item?.at)))
    .sort((a, b) => parseTime(a.at) - parseTime(b.at));
  let effective = null;
  for (const transition of history) {
    const at = parseTime(transition.at);
    if (at <= cutoffMs) effective = transition.to_state || effective;
    else if (effective == null) return transition.from_state || current;
    else break;
  }
  return effective || current;
}


export function playMapStyle(day = new Date(Date.now() - 24 * 3600 * 1000).toISOString().slice(0, 10)) {
  return {
    version: 8,
    sources: {
      baseMap: publicBasemapSource(),
      satelliteContext: {
        type: 'raster',
        tiles: [`https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_SNPP_CorrectedReflectance_TrueColor/default/${day}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg`],
        tileSize: 256,
        attribution: 'NASA EOSDIS GIBS / VIIRS',
      },
    },
    layers: [
      { id: 'base-map', type: 'raster', source: 'baseMap', paint: { 'raster-opacity': 1 } },
      { id: 'satellite-context', type: 'raster', source: 'satelliteContext', paint: { 'raster-opacity': 1, 'raster-fade-duration': 120 } },
    ],
  };
}


export function mergeIncidentPages(previous = [], incoming = []) {
  const byId = new Map((previous || []).map((item) => [item.incident_id, item]));
  for (const item of incoming || []) {
    if (item?.incident_id) byId.set(item.incident_id, item);
  }
  return [...byId.values()].sort((a, b) => {
    const aAt = parseTime(a?.last_update_at || a?.reported_at);
    const bAt = parseTime(b?.last_update_at || b?.reported_at);
    return (Number.isFinite(bAt) ? bAt : 0) - (Number.isFinite(aAt) ? aAt : 0);
  });
}


export function satelliteFootprintCollection(timeline = []) {
  return {
    type: 'FeatureCollection',
    features: timeline
      .filter((item) => item?.type === 'satellite' && item?.source === 'copernicus_dataspace' && item?.geometry)
      .map((item) => ({
        type: 'Feature',
        geometry: item.geometry,
        properties: {
          observation_id: item.id,
          mission: item.properties?.mission || 'Copernicus',
          sensor_type: item.properties?.sensor_type || 'satellite',
          temporal_relation: item.properties?.temporal_relation || 'nearest',
        },
      })),
  };
}
