import { expect, test } from '@playwright/test';

/**
 * Every view can reach its own bottom.
 *
 * The shell gives a view no scrolling: `body` is `overflow: hidden` at `100vh`,
 * `#root` is a flex column, and each view is a flex child that must declare
 * `overflow: auto` for itself. A view that forgets simply clips — no scrollbar,
 * no error, no console warning. The help view forgot, and the bottom two thirds
 * of it were unreachable in the shipped build while every unit test passed:
 * jsdom has no layout, so nothing in the unit suite can see a clipped element.
 *
 * That makes this a whole class of bug rather than one mistake, and the shape
 * of it is the familiar one — a convention held by every view individually and
 * asserted nowhere. So this checks the property directly, in a real browser, at
 * a viewport short enough to force the overflow: navigate, then reach the last
 * thing on the page.
 *
 * It runs against the built bundle under `vite preview` with no daemon, which
 * is why the routes chosen are the ones that render in full while
 * disconnected. Help and about are exactly that — and are also the longest
 * views in the UI, which is why they were the ones that broke.
 */

/** Short enough that any of these views overflows several times over. */
const SHORT = { width: 1000, height: 500 };

const VIEWS = [
  {
    route: '#/help',
    view: '.helpview',
    /** The last section, so reaching it means the whole view is reachable. */
    last: 'Further reading',
  },
  {
    route: '#/about',
    view: '.aboutview',
    last: 'Source and licence',
  },
];

for (const { route, view, last } of VIEWS) {
  test(`${route} can be scrolled to its end`, async ({ page }) => {
    await page.setViewportSize(SHORT);
    await page.goto(`/${route}`, { waitUntil: 'domcontentloaded' });

    const container = page.locator(view);
    await expect(container).toBeVisible();

    // Guard against a vacuous pass: if the view happens to fit, reaching its
    // bottom proves nothing about whether it could scroll.
    const overflows = await container.evaluate((el) => el.scrollHeight > el.clientHeight + 1);
    expect(
      overflows,
      `${view} must overflow at ${SHORT.height}px for this test to mean anything`,
    ).toBe(true);

    const heading = page.getByRole('heading', { name: last });
    await heading.scrollIntoViewIfNeeded();
    await expect(heading).toBeInViewport();
  });

  test(`${route} scrolls the view, not the document`, async ({ page }) => {
    // If the fix were `body { overflow: auto }` the assertion above would also
    // pass, and the sticky status bar and nav would scroll away with it. The
    // shell's contract is that the document never scrolls.
    await page.setViewportSize(SHORT);
    await page.goto(`/${route}`, { waitUntil: 'domcontentloaded' });
    await page.locator(view).evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    expect(await page.evaluate(() => window.scrollY)).toBe(0);
    await expect(page.getByRole('navigation', { name: 'Views' })).toBeInViewport();
  });
}
