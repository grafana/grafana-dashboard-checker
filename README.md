# Play dashboard checker

Checks public dashboards on a Grafana instance as an anonymous visitor and reports failing panels.

Runs on a schedule against [play.grafana.org](https://play.grafana.org) via `.github/workflows/scan.yml`,
which pulls the image this repo's CI builds (`.github/workflows/ci.yml`) and pushes results to Play's
Grafana Cloud Loki.

## Run

```sh
python3 check_dashboards.py --limit-dashboards 10        # quick check
python3 check_dashboards.py --json results.json          # full run, machine readable
```

Options: `--base-url` (default `https://play.grafana.org`), `--concurrency` (4), `--delay` (1.0s,
for rate-limited sources), `--timeout` (20s), `--show-raw` (local use only). Exit code 1 if any panel errors.

## What it reports

| Status | Meaning |
| --- | --- |
| `ok` | Query returned data |
| `error` | Query failed (missing/unreachable datasource, timeout, permission, query error, 5xx) |
| `no_data` | Query succeeded but returned nothing (warning only) |
| `inconclusive` | Rate limited, or a variable couldn't be resolved |
| `skipped` | Skipped on purpose (BigQuery, library panels) |

## Known limits

- Variables use their saved value; "All"/multi-value results are approximate.
- Only query failures are detected, not visual/plugin render failures.
- Library panels and app pages (`/a/...`) aren't covered.
- The query replay itself has caused false positives (bad intervals, unexpanded Grafana macros) —
  that's what the browser confirmation pass below exists to catch.

## Test

```sh
python3 -m unittest -v tests.test_check_dashboards tests.test_confirmation
```

## Continuous mode

`server.py` loops `check_dashboards.scan()` and serves the results. This is what the Dockerfile runs
by default. Nothing deploys it today; the scheduled scan below is how Play is checked.

```sh
CHECKER_BASE_URL=https://play.grafana.org CHECKER_SCAN_INTERVAL_SECONDS=900 python3 server.py
```

- `GET /metrics` — Prometheus gauges prefixed `play_dashboard_checker_`: `up`, `last_scan_timestamp_seconds`,
  `last_scan_duration_seconds`, `last_scan_success`, `dashboards_scanned`, `panels_total{status}`,
  `dashboard_broken{dashboard_uid,...}` (one series per dashboard), `confirmation_enabled`, `api_false_positives`,
  `dashboards_unconfirmed`.
- `GET /healthz` / `GET /readyz` (503 until the first scan completes).
- Every failing panel and scan summary is also printed to stdout as JSON, for Loki.

Env vars: `CHECKER_BASE_URL`, `CHECKER_SCAN_INTERVAL_SECONDS` (900), `CHECKER_CONCURRENCY` (4),
`CHECKER_THROTTLE_DELAY_SECONDS` (1.0), `CHECKER_TIMEOUT_SECONDS` (60), `CHECKER_PORT` (8080),
`CHECKER_LIMIT_DASHBOARDS` (0 = no limit, local testing only), `CHECKER_CONFIRM_WITH_BROWSER` (true).

## Scheduled mode (`CHECKER_MODE=once`)

A single scan, no HTTP server -- what `.github/workflows/scan.yml` runs daily (06:00 UTC) against Play,
pushing to Loki instead of just printing since there's no cluster shipping logs for it. Exits 1 if
discovery itself failed or the Loki push failed; exits 0 even when panels are confirmed broken --
that's a finding for the dashboard to show, not a reason to redden the workflow run.

Env vars: `LOKI_URL`, `LOKI_USERNAME`, `LOKI_TOKEN` (a dedicated access policy, scoped to logs:write
only), `LOKI_JOB_LABEL` (default `play-dashboard-checker`, the stream's `job` label).

Build dashboards from `event="scan_complete"` lines (`dashboards_total`, `dashboards_broken`, `panels`
fields, via LogQL `unwrap` for counts/trends) and `event="panel_failed"` lines (the broken-dashboards
list/table).

## Browser confirmation (`confirmation/`)

Every dashboard with an `error`-status panel gets loaded once in a real headless Chromium page and
checked for Grafana's red panel-status error icon (`[data-testid="data-testid Panel status error"]`) on
every panel — what a human visitor actually sees, with none of our query/variable/macro guesswork to get
wrong. ("No data" placeholders don't count; that's a warning, not an error.) One panel still showing the
icon after a settle window (slow queries can flash it) is enough to mark the dashboard broken; the check
stops there rather than waiting on each panel. `panel_failed` lines say whether that particular panel was
one of the red ones (`panel_red`). A dashboard that renders clean
logs its flagged panels as `api_false_positive`, never silently dropped. Runs only against that flagged
subset, not the whole fleet every cycle.

Dashboards are split across `CHECKER_CONFIRM_VUS` (3) browsers. The whole pass gets
`CHECKER_CONFIRM_TIMEOUT_SECONDS` (3600); dashboards it doesn't reach in time, or can't load, keep the
API verdict and are reported with `"confirmed": false` on their `panel_failed` lines (counted in
`dashboards_unconfirmed`), alongside a `confirm_timed_out` event. Panels inside collapsed rows never render,
so the browser can't see them fail — same as a visitor who doesn't expand the row.

Uses k6's browser module (`k6 run confirmation/confirm.js`, shelled out from `confirmation/confirm.py`)
rather than Playwright, to dogfood a Grafana product against Grafana's own Play instance. If `k6` isn't
on `PATH`, the service logs `confirm_unavailable` and falls back to unconfirmed results instead of
refusing to run.