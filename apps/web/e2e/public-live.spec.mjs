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
  const safety = categories.locator('a[href="#navigation_safety"]');
  await expect(safety).toBeVisible();
  await expect(safety).toContainText('Navigation safety');
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


test('public Live omits the legacy acquisition pipeline block', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  await expect(page.getByRole('region', { name: 'Acquisition pipeline' })).toHaveCount(0);
});

test('Listen live is exposed only for an eligible public receiver', async ({ page }) => {
  await page.goto(`${RADIO_PANEL_URL}?eligible=1`);
  await expect(page.getByRole('button', { name: 'Listen live' })).toBeVisible();
  await expect(page.getByText(/Ephemeral stream only; persistent audio is not stored/i)).toBeVisible();

  await page.goto(`${RADIO_PANEL_URL}?eligible=0`);
  await expect(page.getByRole('button', { name: 'Listen live' })).toHaveCount(0);
});

test('public Live does not expose raw AIS vessel layer controls', async ({ page }) => {
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
  await expect(row('AIS · moving vessels')).toHaveCount(0);
  await expect(row('AIS · stationary vessels')).toHaveCount(0);
  await expect(row('AIS · selected vessel trail')).toHaveCount(0);
  await expect(row('NGO SAR fleet')).toBeChecked();
});

test('Alarm Phone transport type stays Humanitarian Distress in counts and feed filtering', async ({ page }) => {
  await page.goto(PUBLIC_LIVE_URL);
  await page.getByRole('button', { name: 'Expand signal categories' }).click();
  const categories = page.getByRole('group', { name: 'Signal categories' });

  await page.locator('button[aria-label$="Humanitarian"]').click();
  await expect(categories.locator('a[href="#distress"]')).toContainText(/Distress\s*1/);

  await page.locator('button[aria-label$="Maritime"]').click();
  await expect(categories.locator('a[href="#public_observation"]')).toContainText(/Public observation\s*0/);
  await categories.locator('a[href="#public_observation"]').click();
  await expect(page.getByText('Distress report', { exact: true })).toBeVisible();
});

test('mobile Live keeps an interactive incident map when MapLibre cannot initialize', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => { window.__SEACOMMONS_FORCE_MAP_FALLBACK__ = true; });
  await page.goto(PUBLIC_LIVE_URL);
  await expect(page.locator('.leaflet-container')).toBeVisible();
  const markers = page.locator('.seacommons-fallback-marker');
  await expect(markers).toHaveCount(2);
  const targetMarker = page.locator('.seacommons-fallback-marker--humanitarian-1');
  await expect(targetMarker).toHaveAttribute('role', 'button');
  await targetMarker.dispatchEvent('pointerup');
  const fallbackReport = page.locator('.cone-panel--intel');
  await expect(fallbackReport).toBeVisible();
  await expect(fallbackReport.getByText('Distress report', { exact: true }).first()).toBeVisible();
});
