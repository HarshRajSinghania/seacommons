import assert from 'node:assert/strict';
import test from 'node:test';

import { createFallbackMap } from './fallbackRenderer.js';

function fakeLeaflet(log) {
  const map = {
    setView(latlng, zoom) { log.setView = [latlng, zoom]; return map; },
    flyTo(latlng, zoom) { log.flyTo = [latlng, zoom]; },
    fitBounds(bounds) { log.fitBounds = bounds; },
    invalidateSize() { log.resized = true; },
    remove() { log.removed = true; },
  };
  return {
    map() { return map; },
    tileLayer(url) { log.tileUrl = url; return { addTo() { return this; } }; },
    layerGroup() { return { addTo() { return this; }, clearLayers() { log.cleared = true; } }; },
    circleMarker(latlng, options) {
      const element = {
        handlers: {},
        attrs: {},
        addEventListener(event, handler) { element.handlers[event] = handler; },
        setAttribute(name, value) { element.attrs[name] = value; },
      };
      const marker = {
        on(_event, handler) { marker.handler = handler; return marker; },
        addTo() { log.markers.push({ latlng, options, marker, element }); return marker; },
        getElement() { return element; },
      };
      return marker;
    },
    polygon(latlngs, options) {
      const layer = {
        on(_event, handler) { layer.handler = handler; return layer; },
        addTo() { log.polygons.push({ latlngs, options, layer }); return layer; },
      };
      return layer;
    },
    latLngBounds(points) { return { points }; },
  };
}
test('fallback map renders only real point features and preserves canonical selection', async () => {
  const log = { markers: [], polygons: [] };
  const selected = [];
  const renderer = await createFallbackMap({
    container: {},
    center: [15, 36],
    zoom: 5,
    onFeatureSelect: (feature) => selected.push(feature),
    leaflet: fakeLeaflet(log),
  });

  const point = { type: 'Feature', geometry: { type: 'Point', coordinates: [25.1, 34.1] }, properties: { incident_id: 'HUM-1', domain: 'humanitarian' } };
  const missing = { type: 'Feature', geometry: null, properties: { incident_id: 'HUM-2' } };
  renderer.setFeatures([point, missing]);

  assert.equal(log.markers.length, 1);
  assert.deepEqual(log.markers[0].latlng, [34.1, 25.1]);
  assert.equal(log.markers[0].element.attrs.role, 'button');
  assert.equal(log.markers[0].element.attrs['aria-label'], 'Open incident HUM-1');
  assert.equal(typeof log.markers[0].element.handlers.pointerup, 'function');
  log.markers[0].element.handlers.pointerup({ stopPropagation() {} });
  assert.deepEqual(selected, [point]);
});

test('fallback map renders public search-area polygons and selects their incident', async () => {
  const log = { markers: [], polygons: [] };
  const selected = [];
  const renderer = await createFallbackMap({
    container: {}, center: [15, 36], zoom: 5,
    onFeatureSelect: (feature) => selected.push(feature),
    leaflet: fakeLeaflet(log),
  });
  const area = {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [[[24, 35], [25, 35], [25, 36], [24, 35]]] },
    properties: { incident_id: 'HUM-AREA-1', domain: 'humanitarian' },
  };

  renderer.setFeatures([area]);

  assert.equal(log.polygons.length, 1);
  assert.deepEqual(log.polygons[0].latlngs, [[[35, 24], [35, 25], [36, 25], [35, 24]]]);
  assert.equal(log.polygons[0].options.fillOpacity, 0.22);
  log.polygons[0].layer.handler();
  assert.deepEqual(selected, [area]);
});

test('fallback map supports fit, fly, resize and destroy', async () => {
  const log = { markers: [], polygons: [] };
  const renderer = await createFallbackMap({ container: {}, center: [15, 36], zoom: 5, onFeatureSelect() {}, leaflet: fakeLeaflet(log) });
  const features = [{ type: 'Feature', geometry: { type: 'Point', coordinates: [12, 35] }, properties: {} }];
  renderer.fitFeatures(features);
  renderer.flyTo({ center: [13, 36], zoom: 7 });
  renderer.resize();
  renderer.destroy();
  assert.deepEqual(log.fitBounds.points, [[35, 12]]);
  assert.deepEqual(log.flyTo, [[36, 13], 7]);
  assert.equal(log.resized, true);
  assert.equal(log.removed, true);
});
