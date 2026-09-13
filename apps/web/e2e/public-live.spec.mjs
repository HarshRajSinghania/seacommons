import { expect, test } from '@playwright/test';

import { installDeterministicRoutes, PUBLIC_LIVE_URL } from './support/publicFixtures.mjs';

const RADIO_PANEL_URL = 'http://live.seacommons.org:4173/e2e/radio-panel.html';

test.beforeEach(async ({ page }) => {
  await installDeterministicRoutes(page);
});

test('public Live shell renders on the real public-host code path', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  await expect(page.locator('main.cop-shell.is-live-mode')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Live feed' })).toBeVisible();
});

test('Live semantics keep Humanitarian and Maritime as the public macro split', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  await page.getByRole('button', { name: 'Expand signal categories' }).click();

  const categories = page.getByRole('group', { name: 'Signal categories' });
  await expect(categories).toBeVisible();
  await expect(categories.getByRole('link', { name: /^Humanitarian/ })).toBeVisible();
  await expect(categories.getByRole('link', { name: /^Maritime/ })).toBeVisible();
  await expect(page.getByText('Maritime Security', { exact: true })).toHaveCount(0);

  const maritimeToggle = page.locator('button[aria-label$="Maritime"]');
  await expect(maritimeToggle).toHaveAttribute('aria-expanded', 'false');
  await maritimeToggle.click();
  await expect(page.locator('button[aria-label="Collapse Maritime"]')).toHaveAttribute('aria-expanded', 'true');
  const safety = categories.locator('a[href="#incident"]');
  await expect(safety).toBeVisible();
  await expect(safety).toContainText('Safety');
  await expect(page.getByText('Not under command report', { exact: true })).toBeVisible();
});

test('Humanitarian public browser surface contains no vessel dossier identifiers', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  const body = await page.locator('body').innerText();
  expect(body).not.toMatch(/\bMMSI\b/i);
  expect(body).not.toMatch(/\bIMO\b/i);
  expect(body).not.toMatch(/\bcallsign\b/i);
  expect(body).not.toMatch(/tracker dossier/i);
});


test('acquisition renders five canonical families and public-safe radio provenance', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  const acquisition = page.getByRole('region', { name: 'Acquisition pipeline' });
  for (const label of ['AIS', 'First-party', 'Partner', 'Public feed', 'Radio']) {
    await expect(acquisition.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(acquisition.getByText('Mediterranean DSC', { exact: true })).toBeVisible();
  await expect(acquisition.getByText(/kiwisdr · DSC · 2187\.5 kHz · USB/)).toBeVisible();
  const text = await acquisition.innerText();
  expect(text).not.toMatch(/frontend_url|physical_lineage|secret\.example/i);
});

test('Listen live is exposed only for an eligible public receiver', async ({ page }) => {
  await page.goto(`${RADIO_PANEL_URL}?eligible=1`);
  await expect(page.getByRole('button', { name: 'Listen live' })).toBeVisible();
  await expect(page.getByText(/Ephemeral stream only; persistent audio is not stored/i)).toBeVisible();

  await page.goto(`${RADIO_PANEL_URL}?eligible=0`);
  await expect(page.getByRole('button', { name: 'Listen live' })).toHaveCount(0);
});

test('public Live ignores the legacy AIS-on cache and starts incident-first', async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem('seacommons_layer_vis', JSON.stringify({
      ais_moving: true,
      ais_stationary: true,
      ais_trails: true,
    }));
  });
  await page.goto(PUBLIC_LIVE_URL);
  await page.getByTitle('Map layers').click();

  const row = (label) => page.locator('.layer-row').filter({ hasText: label }).locator('input');
  await expect(row('AIS · moving vessels')).not.toBeChecked();
  await expect(row('AIS · stationary vessels')).not.toBeChecked();
  await expect(row('AIS · selected vessel trail')).not.toBeChecked();
  await expect(row('NGO SAR fleet')).toBeChecked();
});

test('Alarm Phone transport type stays Humanitarian Distress in counts and feed filtering', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  await page.getByRole('button', { name: 'Expand signal categories' }).click();
  const categories = page.getByRole('group', { name: 'Signal categories' });

  await page.locator('button[aria-label$="Humanitarian"]').click();
  await expect(categories.locator('a[href="#distress"]')).toContainText(/Distress\s*1/);

  await page.locator('button[aria-label$="Maritime"]').click();
  await expect(categories.locator('a[href="#social"]')).toContainText(/Public observations\s*0/);
  await categories.locator('a[href="#social"]').click();
  await expect(page.getByText('Distress report', { exact: true })).toBeVisible();
});

test('mobile Live keeps an interactive incident map when MapLibre cannot initialize', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => { window.__SEACOMMONS_FORCE_MAP_FALLBACK__ = true; });
  await page.goto(PUBLIC_LIVE_URL);
  await expect(page.locator('.leaflet-container')).toBeVisible();
  const markers = page.locator('.seacommons-fallback-marker');
  await expect(markers).toHaveCount(2);
  await page.locator('.seacommons-fallback-marker--humanitarian-1').click({ force: true });
  await expect(page.locator('.cone-panel--intel')).toBeVisible();
  await expect(page.getByText('Distress report', { exact: true })).toBeVisible();
});
