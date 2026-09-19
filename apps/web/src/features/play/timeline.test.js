import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

import {
  normalizeTimeline,
  selectFrame,
  selectSatelliteObservation,
  statusLabel,
  resolveGlobalTimelinePosition,
  incidentsAtCutoff,
  incidentCollection,
  timelineAtCutoff,
  incidentStatusAtCutoff,
  satelliteFootprintCollection,
  archiveCoverage,
  groupArchiveByMonth,
  satelliteContextTileUrl,
} from './timeline.js';

const items = [
  { id: 'later', at: '2026-09-05T12:00:00Z', type: 'attending_news', geometry: null },
  { id: 'report', at: '2026-09-04T12:00:00Z', type: 'report', geometry: { type: 'Point', coordinates: [14, 35] } },
  { id: 'sat-before', at: '2026-09-04T10:00:00Z', type: 'satellite', properties: { asset_ref: 'before.jpg' } },
  { id: 'drift', at: '2026-09-04T12:10:00Z', type: 'drift', geometry: { type: 'LineString', coordinates: [[14, 35], [14.2, 35.2]] } },
];

test('normalizeTimeline sorts ascending and assigns stable frame indices', () => {
  const result = normalizeTimeline(items);
  assert.deepEqual(result.map((item) => item.id), ['sat-before', 'report', 'drift', 'later']);
  assert.deepEqual(result.map((item) => item.frameIndex), [0, 1, 2, 3]);
});

test('selectFrame clamps index and carries the last known geometry', () => {
  const timeline = normalizeTimeline(items);
  const first = selectFrame(timeline, -10);
  const last = selectFrame(timeline, 999);
  assert.equal(first.item.id, 'sat-before');
  assert.equal(last.item.id, 'later');
  assert.deepEqual(last.geometry, { type: 'LineString', coordinates: [[14, 35], [14.2, 35.2]] });
});

test('selectSatelliteObservation chooses the closest snapshot at or before the frame', () => {
  const timeline = normalizeTimeline([
    ...items,
    { id: 'sat-after', at: '2026-09-04T14:00:00Z', type: 'satellite', properties: { asset_ref: 'after.jpg' } },
  ]);
  const observation = selectSatelliteObservation(timeline, '2026-09-04T12:10:00Z');
  assert.equal(observation.id, 'sat-before');
  assert.equal(observation.properties.asset_ref, 'before.jpg');
});

test('statusLabel never exposes archived as an incident outcome', () => {
  assert.equal(statusLabel('resolved'), 'RESOLVED');
  assert.equal(statusLabel('needs_review'), 'NEEDS REVIEW');
  assert.equal(statusLabel('outcome_unknown'), 'OUTCOME UNKNOWN');
  assert.equal(statusLabel('archived'), 'OUTCOME UNKNOWN');
});

test('satelliteRasterDescriptor supports dated tiles and bounded image previews', async () => {
  const { satelliteRasterDescriptor } = await import('./timeline.js');
  const tile = satelliteRasterDescriptor({
    properties: { asset_ref: 'https://example.test/{z}/{y}/{x}.jpg', bbox: [13.9, 35.3, 14.3, 35.7] },
  });
  assert.equal(tile.type, 'raster');
  assert.deepEqual(tile.tiles, ['https://example.test/{z}/{y}/{x}.jpg']);

  const image = satelliteRasterDescriptor({
    properties: { asset_ref: 'https://example.test/preview.jpg', bbox: [14, 35, 14.2, 35.2] },
  });
  assert.equal(image.type, 'image');
  assert.deepEqual(image.coordinates[0], [14, 35.2]);
});

test('satelliteRasterDescriptor rejects non-image product assets', async () => {
  const { satelliteRasterDescriptor } = await import('./timeline.js');
  assert.equal(satelliteRasterDescriptor({ properties: { asset_ref: 's3://bucket/product.safe' } }), null);
});


test('global archive timeline is ALL at its rightmost position and temporal before it', () => {
  const incidents = [
    { incident_id: 'a', reported_at: '2026-09-01T00:00:00Z' },
    { incident_id: 'b', reported_at: '2026-09-05T00:00:00Z' },
  ];
  assert.deepEqual(resolveGlobalTimelinePosition(incidents, 1000), { mode: 'all', cutoff: null });
  const past = resolveGlobalTimelinePosition(incidents, 500);
  assert.equal(past.mode, 'temporal');
  assert.equal(past.cutoff, '2026-09-03T00:00:00.000Z');
});

test('cutoff filters archive incidents and selected-case evidence without leaking the future', () => {
  const incidents = [
    { incident_id: 'a', reported_at: '2026-09-01T00:00:00Z', geometry: { type: 'Point', coordinates: [10, 35] } },
    { incident_id: 'b', reported_at: '2026-09-04T00:00:00Z', geometry: { type: 'Point', coordinates: [12, 36] } },
  ];
  const cutoff = '2026-09-02T00:00:00.000Z';
  assert.deepEqual(incidentsAtCutoff(incidents, cutoff).map((x) => x.incident_id), ['a']);
  const fc = incidentCollection(incidents, cutoff);
  assert.equal(fc.features.length, 1);
  assert.equal(fc.features[0].properties.incident_id, 'a');
  const timeline = normalizeTimeline([
    { id: 'r', at: '2026-09-01T01:00:00Z', type: 'report' },
    { id: 'future', at: '2026-09-03T01:00:00Z', type: 'satellite' },
  ]);
  assert.deepEqual(timelineAtCutoff(timeline, cutoff).map((x) => x.id), ['r']);
  assert.equal(timelineAtCutoff(timeline, null).length, 2);
});

test('Play surface exposes ALL-first global timeline and mobile drawer/sheet contracts', async () => {
  const { readFile } = await import('node:fs/promises');
  const jsx = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('./play.css', import.meta.url), 'utf8');
  assert.match(jsx, /aria-label="Global archive timeline"/);
  assert.match(jsx, /play-mobile-cases-toggle/);
  assert.match(jsx, /play-evidence.*is-open/);
  assert.match(jsx, /next_offset/);
  assert.match(jsx, /!visibleIncidents\.some/);
  assert.match(css, /\.play-mobile-cases-toggle/);
  assert.match(css, /\.play-evidence\.is-open/);
});

test('incident status at cutoff uses only transitions knowable by that time', () => {
  const incident = {
    incident_status: 'resolved',
    status_history: [
      { at: '2026-09-03T00:00:00Z', from_state: 'active', to_state: 'needs_review' },
      { at: '2026-09-04T00:00:00Z', from_state: 'needs_review', to_state: 'resolved' },
    ],
  };
  assert.equal(incidentStatusAtCutoff(incident, '2026-09-02T00:00:00Z'), 'active');
  assert.equal(incidentStatusAtCutoff(incident, '2026-09-03T12:00:00Z'), 'needs_review');
  assert.equal(incidentStatusAtCutoff(incident, null), 'resolved');
});

test('Play map style keeps a visible street-map fallback under satellite imagery', async () => {
  const { playMapStyle } = await import('./timeline.js');
  const style = playMapStyle('2026-09-04');
  assert.equal(style.layers[0].id, 'base-map');
  assert.equal(style.layers[0].paint?.['raster-opacity'] ?? 1, 1);
  assert.equal(style.layers[1].id, 'satellite-context');
  assert.equal(style.layers[1].paint?.['raster-opacity'], 1);
  assert.ok(style.sources.baseMap?.tiles?.length);
  assert.ok(style.sources.satelliteContext?.tiles?.length);
});

test('Play panels use the same glass treatment as public Live and keep the map dominant', async () => {
  const css = await readFile(new URL('./play.css', import.meta.url), 'utf8');
  assert.match(css, /background:\s*rgba\(3,\s*10,\s*14,\s*\.88\)/);
  assert.match(css, /backdrop-filter:\s*blur\(18px\)\s+saturate\(1\.15\)/);
  assert.match(css, /\.play-public-shell/);
  assert.match(css, /--live-panel-width:\s*392px/);
  assert.match(css, /\.play-timeline-bar/);
});

test('mergeIncidentPages progressively deduplicates pages and keeps the newest version', async () => {
  const { mergeIncidentPages } = await import('./timeline.js');
  const first = [
    { incident_id: 'a', reported_at: '2026-09-05T10:00:00Z', title: 'A' },
    { incident_id: 'b', reported_at: '2026-09-05T09:00:00Z', title: 'B old' },
  ];
  const second = [
    { incident_id: 'b', reported_at: '2026-09-05T09:00:00Z', title: 'B new' },
    { incident_id: 'c', reported_at: '2026-09-04T08:00:00Z', title: 'C' },
  ];
  const merged = mergeIncidentPages(first, second);
  assert.deepEqual(merged.map((item) => item.incident_id), ['a', 'b', 'c']);
  assert.equal(merged.find((item) => item.incident_id === 'b').title, 'B new');
});


test('Copernicus OData thumbnails are renderable image sources even without a filename extension', async () => {
  const { satelliteRasterDescriptor } = await import('./timeline.js');
  const observation = {
    source: 'copernicus_dataspace',
    properties: {
      asset_ref: 'https://datahub.creodias.eu/odata/v1/Assets(abc)/$value',
      bbox: [14, 35, 15, 36],
      mission: 'Sentinel-1',
    },
  };
  const source = satelliteRasterDescriptor(observation);
  assert.equal(source.type, 'image');
  assert.equal(source.url, observation.properties.asset_ref);
});

test('Sentinel footprints remain visible when no raster preview is available', () => {
  const timeline = [{
    id: 's1', type: 'satellite', source: 'copernicus_dataspace',
    geometry: { type: 'Polygon', coordinates: [[[14,35],[15,35],[15,36],[14,35]]] },
    properties: { mission: 'Sentinel-1', sensor_type: 'sar' },
  }];
  const collection = satelliteFootprintCollection(timeline);
  assert.equal(collection.features.length, 1);
  assert.equal(collection.features[0].properties.mission, 'Sentinel-1');
});

test('satellite layer auto mode prefers Sentinel over newer VIIRS context', async () => {
  const { selectPreferredSatelliteObservation } = await import('./timeline.js');
  const timeline = [
    { id: 's1', at: '2026-09-01T10:00:00Z', type: 'satellite', source: 'copernicus_dataspace', properties: { mission: 'Sentinel-1' } },
    { id: 'viirs', at: '2026-09-04T00:00:00Z', type: 'satellite', source: 'nasa_gibs', properties: { mission: 'VIIRS Suomi-NPP' } },
  ];
  assert.equal(selectPreferredSatelliteObservation(timeline, '2026-09-05T00:00:00Z')?.id, 's1');
});

test('satellite layer can explicitly select a mission', async () => {
  const { selectPreferredSatelliteObservation } = await import('./timeline.js');
  const timeline = [
    { id: 's1', at: '2026-09-01T10:00:00Z', type: 'satellite', source: 'copernicus_dataspace', properties: { mission: 'Sentinel-1' } },
    { id: 's2', at: '2026-09-02T10:00:00Z', type: 'satellite', source: 'copernicus_dataspace', properties: { mission: 'Sentinel-2' } },
  ];
  assert.equal(selectPreferredSatelliteObservation(timeline, '2026-09-05T00:00:00Z', 'Sentinel-2')?.id, 's2');
});


test('selector respects cutoff and null cloud metadata', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /new Set\(visibleTimeline/);
  assert.match(source, /setSatelliteMission\('auto'\)/);
  assert.match(source, /cloud_cover != null/);
});

test('Play satellite basemap is the default visual surface over an OSM fallback', async () => {
  const { playMapStyle } = await import('./timeline.js');
  const style = playMapStyle('2026-09-04');
  assert.equal(style.layers[0].id, 'base-map');
  assert.equal(style.layers[1].id, 'satellite-context');
  assert.equal(style.layers[1].paint?.['raster-opacity'], 1);
  assert.match(style.sources.satelliteContext.tiles[0], /VIIRS_NOAA21_CorrectedReflectance_TrueColor/);
});

test('Play public map hides attribution control and initializes independently of a selected case', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /attributionControl:\s*false/);
  assert.match(source, /map\.resize\(\)/);
  const init = source.slice(source.indexOf("map.on('load'"), source.indexOf("mapRef.current = map"));
  assert.doesNotMatch(init, /if\s*\(selectedId\)/);
});

test('Play reuses the public Live shell classes instead of a parallel panel system', async () => {
  const jsx = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  const entry = await readFile(new URL('../../play.jsx', import.meta.url), 'utf8');
  assert.match(entry, /import ['"]\.\/styles\.css['"]/);
  assert.match(jsx, /cop-shell[^`]*play-public-shell/);
  assert.match(jsx, /live-feed-panel is-open play-archive-panel/);
  assert.match(jsx, /live-feed-panel__header/);
  assert.match(jsx, /live-feed-panel__body/);
  assert.match(jsx, /cone-panel cone-panel--intel play-evidence/);
});

test('Play mobile archive drawer stays closed until the archive toggle opens it', async () => {
  const css = await readFile(new URL('./play.css', import.meta.url), 'utf8');
  assert.match(css, /\.play-public-shell \.play-archive-panel\s*\{[^}]*transform:\s*translateY\(102%\)/s);
  assert.match(css, /\.play-public-shell \.play-archive-panel\.is-mobile-open\s*\{[^}]*transform:\s*translateY\(0\)/s);
});

test('Play uses two main categories, incident types and evidence facets', async () => {
  const jsx = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  for (const label of ['ALL', 'HUMANITARIAN', 'MARITIME']) {
    assert.match(jsx, new RegExp(`>${label}<`));
  }
  assert.match(jsx, /Incident type/);
  for (const label of ['SATELLITE', 'SANCTIONS', 'INVESTIGATION']) {
    assert.match(jsx, new RegExp(label));
  }
  assert.doesNotMatch(jsx, />CORROBORATED</);
});

test('Play uses the shared Live vessel triangle for AIS archive identities', async () => {
  const { incidentCollection } = await import('./timeline.js');
  const collection = incidentCollection([
    { incident_id: 'aisanom:123:gap', case_type: 'ais_anomaly', domain: 'maritime', geometry: { type: 'Point', coordinates: [12, 35] } },
    { incident_id: 'fus:abc', case_type: 'correlated_alert', domain: 'maritime', geometry: { type: 'Point', coordinates: [13, 36] } },
  ]);
  assert.equal(collection.features[0].properties.marker_kind, 'vessel');
  assert.equal(collection.features[1].properties.marker_kind, 'incident');
  const jsx = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(jsx, /createVesselArrowImage/);
  assert.match(jsx, /id: 'play-vessels'/);
});


test('Play gives the exact archive counter a longer timeout without delaying map pages', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /fetchJson\(apiBase, '\/api\/v1\/play\/counts', undefined, 30_000\)/);
  assert.match(source, /fetchJson\(apiBase, `\/api\/v1\/play\/incidents\?limit=100&offset=\$\{offset\}`, undefined, 30_000\)/);
});


test('Play labels partial progressive archive counts as loaded until exact total arrives', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /archiveTotal != null \? `\$\{archiveTotal\} archive` : `\$\{visibleIncidents\.length\} loaded`/);
  assert.match(source, /Archive · \{archiveTotal != null \? archiveTotal : `\$\{visibleIncidents\.length\} loaded`\}/);
  assert.doesNotMatch(source, /`\$\{visibleIncidents\.length\} points`/);
});

test('Play archive loader follows pagination to exhaustion without a fixed page ceiling', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /pages\s*<\s*\(full\s*\?\s*100/);
  assert.match(source, /seenOffsets/);
  assert.match(source, /next_offset/);
});

test('Play adopts first-page total_count immediately as the catalog total', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /payload\?\.total_count/);
  assert.match(source, /setArchiveTotal\(Number\(payload\.total_count\)\)/);
});

test('Play map initialization fails over to the shared interactive renderer', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /createFallbackMap/);
  assert.match(source, /fallbackMapRef/);
  assert.match(source, /__SEACOMMONS_FORCE_MAP_FALLBACK__/);
  assert.match(source, /fallback\.setFeatures\(incidentCollection\(filteredIncidents\)\.features\)/);
  assert.match(source, /setSelectedId\(String\(feature\.properties\.incident_id\)\)/);
});


test('archiveCoverage reports UTC range, covered months and gaps', () => {
  const coverage = archiveCoverage([
    { incident_id: 'a', reported_at: '2024-10-19T18:17:19Z' },
    { incident_id: 'b', reported_at: '2024-12-02T10:00:00Z' },
    { incident_id: 'c', reported_at: '2026-09-15T17:15:16Z' },
  ]);
  assert.equal(coverage.start, '2024-10-19T18:17:19.000Z');
  assert.equal(coverage.end, '2026-09-15T17:15:16.000Z');
  assert.deepEqual(coverage.coveredMonths, ['2024-10', '2024-12', '2026-09']);
  assert.ok(coverage.missingMonths.includes('2024-11'));
  assert.ok(coverage.missingMonths.includes('2026-08'));
});

test('groupArchiveByMonth keeps newest month and cases first', () => {
  const groups = groupArchiveByMonth([
    { incident_id: 'old', reported_at: '2024-10-19T18:17:19Z' },
    { incident_id: 'newer', reported_at: '2026-09-15T17:15:16Z' },
    { incident_id: 'newest', reported_at: '2026-09-15T18:15:16Z' },
  ]);
  assert.deepEqual(groups.map((group) => group.key), ['2026-09', '2024-10']);
  assert.deepEqual(groups[0].incidents.map((item) => item.incident_id), ['newest', 'newer']);
});

test('satelliteContextTileUrl follows archive day and NOAA mission', () => {
  assert.match(satelliteContextTileUrl('2026-09-04', 'VIIRS NOAA-21'), /VIIRS_NOAA21_CorrectedReflectance_TrueColor/);
  assert.match(satelliteContextTileUrl('2026-09-04', 'VIIRS NOAA-20'), /VIIRS_NOAA20_CorrectedReflectance_TrueColor/);
  assert.match(satelliteContextTileUrl('2026-09-04', 'VIIRS Suomi-NPP'), /VIIRS_SNPP_CorrectedReflectance_TrueColor/);
  assert.match(satelliteContextTileUrl('2026-09-04', 'VIIRS NOAA-21'), /2026-09-04/);
});


test('Play synchronizes global satellite context to archive time and exposes continuity metadata', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /satelliteContextTileUrl/);
  assert.match(source, /setTiles/);
  assert.match(source, /archiveCoverage/);
  assert.match(source, /groupArchiveByMonth/);
  assert.match(source, /play-archive-month/);
  assert.match(source, /evidence_counts/);
});


test('Play case dossier renders the selected satellite quicklook asset', async () => {
  const source = await readFile(new URL('./PlayTimeline.jsx', import.meta.url), 'utf8');
  assert.match(source, /play-satellite-preview/);
  assert.match(source, /satelliteProps\.asset_ref/);
  assert.match(source, /Satellite quicklook/);
});
