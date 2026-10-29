// For each candidate panel (read from CANDIDATES_FILE), loads its real
// dashboard, scrolls the panel into view, and checks Grafana's own
// error/no-data test id. Confirmed panels are printed as one JSON line each;
// unconfirmed ones are simply not printed. See confirm.py for the full story.
import { browser } from 'k6/browser';

export const options = {
  scenarios: {
    ui: {
      executor: 'shared-iterations',
      options: { browser: { type: 'chromium' } },
    },
  },
};

const HEADER_SELECTOR = '[data-testid^="data-testid Panel header"]';
const ERROR_SELECTOR = '[data-testid*="Panel data error message"]';

// open() only works at module/init scope, not inside the default function.
const CANDIDATES = JSON.parse(open(__ENV.CANDIDATES_FILE));

// Runs in the page's own JS realm via page.evaluate(); only its arguments
// are in scope, nothing from this file.
function findAndScrollIntoView(headerSel, title) {
  const headers = Array.from(document.querySelectorAll(headerSel));
  const hit = headers.find((h) => (h.textContent || '').trim().startsWith(title));
  if (!hit) return { found: false };
  hit.scrollIntoView({ block: 'center' });
  return { found: true };
}

function readErrorState(headerSel, errorSel, title) {
  const headers = Array.from(document.querySelectorAll(headerSel));
  const hit = headers.find((h) => (h.textContent || '').trim().startsWith(title));
  if (!hit) return { found: false, hasError: false, text: null };
  const el = hit.querySelector(errorSel);
  return { found: true, hasError: !!el, text: el ? el.textContent : null };
}

async function confirmOnePanel(page, panelTitle, appearTimeoutMs, settleMs, pollMs) {
  const scrolled = await page.evaluate(findAndScrollIntoView, HEADER_SELECTOR, panelTitle);
  if (!scrolled.found) return null; // panel renamed/removed since the API scan

  // Scrolling into view is what triggers the panel's query (lazy-loaded).
  let elapsed = 0;
  let state = await page.evaluate(readErrorState, HEADER_SELECTOR, ERROR_SELECTOR, panelTitle);
  while (!state.hasError && elapsed < appearTimeoutMs) {
    await page.waitForTimeout(Math.min(500, appearTimeoutMs - elapsed));
    elapsed += 500;
    state = await page.evaluate(readErrorState, HEADER_SELECTOR, ERROR_SELECTOR, panelTitle);
  }
  if (!state.hasError) return null; // never appeared -> not broken

  // A slow query can show "No data" before the real response lands; poll
  // until it clears or the window elapses, and only trust it if it sticks.
  let text = state.text;
  elapsed = 0;
  while (elapsed < settleMs) {
    await page.waitForTimeout(Math.min(pollMs, settleMs - elapsed));
    elapsed += pollMs;
    state = await page.evaluate(readErrorState, HEADER_SELECTOR, ERROR_SELECTOR, panelTitle);
    if (!state.hasError) return null; // cleared -- the earlier state was a loading flash
    text = state.text || text;
  }
  return text;
}

export default async function () {
  const baseUrl = (__ENV.CHECKER_BASE_URL || 'https://play.grafana.org').replace(/\/+$/, '');
  const candidates = CANDIDATES;
  const navTimeoutMs = Number(__ENV.CHECKER_CONFIRM_NAV_TIMEOUT_MS || 20000);
  const appearTimeoutMs = Number(__ENV.CHECKER_CONFIRM_APPEAR_TIMEOUT_MS || 20000);
  const settleMs = Number(__ENV.CHECKER_CONFIRM_SETTLE_MS || 25000);
  const pollMs = Number(__ENV.CHECKER_CONFIRM_POLL_MS || 1500);

  const byDashboard = {};
  for (const c of candidates) {
    (byDashboard[c.dashboard_uid] = byDashboard[c.dashboard_uid] || []).push(c);
  }

  for (const [dashboardUid, panels] of Object.entries(byDashboard)) {
    // A fresh context per dashboard: reusing newPage() after a previous
    // context was left open raises "existing browser context must be closed".
    const context = await browser.newContext();
    const page = await context.newPage();
    try {
      await page.goto(`${baseUrl}/d/${dashboardUid}`, { waitUntil: 'load', timeout: navTimeoutMs });
      await page.waitForSelector(HEADER_SELECTOR, { timeout: navTimeoutMs });
      for (const c of panels) {
        const confirmedText = await confirmOnePanel(
          page, c.panel_title, appearTimeoutMs, settleMs, pollMs
        );
        if (confirmedText !== null) {
          console.log(JSON.stringify({
            confirmed: true,
            dashboard_uid: c.dashboard_uid,
            panel_id: c.panel_id,
            panel_title: c.panel_title,
            confirmed_text: confirmedText,
          }));
        }
      }
    } catch (e) {
      // A dashboard that fails to load leaves its panels unconfirmed, not "broken".
      console.log(JSON.stringify({ confirmed: false, dashboard_load_failed: true,
                                    dashboard_uid: dashboardUid, error: String(e) }));
    } finally {
      await context.close();
    }
  }
}
