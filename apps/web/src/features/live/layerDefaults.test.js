import assert from 'node:assert/strict';
import test from 'node:test';

import { initialLayerVisibility, PUBLIC_LIVE_LAYER_STORAGE_KEY } from './layerDefaults.js';

function storage(values = {}) {
  return { getItem: (key) => values[key] ?? null };
}

test('public Live starts incident-first even when the legacy layer cache enabled raw AIS', () => {
  const value = initialLayerVisibility({
    publicLive: true,
    storage: storage({ seacommons_layer_vis: JSON.stringify({ ais_moving: true, ais_stationary: true, ais_trails: true }) }),
  });
  assert.equal(value.ais_moving, false);
  assert.equal(value.ais_stationary, false);
  assert.equal(value.ais_trails, false);
  assert.equal(value.ngo_vessels, true);
  assert.equal(value.sar, true);
  assert.equal(value.nautical, true);
});

test('public Live never restores raw AIS from its public layer profile', () => {
  const value = initialLayerVisibility({
    publicLive: true,
    storage: storage({ [PUBLIC_LIVE_LAYER_STORAGE_KEY]: JSON.stringify({ ais_moving: true, nautical: false }) }),
  });
  assert.equal(value.ais_moving, false);
  assert.equal(value.ais_stationary, false);
  assert.equal(value.ais_trails, false);
  assert.equal(value.nautical, true);
});
