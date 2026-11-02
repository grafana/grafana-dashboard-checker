# Play dashboard checker

Checks public dashboards on a Grafana instance as an anonymous visitor and reports failing panels.

Runs on a schedule against [play.grafana.org](https://play.grafana.org) via `.github/workflows/scan.yml`,
which pulls the image this repo's CI builds (`.github/workflows/ci.yml`) and pushes results to Play's
Grafana Cloud Loki.

## How it checks (`browser/`)

Every dashboard from `/api/search` gets loaded in a real headless Chromium page and checked for Grafana's
red panel-status error icon (`[data-testid="data-testid Panel status error"]`) — what a human visitor
actually sees. ("No data" placeholders don't count; that's a warning, not an error.) Any panel showing it
makes the dashboard broken.

For each dashboard, `browser/scan.js`:

1. Loads it in a tall (6000px) viewport, so lazy-loaded panels render without scrolling.
2. Waits until no panel has been loading for 3s, capped at 30s. A dashboard that hits the cap (live or
   streaming panels that never finish) is reported as `dashboard_unsettled`, not as clean.
3. Expands every collapsed row, repeating if that reveals more, and waits again.
4. Clicks through every tab (Grafana only renders the selected one), doing steps 2–3 in each.
5. Records every red panel's title, id and tab.

Uses k6's browser module (`k6 run browser/scan.js`, shelled out from `browser/scan.py`) rather than
Playwright, to dogfood a Grafana product against Grafana's own Play instance. Dashboards are split across
`CHECKER_BROWSER_VUS` (3) browsers; each is its own Chromium, so more than a few starves a 2-vCPU
runner. The whole pass gets `CHECKER_BROWSER_TIMEOUT_SECONDS` (3600); dashboards it doesn't reach in time
are counted in `dashboards_unchecked` alongside a `scan_timed_out` event. If `k6` isn't on `PATH`, the
scan fails (`scan_failed`) rather than reporting nothing as broken.

Other browser settings: `CHECKER_BROWSER_NAV_TIMEOUT_MS` (20000), `CHECKER_BROWSER_SETTLE_CAP_MS`
(30000), `CHECKER_BROWSER_STABLE_MS` (3000).

## Known limits

- The error message itself only appears in a hover tooltip, so it isn't recorded; open the panel link.
- Variables use their saved defaults, same as a visitor landing on the dashboard.
- App pages (`/a/...`) aren't covered.

## Scheduled mode (`CHECKER_MODE=once`)

A single scan, no HTTP server -- what `.github/workflows/scan.yml` runs daily (06:00 UTC) against Play,
pushing to Loki instead of just printing since there's no cluster shipping logs for it. Exits 1 if
discovery, the browser, or the Loki push failed; exits 0 even when panels are broken -- that's a finding
for the dashboard to show, not a reason to redden the workflow run.

Env vars: `LOKI_URL`, `LOKI_USERNAME`, `LOKI_TOKEN` (a dedicated access policy, scoped to logs:write
only), `LOKI_JOB_LABEL` (default `play-dashboard-checker`, the stream's `job` label).

Log lines (`event=`):

| Event | One per | Fields |
| --- | --- | --- |
| `panel_failed` | red panel | `dashboard_uid`, `dashboard_title`, `folder`, `panel_id`, `panel_title`, `tab`, `link` |
| `dashboard_unsettled` | dashboard still loading at the cap | `dashboard_uid`, `dashboard_title`, `folder`, `link` |
| `dashboard_load_failed` | dashboard the browser couldn't load | the same, plus `error` |
| `scan_timed_out` | scan that ran out of time | `dashboards_checked`, `dashboards_total` |
| `scan_complete` | scan | `dashboards_total`, `dashboards_checked`, `dashboards_broken`, `dashboards_unsettled`, `dashboards_load_failed`, `dashboards_unchecked`, `panels_seen`, `panels_red`, `rows_expanded`, `tabs_visited`, `duration_s` |
| `scan_failed` | scan that couldn't run | `error`, `detail` |

Build dashboards from `scan_complete` (via LogQL `unwrap` for counts/trends) and `panel_failed` (the
broken-dashboards list/table).

## Continuous mode

`server.py` loops the same scan and serves the results. This is what the Dockerfile runs by default.
Nothing deploys it today; the scheduled scan above is how Play is checked.

```sh
CHECKER_BASE_URL=https://play.grafana.org CHECKER_SCAN_INTERVAL_SECONDS=86400 python3 server.py
```

- `GET /metrics` — Prometheus gauges prefixed `play_dashboard_checker_`: `up`, `last_scan_timestamp_seconds`,
  `last_scan_duration_seconds`, `last_scan_success`, `dashboards_scanned`, `dashboards_checked`,
  `panels_red`, `dashboard_broken{dashboard_uid,...}` (one series per broken dashboard),
  `dashboards_unsettled`, `dashboards_load_failed`, `dashboards_unchecked`.
- `GET /healthz` / `GET /readyz` (503 until the first scan completes).
- Every log line above is also printed to stdout as JSON, for Loki.

Env vars: `CHECKER_BASE_URL`, `CHECKER_SCAN_INTERVAL_SECONDS` (900), `CHECKER_TIMEOUT_SECONDS` (60, for
`/api/search`), `CHECKER_PORT` (8080), `CHECKER_LIMIT_DASHBOARDS` (0 = no limit, local testing only).

## Query replay (`check_dashboards.py`)

The original approach: replays each panel's queries against `/api/ds/query` and classifies the responses.
It's no longer part of the scheduled scan (it can't see render errors, and produced false positives on
bad intervals and unexpanded Grafana macros), but it still gives a failure category for a panel, which the
browser can't.

```sh
python3 check_dashboards.py --limit-dashboards 10        # quick check
python3 check_dashboards.py --json results.json          # full run, machine readable
```

Options: `--base-url` (default `https://play.grafana.org`), `--concurrency` (4), `--delay` (1.0s,
for rate-limited sources), `--timeout` (20s), `--show-raw` (local use only). Exit code 1 if any panel errors.

| Status | Meaning |
| --- | --- |
| `ok` | Query returned data |
| `error` | Query failed (missing/unreachable datasource, timeout, permission, query error, 5xx) |
| `no_data` | Query succeeded but returned nothing (warning only) |
| `inconclusive` | Rate limited, or a variable couldn't be resolved |
| `skipped` | Skipped on purpose (BigQuery, library panels) |

## Test

```sh
python3 -m unittest -v tests.test_check_dashboards tests.test_browser_scan
```
