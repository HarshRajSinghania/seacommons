import { expect, test } from '@playwright/test';

const SITE_URL = 'http://127.0.0.1:4173/site.html';

test('institutional homepage explains Humanitarian and Maritime without legacy simulation surfaces', async ({ page }) => {
  await page.goto(SITE_URL);

  await expect(page.getByRole('heading', { level: 1 })).toContainText('incomplete evidence');
  await expect(page.locator('#humanitarian')).toContainText('Humanitarian');
  await expect(page.locator('#maritime')).toContainText('Maritime');

  await expect(page.getByText('Evidence in motion', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/Pipeline \/ 24 h/i)).toHaveCount(0);
  await expect(page.getByText('ENGINE', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/drift demonstrator/i)).toHaveCount(0);
});

test('overall strip is one all-time row with four public case totals', async ({ page }) => {
  await page.goto(SITE_URL);

  const strip = page.getByRole('region', { name: 'SeaCommons all-time public case totals' });
  await expect(strip).toBeVisible();
  for (const label of ['Overall', 'Humanitarian', 'Maritime', 'Investigations']) {
    await expect(strip.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(strip.getByText(/24 h/i)).toHaveCount(0);
});

test('homepage moves from public surfaces to sources and the evidence model', async ({ page }) => {
  await page.goto(SITE_URL);

  await expect(page.locator('.surface-card[href="https://live.seacommons.org"]')).toBeVisible();
  await expect(page.locator('.surface-card[href="https://play.seacommons.org"]')).toBeVisible();
  await expect(page.locator('.surface-card[href="/docs"]')).toBeVisible();

  await expect(page.locator('#sources')).toContainText('Public reports');
  await expect(page.locator('#sources')).toContainText('AIS');
  await expect(page.locator('#sources')).toContainText('Maritime radio');
  await expect(page.locator('#sources')).toContainText('Satellite');
  await expect(page.locator('#sources')).toContainText('Environment');

  await expect(page.getByRole('heading', { name: /Six stages/i })).toBeVisible();
  await expect(page.getByText('Independence', { exact: true })).toBeVisible();
});

test('homepage states key epistemic and privacy limits plainly', async ({ page }) => {
  await page.goto(SITE_URL);

  await expect(page.getByText(/An extracted coordinate is not automatically a verified coordinate/i)).toBeVisible();
  await expect(page.getByText(/Missing AIS positions do not prove that a vessel was absent or deliberately silent/i)).toBeVisible();
  await expect(page.getByText(/Humanitarian privacy comes before map precision/i)).toBeVisible();
  await expect(page.getByText(/can be incomplete, delayed or wrong/i)).toBeVisible();
});
