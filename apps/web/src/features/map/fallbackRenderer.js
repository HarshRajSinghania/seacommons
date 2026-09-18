import {
  PUBLIC_BASEMAP_LABEL_URL,
  PUBLIC_BASEMAP_TILE_URL,
  PUBLIC_SEAMARK_TILE_URL,
} from './publicBasemap.js';

function pointFeatures(features = []) {
  return (features || []).filter((feature) => {
    const coordinates = feature?.geometry?.type === 'Point' ? feature.geometry.coordinates : null;
    return Array.isArray(coordinates)
      && Number.isFinite(Number(coordinates[0]))
      && Number.isFinite(Number(coordinates[1]));
  });
}

function polygonFeatures(features = []) {
  return (features || []).filter((feature) => (
    ['Polygon', 'MultiPolygon'].includes(feature?.geometry?.type)
    && Array.isArray(feature.geometry.coordinates)
  ));
}

function leafletPolygonCoordinates(geometry) {
  const polygons = geometry.type === 'MultiPolygon' ? geometry.coordinates : [geometry.coordinates];
  return polygons.map((polygon) => polygon.map((ring) => ring.map(([lon, lat]) => [Number(lat), Number(lon)])));
}

function markerClass(feature) {
  const raw = String(feature?.properties?.incident_id || feature?.properties?.id || 'feature');
  const safe = raw.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
  return `seacommons-fallback-marker seacommons-fallback-marker--${safe || 'feature'}`;
}

function semanticColor(feature) {
  const p = feature?.properties || {};
  const domain = String(p.domain || p.macro_domain || p.maritime_domain || '').toLowerCase();
  const source = String(p.source || '').toLowerCase();
  const category = String(p.visual_category || p.signal_category || p.category || '').toLowerCase();
  if (domain === 'humanitarian' || source.includes('alarm')) return '#ff5a57';
  if (category.includes('navigation') || category.includes('casualty')) return '#ff9f43';
  if (category.includes('sanction') || category.includes('grey') || category.includes('spoof')) return '#f472b6';
  if (domain === 'security') return '#c084fc';
  return '#38bdf8';
}

function markerStyle(feature) {
  const color = semanticColor(feature);
  return {
    radius: 7,
    weight: 2,
    color: '#06151d',
    fillColor: color,
    fillOpacity: 0.96,
    className: markerClass(feature),
  };
}

function vesselHeading(feature) {
  const p = feature?.properties || {};
  const raw = p.latest_heading ?? p.heading_deg ?? p.heading ?? p.latest_course ?? p.course ?? p.cog ?? 0;
  const value = Number(raw);
  return Number.isFinite(value) ? ((value % 360) + 360) % 360 : 0;
}

function vesselIsStationary(feature) {
  const p = feature?.properties || {};
  if (p.motion_state) return p.motion_state !== 'moving';
  const raw = p.latest_sog ?? p.speed ?? p.sog;
  const speed = Number(raw);
  return !Number.isFinite(speed) || speed <= 0.5;
}

function vesselColor(feature) {
  const p = feature?.properties || {};
  const role = `${p.role || ''} ${p.operator_type || ''} ${p.fleet || ''} ${p.group || ''}`.toLowerCase();
  return /ngo|sar|rescue|civil/.test(role) ? '#34d399' : '#60a5fa';
}

function bindSelection(marker, feature, onFeatureSelect) {
  const element = marker.getElement?.();
  const id = String(feature?.properties?.incident_id || feature?.properties?.id || feature?.properties?.mmsi || 'feature');
  if (!element) {
    marker.on?.('click', () => onFeatureSelect?.(feature));
    return;
  }
  element.setAttribute('role', 'button');
  element.setAttribute('tabindex', '0');
  const isVessel = feature?.properties?.entity_kind === 'vessel' || feature?.properties?.report_type === 'vessel';
  element.setAttribute('aria-label', `${isVessel ? 'Open vessel' : 'Open incident'} ${id}`);
  let lastSelectionAt = 0;
  const select = (event) => {
    event?.stopPropagation?.();
    const now = Date.now();
    if (now - lastSelectionAt < 250) return;
    lastSelectionAt = now;
    onFeatureSelect?.(feature);
  };
  element.addEventListener('pointerup', select);
  element.addEventListener('click', select);
  element.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault?.();
      select(event);
    }
  });
}

function normalizePayload(payload) {
  if (Array.isArray(payload)) return { incidents: payload, vessels: [] };
  return { incidents: payload?.incidents || [], vessels: payload?.vessels || [] };
}

export async function createFallbackMap({ container, center, zoom, onFeatureSelect, leaflet }) {
  if (!leaflet) await import('leaflet/dist/leaflet.css');
  const module = leaflet || await import('leaflet');
  const L = module.default || module;
  const map = L.map(container, { zoomControl: true, attributionControl: false, preferCanvas: true });
  map.setView([Number(center[1]), Number(center[0])], zoom);
  // Incident geometry must remain a real DOM target for keyboard/pointer
  // interaction and accessibility even when the map prefers Canvas globally.
  const incidentRenderer = typeof L.svg === 'function' ? L.svg({ padding: 0.5 }) : undefined;
  const retinaTiles = { detectRetina: true, maxZoom: 19, maxNativeZoom: 16, crossOrigin: true };
  L.tileLayer(PUBLIC_BASEMAP_TILE_URL, { ...retinaTiles, maxNativeZoom: 19 }).addTo(map);
  if (PUBLIC_BASEMAP_LABEL_URL) {
    L.tileLayer(PUBLIC_BASEMAP_LABEL_URL, { ...retinaTiles, opacity: 0.9 }).addTo(map);
  }
  L.tileLayer(PUBLIC_SEAMARK_TILE_URL, { detectRetina: true, maxZoom: 19, maxNativeZoom: 18, opacity: 0.95, crossOrigin: true }).addTo(map);
  const markers = L.layerGroup().addTo(map);

  return {
    setFeatures(payload) {
      const { incidents, vessels } = normalizePayload(payload);
      markers.clearLayers();
      for (const feature of polygonFeatures(incidents)) {
        const polygons = leafletPolygonCoordinates(feature.geometry);
        for (const latlngs of polygons) {
          const color = semanticColor(feature);
          L.polygon(latlngs, {
            color, weight: 2, fillColor: color, fillOpacity: 0.22,
            className: markerClass(feature), renderer: incidentRenderer,
          }).on('click', () => onFeatureSelect?.(feature)).addTo(markers);
        }
      }
      for (const feature of pointFeatures(incidents)) {
        const [lon, lat] = feature.geometry.coordinates;
        const uncertaintyM = Number(feature.properties?.location_uncertainty_m || 0);
        const source = String(feature.properties?.coordinate_source || '');
        if (uncertaintyM > 20000 && source.startsWith('media_') && L.circle) {
          const color = semanticColor(feature);
          L.circle([Number(lat), Number(lon)], {
            radius: uncertaintyM,
            color,
            weight: 1,
            fillColor: color,
            fillOpacity: 0.12,
            className: `${markerClass(feature)} seacommons-fallback-uncertainty`,
            renderer: incidentRenderer,
          }).on('click', () => onFeatureSelect?.(feature)).addTo(markers);
        }
        const marker = L.circleMarker(
          [Number(lat), Number(lon)],
          { ...markerStyle(feature), renderer: incidentRenderer },
        ).addTo(markers);
        bindSelection(marker, feature, onFeatureSelect);
      }
      for (const feature of pointFeatures(vessels)) {
        const [lon, lat] = feature.geometry.coordinates;
        if (vesselIsStationary(feature) || !L.divIcon || !L.marker) {
          const marker = L.circleMarker([Number(lat), Number(lon)], {
            radius: 5, weight: 1.5, color: '#06151d', fillColor: vesselColor(feature), fillOpacity: 0.98,
            className: `${markerClass(feature)} seacommons-fallback-vessel-stationary`,
          }).addTo(markers);
          bindSelection(marker, feature, onFeatureSelect);
          continue;
        }
        const heading = vesselHeading(feature);
        const color = vesselColor(feature);
        const icon = L.divIcon({
          className: 'seacommons-fallback-vessel-icon',
          html: `<span class="seacommons-fallback-vessel-arrow" style="--vessel-heading:${heading}deg;--vessel-color:${color}"></span>`,
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        });
        const marker = L.marker([Number(lat), Number(lon)], { icon, keyboard: true }).addTo(markers);
        bindSelection(marker, feature, onFeatureSelect);
      }
    },
    fitFeatures(payload) {
      const { incidents, vessels } = normalizePayload(payload);
      const points = [...pointFeatures(incidents), ...pointFeatures(vessels)].map((feature) => {
        const [lon, lat] = feature.geometry.coordinates;
        return [Number(lat), Number(lon)];
      });
      if (points.length) map.fitBounds(L.latLngBounds(points), { padding: [28, 28], maxZoom: 9 });
    },
    flyTo({ center: nextCenter, zoom: nextZoom }) {
      map.flyTo([Number(nextCenter[1]), Number(nextCenter[0])], nextZoom);
    },
    resize() { map.invalidateSize(); },
    destroy() { map.remove(); },
  };
}
