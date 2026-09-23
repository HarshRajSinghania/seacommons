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

test('navigation safety is an incident type under the Maritime macro', () => {
  const maritime = main.slice(main.indexOf("key: 'maritime'"), main.indexOf('];', main.indexOf("key: 'maritime'")));
  assert.match(maritime, /key: 'navigation_safety'/);
  assert.match(maritime, /label: 'Navigation safety'/);
  assert.doesNotMatch(maritime, /key: 'sanctions'/);
});


test('public Live is case-first and does not expose raw AIS vessel layers', () => {
  const allowList = main.slice(main.indexOf('const PUBLIC_LIVE_LAYER_GROUPS'), main.indexOf(']);', main.indexOf('const PUBLIC_LIVE_LAYER_GROUPS')));
  assert.doesNotMatch(allowList, /'ais_moving'/);
  assert.doesNotMatch(allowList, /'ais_stationary'/);
  assert.doesNotMatch(allowList, /'ais_trails'/);
  assert.match(allowList, /INTEL_MAP_CATEGORIES\.map\(\(category\) => `intel_\$\{category\.key\}`\)/);
  assert.match(main, /id: 'vessels-stationary-layer'/); // operator console still owns raw AIS
  assert.match(main, /id: 'selected-vessel-track'/);
  assert.match(main, /openVesselReport/);
  assert.match(main, /isPublicContextVessel[\s\S]{0,260}if \(!isPublicContextVessel\) return/);
  assert.match(main, /isPublicLiveHost[\s\S]{0,120}Promise\.resolve\(\{ type: 'FeatureCollection', features: \[\] \}\)/);
  assert.match(main, /isPublicLiveHost[\s\S]{0,220}proximity-vessels[\s\S]{0,120}features: \[\]/);
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


test('public Live maps both point and area incidents and list selection focuses either geometry', () => {
  assert.match(main, /geometry\?\.type === 'Point' \|\| feature\.geometry\?\.type === 'Polygon'/);
  assert.match(main, /const coordinates = panelFocusCoordinates\(\{ feature \}\)/);
  assert.match(main, /intel-distress-polygon-fill/);
  assert.match(main, /intel-distress-area/);
});

test('public Live header no longer embeds acquisition pipeline controls', () => {
  const publicShell = main.slice(main.indexOf('{isPublicLiveHost ? ('), main.indexOf('{!isPublicLiveHost', main.indexOf('{isPublicLiveHost ? (')));
  assert.doesNotMatch(publicShell, /live-acquisition/);
  assert.doesNotMatch(publicShell, />Acquisition</);
});

test('public Live loads only the fresh Civil SAR fleet projection when its facet is active', () => {
  const publicShell = main.slice(main.indexOf('{isPublicLiveHost ? ('), main.indexOf('{!isPublicLiveHost', main.indexOf('{isPublicLiveHost ? (')));
  assert.doesNotMatch(publicShell, /CivilSarFleetPanel/);
  assert.match(main, /sar_fleet: false/);
  assert.match(main, /Civil SAR fleet/);
  assert.match(main, /liveFacets\.sar_fleet \? '\/api\/v1\/live\/sar-fleet' : null/);
  assert.match(main, /: '\/api\/v1\/intel\/ngo'/);
});

test('movement panel distinguishes real tracks from unavailable movement evidence', () => {
  assert.match(cone, /Movement \/ drift evidence/);
  assert.match(cone, /No observed movement track is available/);
  assert.match(cone, /area-based position cannot originate a drift model/i);
});


test('image-derived humanitarian map pins keep a centre marker plus uncertainty halo', () => {
  assert.match(main, /media_pin_landmark/);
  assert.match(main, /id: 'intel-distress-area'/);
  assert.match(main, /location_uncertainty_m/);
  const filterBlock = main.slice(main.indexOf('const _PRECISE_POINT_FILTER'), main.indexOf('// CATEGORY', main.indexOf('const _PRECISE_POINT_FILTER')));
  assert.match(filterBlock, /media_pin_landmark/);
  assert.match(filterBlock, /media_ocr_consensus/);
});


test('public Live always boots on OpenStreetMap with nautical seamarks', () => {
  assert.match(main, /if \(isPublicLiveHost\) return 'standard'/);
  assert.match(main, /const effectiveBaseMap = isPublicLiveHost \? 'standard' : baseMap/);
  assert.match(main, /tiles: \['https:\/\/tiles\.openseamap\.org\/seamark\/\{z\}\/\{x\}\/\{y\}\.png'\]/);
  assert.match(main, /allowSatellite=\{!isPublicLiveHost\}/);
});


test('context vessel markers are raised above later map layers', () => {
  assert.match(main, /for \(const contextVesselLayerId of/);
  assert.match(main, /'vessels-ngo-stationary', 'vessels-ngo'/);
  assert.match(main, /'sanctioned-vessels-stationary', 'sanctioned-vessels-moving'/);
  assert.match(main, /map\.moveLayer\(contextVesselLayerId\)/);
});

test('Sanctions is a facet with a fresh-only vessel overlay, not a third macro category', () => {
  const publicMacros = main.slice(
    main.indexOf('const SIGNALS_MACRO_GROUPS'),
    main.indexOf('];', main.indexOf('const SIGNALS_MACRO_GROUPS')) + 2,
  );
  assert.doesNotMatch(publicMacros, /key: 'sanctions'/);
  assert.match(main, /Sanctioned vessels/);
  assert.match(main, /\/api\/v1\/live\/sanctioned-vessels/);
  assert.match(main, /sanctioned-vessels-moving/);
  assert.match(main, /sanctioned-vessels-stationary/);
  assert.doesNotMatch(main, /\['corroborated', 'Corroborated'\]/);
});


test('Sanctions and Civil SAR are always-visible context layers, not hidden investigation filters', () => {
  assert.match(main, /aria-label="Context layers"/);
  assert.match(main, /Sanctioned vessels/);
  assert.match(main, /Civil SAR fleet/);
  const contextStart = main.indexOf('aria-label="Context layers"');
  const expandedStart = main.indexOf('{signalsExpanded && (');
  assert.ok(contextStart > 0 && expandedStart > contextStart);
  const expandedBlock = main.slice(expandedStart, main.indexOf('{SIGNALS_MACRO_GROUPS.map', expandedStart));
  assert.doesNotMatch(expandedBlock, /\['sanctions', 'Sanctions'\]/);
  assert.doesNotMatch(expandedBlock, /\['sar_fleet', 'Civil SAR fleet'\]/);
});
