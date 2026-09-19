import { expect, test } from '@playwright/test';

import { installDeterministicRoutes, PUBLIC_PLAY_URL } from './support/publicFixtures.mjs';

test.beforeEach(async ({ page }) => {
  await installDeterministicRoutes(page);
});

test('Play archive exposes deterministic filters, dossier and global timeline', async ({ page }) => {
  await page.goto(PUBLIC_PLAY_URL);

  await expect(page.locator('main.play-public-shell')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Play' })).toBeVisible();
  const filters = page.getByRole('group', { name: 'Play archive filters' });
  for (const label of ['ALL', 'HUMANITARIAN', 'MARITIME', 'SATELLITE', 'SANCTIONS', 'INVESTIGATION']) {
    await expect(filters.getByRole('button', { name: label })).toBeVisible();
  }
  await expect(filters.getByRole('button', { name: 'CORRELATED' })).toHaveCount(0);
  await expect(filters.getByRole('combobox', { name: 'Incident type' })).toBeVisible();

  const cases = page.locator('.play-cases__list');
  await expect(cases.getByText('Historical distress', { exact: true })).toBeVisible();
  await expect(cases.getByText('Historical safety report', { exact: true })).toBeVisible();
  await expect(cases.getByText('Reviewed correlated case', { exact: true })).toBeVisible();

  await filters.getByRole('button', { name: 'HUMANITARIAN' }).click();
  await expect(cases.getByText('Historical distress', { exact: true })).toBeVisible();
  await expect(cases.getByText('Historical safety report', { exact: true })).toHaveCount(0);
  await expect(cases.getByText('Reviewed correlated case', { exact: true })).toHaveCount(0);

  await filters.getByRole('button', { name: 'ALL' }).click();
  await cases.getByText('Historical distress', { exact: true }).click();
  const dossier = page.locator('.play-evidence.is-open');
  await expect(dossier).toBeVisible();
  await expect(dossier.locator('h2', { hasText: 'Historical distress' })).toBeVisible();
  await expect(dossier.getByText('RESOLVED', { exact: true })).toBeVisible();

  await expect(page.getByRole('slider', { name: 'Global archive timeline' })).toBeVisible();
  await expect(page.locator('.play-all-badge strong')).toHaveText('ALL');
});

test('mobile Play keeps an interactive archive map when MapLibre cannot initialize', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => { window.__SEACOMMONS_FORCE_MAP_FALLBACK__ = true; });
  await page.goto(PUBLIC_PLAY_URL);
  await expect(page.locator('.leaflet-container')).toBeVisible();
  const markers = page.locator('.seacommons-fallback-marker');
  await expect(markers).toHaveCount(3);
  const targetMarker = page.locator('.seacommons-fallback-marker--play-humanitarian-1');
  await expect(targetMarker).toHaveAttribute('role', 'button');
  await targetMarker.dispatchEvent('pointerup');
  const dossier = page.locator('.play-evidence.is-open');
  await expect(dossier).toBeVisible();
  await expect(dossier.locator('h2')).toContainText('Historical distress');
});
