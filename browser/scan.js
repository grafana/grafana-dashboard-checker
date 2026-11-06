// For each dashboard in DASHBOARDS_FILE, loads it the way an anonymous visitor
// would and looks for Grafana's red panel-status error icon on every panel:
// expands collapsed rows and clicks through every tab so nothing stays hidden.
// Prints one JSON line per dashboard it finished; one it never got to
// (timeout) prints nothing. See scan.py for the full story.
import { browser } from 'k6/browser';
import exec from 'k6/execution';

const HEADER_SELECTOR = '[data-testid^="data-testid Panel header"]';
// The red triangle in a panel's header. Not "Panel data error message": that's
// the "No data" placeholder, which isn't an error.
const ERROR_SELECTOR = '[data-testid="data-testid Panel status error"]';
const COLLAPSED_ROW_SELECTOR = '[data-testid^="data-testid dashboard-row-toggle-for-"][aria-expanded="false"]';
const TAB_SELECTOR = '[role="tab"][data-tab-activation-key]';
// Clicking the red triangle opens this with the error message and some buttons.
const TOGGLETIP_SELECTOR = '[data-testid="toggletip-content"]';
const TOGGLETIP_CLOSE_SELECTOR = '[data-testid="toggletip-header-close"]';

// open() only works at module/init scope, not inside the default function.
const DASHBOARDS = JSON.parse(open(__ENV.DASHBOARDS_FILE));

export const options = {
  scenarios: {
    ui: {
      // One iteration per dashboard, so VUs actually split the work.
      executor: 'shared-iterations',
      // Each VU is its own Chromium; more than a few starves a 2-vCPU runner.
      vus: Math.max(1, Math.min(Number(__ENV.CHECKER_BROWSER_VUS || 3), DASHBOARDS.length)),
      iterations: Math.max(1, DASHBOARDS.length),
      // scan.py sets this below its own kill timeout so k6 exits cleanly.
      maxDuration: __ENV.CHECKER_BROWSER_MAX_DURATION || '55m',
      options: { browser: { type: 'chromium' } },
    },
  },
};

// The functions below run in the page's own JS realm via page.evaluate(); only
// their arguments are in scope, nothing from this file.
// A panel is loading while its loading-bar container holds the bar.
function loadingCount() {
  return Array.from(document.querySelectorAll('[class*="panel-loading-bar-container"]'))
    .filter((c) => c.children.length > 0).length;
}

// Records the panels on the page that haven't been recorded yet, and marks
// them: panels outside a nested tab stay on the page while it's switched.
function collectPanels(headerSel, errorSel) {
  const prefix = 'data-testid Panel header ';
  const headers = Array.from(document.querySelectorAll(headerSel))
    .filter((h) => !h.hasAttribute('data-checker-seen'));
  // The panel's wrapper carries data-viz-panel-key="panel-<id>".
  const panelId = (h) => {
    const el = h.closest('[data-viz-panel-key]');
    const m = el && /^panel-(\d+)$/.exec(el.getAttribute('data-viz-panel-key'));
    return m ? Number(m[1]) : null;
  };
  // Repeated panels share a title and id, so mark each red one to find its
  // icon again to read its error.
  let mark = document.querySelectorAll('[data-checker-red]').length;
  const red = headers.filter((h) => !!h.querySelector(errorSel)).map((h) => {
    h.setAttribute('data-checker-red', String(mark));
    return {
      panel_title: (h.getAttribute('data-testid') || '').slice(prefix.length),
      panel_id: panelId(h),
      mark: mark++,
    };
  });
  for (const h of headers) h.setAttribute('data-checker-seen', '');
  return { panels: headers.length, red };
}

// The error only shows in a toggletip: opens it with a DOM click (k6's own
// click doesn't open it), reads it minus its buttons ("Fix with Assistant",
// "Inspect"), and closes it again. null if any of that doesn't happen.
async function errorMessage(mark, errorSel, tipSel, closeSel) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitFor = async (cond) => {
    for (let i = 0; i < 20 && !cond(); i++) await sleep(100);
    return cond();
  };
  const icon = document.querySelector(`[data-checker-red="${mark}"] ${errorSel}`);
  if (!icon) return null;
  icon.click();
  const tip = (await waitFor(() => document.querySelector(tipSel))) && document.querySelector(tipSel);
  if (!tip) return null;
  const copy = tip.cloneNode(true);
  for (const b of copy.querySelectorAll('button')) b.remove();
  const text = copy.textContent.replace(/\s+/g, ' ').trim().slice(0, 500) || null;
  const close = tip.querySelector(closeSel);
  if (close) close.click();
  await waitFor(() => !document.querySelector(tipSel));
  return text;
}

// Repeated rows copy their tabs with the same activation key, so a tab is its
// key plus which copy it is, in page order.
function listTabs(tabSel) {
  const copies = {};
  return Array.from(document.querySelectorAll(tabSel)).map((t) => {
    const key = t.getAttribute('data-tab-activation-key');
    const copy = copies[key] = (copies[key] ?? -1) + 1;
    return {
      id: `${key}#${copy}`, key, copy,
      title: t.getAttribute('title') || t.textContent.trim(),
      selected: t.getAttribute('aria-selected') === 'true',
    };
  });
}

// Waits until no panel has been loading for stableMs, or capMs passes.
async function waitForPanels(page, capMs, stableMs, pollMs) {
  let elapsed = 0;
  let idleFor = 0;
  while (elapsed < capMs) {
    await page.waitForTimeout(pollMs);
    elapsed += pollMs;
    idleFor = (await page.evaluate(loadingCount)) === 0 ? idleFor + pollMs : 0;
    if (idleFor >= stableMs) return true;
  }
  return false;
}

const secs = (ms) => Math.round(ms / 100) / 10;

// Matches URLs on baseUrl's own origin (scheme, host and port), and nothing
// else, so the token is never sent to a CDN, a plugin's asset host or an
// image some panel embeds.
function sameOriginPattern(baseUrl) {
  const m = /^(https?:\/\/[^/?#]+)/i.exec(baseUrl);
  if (!m) throw new Error(`CHECKER_BASE_URL must be an http(s) URL, got ${baseUrl}`);
  const escaped = m[1].replace(/[.*+?^$()|[\]{}\\]/g, '\\$&');
  return new RegExp('^' + escaped + '(?:[/?#]|$)', 'i');
}

// With a service account token, every request to Grafana itself carries it.
// Routing rather than the context's extraHTTPHeaders, which would send it
// to every host the page touches.
async function authenticate(page, baseUrl, token, orgId) {
  if (!token) return;
  const extra = { Authorization: `Bearer ${token}` };
  if (orgId) extra['X-Grafana-Org-Id'] = String(orgId);
  await page.route(sameOriginPattern(baseUrl), async (route) => {
    await route.continue({ headers: { ...route.request().headers(), ...extra } });
  });
}

export default async function () {
  const dashboard = DASHBOARDS[exec.scenario.iterationInTest];
  if (!dashboard) return;
  const started = Date.now();

  // scan.py always sets this; there's deliberately no default stack.
  const baseUrl = String(__ENV.CHECKER_BASE_URL || '').replace(/\/+$/, '');
  if (!baseUrl) throw new Error('CHECKER_BASE_URL is not set');
  // Off unless asked for: the message can contain hostnames or query text.
  const includeErrorText = __ENV.CHECKER_INCLUDE_ERROR_TEXT === 'true';
  const navTimeoutMs = Number(__ENV.CHECKER_BROWSER_NAV_TIMEOUT_MS || 20000);
  const capMs = Number(__ENV.CHECKER_BROWSER_SETTLE_CAP_MS || 30000);
  const stableMs = Number(__ENV.CHECKER_BROWSER_STABLE_MS || 3000);
  const pollMs = 500;
  const maxRowPasses = 5; // rows can reveal more collapsed rows
  const maxTabs = 200; // one dashboard repeats a row of tabs per country

  // A tall viewport renders most panels at once (they're lazy-loaded), and a
  // fresh context per dashboard avoids "existing browser context must be closed".
  const context = await browser.newContext({ viewport: { width: 1920, height: 6000 } });
  const page = await context.newPage();
  await authenticate(page, baseUrl, __ENV.CHECKER_GRAFANA_TOKEN, __ENV.CHECKER_ORG_ID);
  let panelsSeen = 0;
  const red = [];
  let settled = true;
  let rowsExpanded = 0;
  const tabsVisited = [];
  // Where the time went: loading the page vs waiting for panels to settle.
  let navMs;
  let settleMs = 0;

  async function settle() {
    const t0 = Date.now();
    const ok = await waitForPanels(page, capMs, stableMs, pollMs);
    settleMs += Date.now() - t0;
    return ok;
  }

  // Best effort: a message we couldn't read shouldn't fail the dashboard.
  async function readError(mark) {
    if (!includeErrorText) return null;
    try {
      return await page.evaluate(errorMessage, mark, ERROR_SELECTOR, TOGGLETIP_SELECTOR,
                                 TOGGLETIP_CLOSE_SELECTOR);
    } catch {
      return null;
    }
  }

  // Expand every collapsed row in what's showing, then record what rendered.
  async function checkView(tab) {
    let viewSettled = await settle();
    for (let pass = 0; pass < maxRowPasses; pass++) {
      const toggles = await page.$$(COLLAPSED_ROW_SELECTOR);
      if (toggles.length === 0) break;
      for (const t of toggles) await t.click();
      rowsExpanded += toggles.length;
      viewSettled = await settle();
    }
    settled = settled && viewSettled;
    const found = await page.evaluate(collectPanels, HEADER_SELECTOR, ERROR_SELECTOR);
    panelsSeen += found.panels;
    for (const { mark, ...r } of found.red) red.push({ ...r, tab, error: await readError(mark) });
  }

  try {
    await page.goto(`${baseUrl}/d/${dashboard.uid}`, { waitUntil: 'load', timeout: navTimeoutMs });
    // An empty dashboard, or one that's only rows/tabs, has no panel header yet.
    await page.waitForSelector(`${HEADER_SELECTOR}, [data-testid^="data-testid dashboard-row-toggle-for-"], ${TAB_SELECTOR}`,
                               { timeout: navTimeoutMs });
    navMs = Date.now() - started;

    // Tabs only render the selected one, so visit each in turn. Tabs can
    // contain tabs, hence re-reading the list after every click; whatever is
    // selected at that point is on screen, so it counts as visited too.
    const visited = new Set();
    const markSelected = (tabs) => {
      for (const t of tabs) {
        if (t.selected && !visited.has(t.id)) {
          visited.add(t.id);
          tabsVisited.push(t.title);
        }
      }
    };
    let tabs = await page.evaluate(listTabs, TAB_SELECTOR);
    const current = tabs.find((t) => t.selected);
    await checkView(current ? current.title : null);
    markSelected(tabs);
    for (let i = 0; i < maxTabs; i++) {
      tabs = await page.evaluate(listTabs, TAB_SELECTOR);
      markSelected(tabs);
      const next = tabs.find((t) => !visited.has(t.id));
      if (!next) break;
      visited.add(next.id);
      tabsVisited.push(next.title);
      const copies = await page.$$(`${TAB_SELECTOR}[data-tab-activation-key="${next.key}"]`);
      await copies[next.copy].click();
      await checkView(next.title);
    }

    console.log(JSON.stringify({
      dashboard_uid: dashboard.uid, red, settled,
      panels_seen: panelsSeen, rows_expanded: rowsExpanded, tabs: tabsVisited,
      duration_s: secs(Date.now() - started), nav_s: secs(navMs), settle_s: secs(settleMs),
    }));
  } catch (e) {
    console.log(JSON.stringify({ dashboard_uid: dashboard.uid, load_failed: true,
                                  error: String(e).slice(0, 500),
                                  duration_s: secs(Date.now() - started) }));
  } finally {
    await context.close();
  }
}
