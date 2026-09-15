import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const main = fs.readFileSync(path.resolve('src/main.jsx'), 'utf8');
const dashboard = fs.readFileSync(path.resolve('src/components/IntelDashboard.jsx'), 'utf8');
const cone = fs.readFileSync(path.resolve('src/components/ConePanel.jsx'), 'utf8');

test('public Live primary semantics are Humanitarian and Maritime, never legacy source buckets', () => {
  assert.match(main, /key: 'humanitarian'[\s\S]*label: 'Humanitarian'/);
  assert.match(main, /key: 'maritime'[\s\S]*label: 'Maritime'/);
  assert.doesNotMatch(main, /label: 'Maritime Security'/);
  assert.doesNotMatch(dashboard, /label: 'Direct'/);
  assert.doesNotMatch(dashboard, /label: 'Public feeds'/);
});

test('vessel incident safety filter belongs to Maritime macro', () => {
  const maritime = main.slice(main.indexOf("key: 'maritime'"), main.indexOf('];', main.indexOf("key: 'maritime'")));
  assert.match(maritime, /key: 'incident'/);
  assert.match(maritime, /label: 'Safety'/);
});


test('public Live is case-first and does not expose raw AIS vessel layers', () => {
  const allowList = main.slice(main.indexOf('const PUBLIC_LIVE_LAYER_GROUPS'), main.indexOf(']);', main.indexOf('const PUBLIC_LIVE_LAYER_GROUPS')));
  assert.doesNotMatch(allowList, /'ais_moving'/);
  assert.doesNotMatch(allowList, /'ais_stationary'/);
  assert.doesNotMatch(allowList, /'ais_trails'/);
  assert.match(main, /id: 'vessels-stationary-layer'/); // operator console still owns raw AIS
  assert.match(main, /id: 'selected-vessel-track'/);
  assert.match(main, /openVesselReport/);
  assert.match(main, /isPublicLiveHost[\s\S]{0,120}Promise\.resolve\(\{ type: 'FeatureCollection', features: \[\] \}\)/);
});

test('public Live exposes the radio receiver mesh and decoded DSC without making Radio a third macro category', () => {
  assert.match(main, /'radio_receivers'/);
  assert.match(main, /'radio_dsc'/);
  assert.match(main, /id: 'radio-receivers-layer'/);
  assert.match(main, /id: 'radio-dsc-layer'/);
  assert.match(main, /\/api\/v1\/live\/radio\/messages/);
  assert.match(main, /\/api\/v1\/live\/radio\/events/);
  assert.doesNotMatch(main, /key: 'radio'[\s\S]{0,80}label: 'Radio'/);
});


test('eligible radio receivers expose bounded VM-backed Listen live WebAudio controls', () => {
  assert.match(main, /listen_available: Boolean\(receiver\.listen_available\)/);
  assert.match(main, /frequency_hz: receiver\.frequency_hz/);
  assert.match(cone, /Listen live/);
  assert.match(cone, /liveListenHttpUrl/);
  assert.match(cone, /fetch\(/);
  assert.doesNotMatch(main, /radioListenBase=\{LIVE_EDGE_BASE/);
  assert.match(cone, /pcm16leToFloat32/);
  assert.match(cone, /persistent audio is not stored/i);
});

test('correlated alerts render canonical semantic colour before domain fallback', () => {
  assert.match(main, /const _fusedColor = \['coalesce', \['get', 'visual_color'\], _domainColor\]/);
  assert.match(main, /'circle-color': _fusedColor/);
});

test('public dashboard filters by semantic signal category, not raw transport type', () => {
  assert.match(dashboard, /signalCategoryOf\(f\.properties \|\| \{\}\)/);
});
