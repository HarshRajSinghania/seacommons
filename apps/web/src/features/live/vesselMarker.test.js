import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

test('public SAR asset markers stay triangular even when stationary', async () => {
  const source = await readFile(new URL('../../main.jsx', import.meta.url), 'utf8');
  assert.match(source, /id: 'vessels-stationary-layer'.*?type: 'circle'/s);
  assert.match(source, /id: 'vessels-layer'.*?icon-image': 'vessel-arrow'/s);
  assert.match(source, /id: 'vessels-ngo-stationary'.*?type: 'symbol'.*?icon-image': 'vessel-arrow'/s);
  assert.match(source, /id: 'vessels-ngo'.*?icon-image': 'vessel-arrow'/s);
  assert.match(source, /id: 'selected-vessel-track'.*?type: 'line'/s);
});

test('vessel click handler opens reports from moving and stationary AIS layers', async () => {
  const source = await readFile(new URL('../../main.jsx', import.meta.url), 'utf8');
  assert.match(source, /for \(const lyr of \['vessels-layer', 'vessels-stationary-layer', 'vessels-ngo', 'vessels-ngo-stationary'/);
  assert.match(source, /openVesselReport\(feature\)/);
});
