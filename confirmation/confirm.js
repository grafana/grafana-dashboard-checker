// For each dashboard with candidate panels (read from CANDIDATES_FILE), loads
// it once and looks for Grafana's red panel-status error icon across every
// panel on the page: one that stays red is enough to call the dashboard broken.
// Prints one JSON line per dashboard it finished checking; a dashboard it
// never got to (timeout) prints nothing. See confirm.py for the full story.
import { browser } from 'k6/browser';
import exec from 'k6/execution';

const HEADER_SELECTOR = '[data-testid^="data-testid Panel header"]';
// The red triangle in a panel's header. Not "Panel data error message": that's
// the "No data" placeholder, which isn't an error.
const ERROR_SELECTOR = '[data-testid="data-testid Panel status error"]';

// open() only works at module/init scope, not inside the default function.
const CANDIDATES = JSON.parse(open(__ENV.CANDIDATES_FILE));

// One iteration per dashboard, so VUs actually split the work.
const DASHBOARDS = (() => {
  const byUid = {};
  for (const c of CANDIDATES) {
    (byUid[c.dashboard_uid] = byUid[c.dashboard_uid] || []).push(c.panel_title);
  }
  return Object.entries(byUid).map(([uid, titles]) => ({ uid, titles }));
})();

export const options = {
  scenarios: {
    ui: {
      executor: 'shared-iterations',
      // Each VU is its own Chromium; more than a few starves a 2-vCPU runner.
      vus: Math.max(1, Math.min(Number(__ENV.CHECKER_CONFIRM_VUS || 3), DASHBOARDS.length)),
      iterations: Math.max(1, DASHBOARDS.length),
      // confirm.py sets this below its own kill timeout so k6 exits cleanly.
      maxDuration: __ENV.CHECKER_CONFIRM_MAX_DURATION || '55m',
      options: { browser: { type: 'chromium' } },
    },
  },
};

// Runs in the page's own JS realm via page.evaluate(); only its arguments
// are in scope, nothing from this file.
function scrollIntoView(headerSel, title) {
  const headers = Array.from(document.querySelectorAll(headerSel));
  const hit = headers.find((h) => (h.textContent || '').trim().startsWith(title));
  if (hit) hit.scrollIntoView({ block: 'center' });
}

// The error message itself only lives in a hover tooltip, so just the titles.
function readErrors(headerSel, errorSel) {
  const prefix = 'data-testid Panel header ';
  return Array.from(document.querySelectorAll(headerSel))
    .filter((h) => !!h.querySelector(errorSel))
    .map((h) => ({ panel_title: (h.getAttribute('data-testid') || '').slice(prefix.length) }));
}

// Waits for any panel to turn red, then for it to stay red through the settle
// window (slow queries can flash it). Returns what's still red at the end.
async function checkDashboard(page, appearTimeoutMs, settleMs, pollMs) {
  let elapsed = 0;
  let errors = await page.evaluate(readErrors, HEADER_SELECTOR, ERROR_SELECTOR);
  while (errors.length === 0 && elapsed < appearTimeoutMs) {
    await page.waitForTimeout(Math.min(500, appearTimeoutMs - elapsed));
    elapsed += 500;
    errors = await page.evaluate(readErrors, HEADER_SELECTOR, ERROR_SELECTOR);
  }
  if (errors.length === 0) return [];

  elapsed = 0;
  while (elapsed < settleMs) {
    await page.waitForTimeout(Math.min(pollMs, settleMs - elapsed));
    elapsed += pollMs;
  }
  return page.evaluate(readErrors, HEADER_SELECTOR, ERROR_SELECTOR);
}

export default async function () {
  const dashboard = DASHBOARDS[exec.scenario.iterationInTest];
  if (!dashboard) return;

  const baseUrl = (__ENV.CHECKER_BASE_URL || 'https://play.grafana.org').replace(/\/+$/, '');
  const navTimeoutMs = Number(__ENV.CHECKER_CONFIRM_NAV_TIMEOUT_MS || 20000);
  const appearTimeoutMs = Number(__ENV.CHECKER_CONFIRM_APPEAR_TIMEOUT_MS || 20000);
  const settleMs = Number(__ENV.CHECKER_CONFIRM_SETTLE_MS || 25000);
  const pollMs = Number(__ENV.CHECKER_CONFIRM_POLL_MS || 1500);

  // A tall viewport renders most panels at once (they're lazy-loaded), and a
  // fresh context per dashboard avoids "existing browser context must be closed".
  const context = await browser.newContext({ viewport: { width: 1920, height: 6000 } });
  const page = await context.newPage();
  try {
    await page.goto(`${baseUrl}/d/${dashboard.uid}`, { waitUntil: 'load', timeout: navTimeoutMs });
    await page.waitForSelector(HEADER_SELECTOR, { timeout: navTimeoutMs });
    // Anything still below the fold: scroll the flagged panels in so they query.
    for (const title of dashboard.titles) {
      await page.evaluate(scrollIntoView, HEADER_SELECTOR, title);
    }
    const errors = await checkDashboard(page, appearTimeoutMs, settleMs, pollMs);
    console.log(JSON.stringify({ dashboard_uid: dashboard.uid, broken: errors.length > 0, errors }));
  } catch (e) {
    // Couldn't load it: leave it unchecked so the API verdict stands.
    console.log(JSON.stringify({ dashboard_uid: dashboard.uid, load_failed: true,
                                  error: String(e) }));
  } finally {
    await context.close();
  }
}
