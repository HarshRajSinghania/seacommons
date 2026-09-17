import test from 'node:test';
import assert from 'node:assert/strict';

import {
  edgeEventToFeature,
  edgeSnapshotToFeatures,
  mergeIntelDriftUpdate,
  receivedSignalFeatures,
  usefulPublicLiveFeatures,
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


test('public Live keeps actionable humanitarian/beacon cases and removes raw AIS-only casualty noise', () => {
  const feature = (id, properties, coordinates = [14, 35]) => ({
    type: 'Feature', geometry: { type: 'Point', coordinates }, properties: { id, ...properties },
  });
  const humanitarian = feature('intel:h1', { type: 'twitter', source: 'alarm_phone', visual_category: 'humanitarian_alarm_phone' });
  const sart = feature('intel:sart', { type: 'distress', source: 'ais_sart', visual_category: 'distress', verification_status: 'ais_transponder' });
  const aground = feature('intel:aground', { type: 'distress', source: 'ais', visual_category: 'navigation_casualty', verification_status: 'ais_transponder', title: 'Vessel ran aground — TEST' });
  const publishedHypothesis = feature('intel:hyp', { type: 'investigation', source: 'SeaCommons evidence engine', hypothesis_state: 'published', evidence_stage: 'assessed' });

  assert.deepEqual(
    usefulPublicLiveFeatures([humanitarian, sart, aground, publishedHypothesis]).map((item) => item.properties.id),
    ['intel:h1', 'intel:sart', 'intel:hyp'],
  );
});

test('public Live collapses near-simultaneous Alarm Phone translations of the same regional case', () => {
  const make = (id, title, timestamp) => ({
    type: 'Feature', geometry: { type: 'Point', coordinates: [3.5, 36.79492] },
    properties: { id, type: 'twitter', source: 'alarm_phone', visual_category: 'humanitarian_alarm_phone', coordinate_source: 'region_area', title, timestamp_utc: timestamp },
  });
  const result = usefulPublicLiveFeatures([
    make('intel:en', '@alarm_phone: Where are they? A boat with 27 people left Boumerdes', '2026-09-17T14:45:26Z'),
    make('intel:fr', '@alarm_phone: Porté·es disparu·es! bateau de 27 personnes', '2026-09-17T14:45:59Z'),
  ]);
  assert.equal(result.length, 1);
});
