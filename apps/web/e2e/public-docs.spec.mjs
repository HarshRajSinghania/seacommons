import { expect, test } from '@playwright/test';

const DOCS_URL = 'http://127.0.0.1:4173/docs.html';

test('public documentation renders bundled editorial markdown', async ({ page }) => {
  await page.goto(DOCS_URL);
  await expect(page.getByRole('heading', { level: 1, name: 'SeaCommons documentation' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 2, name: '02. From observation to investigation' })).toBeVisible();
  await expect(page.getByText('Provenance is not metadata added after analysis.')).toBeVisible();
  await expect(page.locator('a[href*="github.com"]')).toHaveCount(0);
});

test('documentation navigation uses a right-side hamburger drawer', async ({ page }) => {
  await page.goto(DOCS_URL);
  const toggle = page.getByRole('button', { name: 'Open navigation' });
  await expect(toggle).toBeVisible();
  await toggle.click();
  await expect(page.locator('#docs-menu')).toHaveClass(/is-open/);
  await expect(page.locator('#docs-menu a[href="/docs"]')).toContainText('Docs');
  await expect(page.getByRole('button', { name: 'Close navigation' }).first()).toBeVisible();
});
