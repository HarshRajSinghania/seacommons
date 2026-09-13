import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import { playMapStyle } from '../play/timeline.js';

const css = fs.readFileSync(new URL('../../styles.css', import.meta.url), 'utf8');

test('Play public basemap does not depend on blocked OSM raster tiles', () => {
  const style = playMapStyle('2026-09-12');
  const url = style.sources.baseMap.tiles[0];
  assert.doesNotMatch(url, /tile\.openstreetmap\.org/);
});

test('mobile Live feed scrolls as one sheet so acquisition cannot hide events', () => {
  const mobileBlock = css.slice(css.indexOf('@media (max-width: 820px)', css.indexOf('.live-feed-panel')));
  assert.match(mobileBlock, /\.live-feed-panel\s*\{[^}]*overflow-y:\s*auto/s);
  assert.match(mobileBlock, /\.live-feed-panel__body\s*\{[^}]*overflow:\s*visible/s);
});
