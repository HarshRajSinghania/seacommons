import test from 'node:test';
import assert from 'node:assert/strict';

import {
  edgeEventToFeature,
  edgeSnapshotToFeatures,
  mergeIntelDriftUpdate,
  receivedSignalFeatures,
} from './normalize.js';

function edgeEvent(overrides = {}) {
  return {
    id: 'incident-1:v1',
    type: 'distress_observation',
    source: 'alarm_phone',
    observed_at: '2026-08-26T10:00:00Z',
    received_at: '2026-08-26T10:01:00Z',
    source_url: 'https://example.test/report',
    geometry: { type: 'Point', coordinates: [14.2, 35.1] },
    properties: {
      incident_id: 'incident-1',
      incident_lifecycle: 'active',
      severity: 'critical',
      verification_status: 'unverified_public_source',
      radius_m: 5000,
      title: 'Boat in distress',
    },
    ...overrides,
  };
}

test('normalizes an edge event to the VM public feature contract', () => {
  const feature = edgeEventToFeature(edgeEvent());

  assert.equal(feature.id, 'intel:incident-1');
  assert.equal(feature.properties.type, 'twitter');
  assert.equal(feature.properties.kind, 'distress');
  assert.equal(feature.properties.location_precision, 'reported_or_derived');
  assert.equal(feature.properties.location_uncertainty_m, 5000);
  assert.equal(feature.properties.text, '');
  assert.deepEqual(feature.geometry.coordinates, [14.2, 35.1]);
});

test('preserves lifecycle and explicit area precision from the edge', () => {
  const feature = edgeEventToFeature(edgeEvent({
    geometry: { type: 'Polygon', coordinates: [[[14, 35], [15, 35], [14, 35]]] },
    properties: {
      incident_id: 'incident-1',
      incident_lifecycle: 'archived',
      location_precision: 'area_low_confidence',
    },
  }));

  assert.equal(feature.properties.kind, 'archived');
  assert.equal(feature.properties.location_precision, 'area_low_confidence');
});

test('drops malformed events at the edge trust boundary', () => {
  const features = edgeSnapshotToFeatures({
    events: [edgeEvent(), null, { id: 'missing-contract' }, edgeEvent({ geometry: { type: 'Point' } })],
  });

  assert.deepEqual(features.map((feature) => feature.id), ['intel:incident-1']);
  assert.equal(edgeEventToFeature(null), null);
  assert.deepEqual(edgeSnapshotToFeatures({ events: 'invalid' }), []);
});

test('filters blocked transports and model products from the VM public feed', () => {
  const feature = (properties) => ({ type: 'Feature', geometry: null, properties });
  const visible = feature({ id: 'visible', type: 'distress', source: 'Alarm Phone' });
  const result = receivedSignalFeatures([
    visible,
    feature({ id: 'blocked-policy', source_policy: 'unofficial' }),
    feature({ id: 'blocked-transport', via: 'twscrape-mirror' }),
    feature({ id: 'model', type: 'sar_model' }),
    null,
  ]);

  assert.deepEqual(result, [visible]);
});

test('replaces stale drift features when an operator event update arrives', () => {
  const stale = {
    type: 'Feature', geometry: null, properties: { intel_event_id: 'event-1', version: 'old' },
  };
  const unrelated = {
    type: 'Feature', geometry: null, properties: { intel_event_id: 'event-2' },
  };
  const trajectory = {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: [[14, 35], [15, 36]] },
    properties: { type: 'trajectory' },
  };
  const result = mergeIntelDriftUpdate(
    { type: 'FeatureCollection', features: [stale, unrelated] },
    {
      id: 'event-1',
      drift: {
        trajectory,
        cone_24h: null,
        impact_point: { features: [] },
        title: 'Updated drift',
        severity: 'high',
        source: 'Alarm Phone',
      },
    },
  );

  assert.equal(result.features.length, 2);
  assert.equal(result.features[0], unrelated);
  assert.equal(result.features[1].properties.intel_event_id, 'event-1');
  assert.equal(result.features[1].properties.intel_title, 'Updated drift');
  assert.equal(result.features[1].properties.version, undefined);
});
