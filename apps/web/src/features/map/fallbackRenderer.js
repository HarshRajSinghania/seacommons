import { PUBLIC_BASEMAP_TILE_URL } from './publicBasemap.js';

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

function markerStyle(feature) {
  const domain = String(feature?.properties?.domain || feature?.properties?.macro_domain || '').toLowerCase();
  const source = String(feature?.properties?.source || '').toLowerCase();
  const humanitarian = domain === 'humanitarian' || source.includes('alarm');
  return {
    radius: 7,
    weight: 2,
    color: humanitarian ? '#ff746f' : '#8ed8ff',
    fillColor: humanitarian ? '#ff746f' : '#8ed8ff',
    fillOpacity: 0.9,
    className: markerClass(feature),
  };
}
export async function createFallbackMap({ container, center, zoom, onFeatureSelect, leaflet }) {
  if (!leaflet) await import('leaflet/dist/leaflet.css');
  const module = leaflet || await import('leaflet');
  const L = module.default || module;
  const map = L.map(container, { zoomControl: true, attributionControl: false });
  map.setView([Number(center[1]), Number(center[0])], zoom);
  L.tileLayer(PUBLIC_BASEMAP_TILE_URL, { maxZoom: 19 }).addTo(map);
  const markers = L.layerGroup().addTo(map);

  return {
    setFeatures(features) {
      markers.clearLayers();
      for (const feature of polygonFeatures(features)) {
        const polygons = leafletPolygonCoordinates(feature.geometry);
        for (const latlngs of polygons) {
          L.polygon(latlngs, {
            color: '#ff746f', weight: 2, fillColor: '#ff746f', fillOpacity: 0.22,
            className: markerClass(feature),
          }).on('click', () => onFeatureSelect?.(feature)).addTo(markers);
        }
      }
      for (const feature of pointFeatures(features)) {
        const [lon, lat] = feature.geometry.coordinates;
        const marker = L.circleMarker([Number(lat), Number(lon)], markerStyle(feature))
          .addTo(markers);
        const element = marker.getElement?.();
        if (element) {
          const incidentId = String(feature?.properties?.incident_id || feature?.properties?.id || 'incident');
          element.setAttribute('role', 'button');
          element.setAttribute('tabindex', '0');
          element.setAttribute('aria-label', `Open incident ${incidentId}`);
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
        } else {
          marker.on('click', () => onFeatureSelect?.(feature));
        }
      }
    },
    fitFeatures(features) {
      const points = pointFeatures(features).map((feature) => {
        const [lon, lat] = feature.geometry.coordinates;
        return [Number(lat), Number(lon)];
      });
      if (points.length) map.fitBounds(L.latLngBounds(points), { padding: [28, 28], maxZoom: 9 });
    },
    flyTo({ center: nextCenter, zoom: nextZoom }) {
      map.flyTo([Number(nextCenter[1]), Number(nextCenter[0])], nextZoom);
    },
    resize() {
      map.invalidateSize();
    },
    destroy() {
      map.remove();
    },
  };
}
