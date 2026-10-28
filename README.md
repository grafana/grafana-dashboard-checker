# Play dashboard checker

Checks public dashboards on a Grafana instance as an anonymous visitor and reports failing panels.

Deployed by `play-backend/dashboard-checker` in [grafana/grafana-play](https://github.com/grafana/grafana-play),
which consumes the image this repo's CI builds and pushes (see `.github/workflows/ci.yml`).

## Run

```sh
python3 check_dashboards.py --limit-dashboards 10        # quick check
python3 check_dashboards.py --json results.json          # full run, machine readable
```

Options: `--base-url` (default `https://playdev.grafana.net`), `--concurrency` (4), `--delay` (1.0s,
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
python3 -m unittest -v tests.test_check_dashboards
```

## Continuous mode

`server.py` loops `check_dashboards.scan()` and serves the results. This is what the Dockerfile runs.

```sh
CHECKER_BASE_URL=https://playdev.grafana.net CHECKER_SCAN_INTERVAL_SECONDS=900 python3 server.py
```

- `GET /metrics` — Prometheus, one series per dashboard (see the module README for the full list).
- `GET /healthz` / `GET /readyz` (503 until the first scan completes).
- Every failing panel and scan summary is also printed to stdout as JSON, for Loki.

Env vars: `CHECKER_BASE_URL`, `CHECKER_SCAN_INTERVAL_SECONDS` (900), `CHECKER_CONCURRENCY` (4),
`CHECKER_THROTTLE_DELAY_SECONDS` (1.0), `CHECKER_TIMEOUT_SECONDS` (60), `CHECKER_PORT` (8080),
`CHECKER_LIMIT_DASHBOARDS` (0 = no limit, local testing only), `CHECKER_CONFIRM_WITH_BROWSER` (true).

## Browser confirmation (`confirmation/`)

Every `error`-status panel gets loaded in a real headless Chromium page and checked against Grafana's
own error test id (`[data-testid*="Panel data error message"]`) — what a human visitor actually sees,
with none of our query/variable/macro guesswork to get wrong. Only panels still showing that element
after a settle window (slow queries can flash it) mark their dashboard broken; anything the API flagged
but the browser didn't confirm is logged as `api_false_positive`, never silently dropped. Runs only
against that small flagged subset, not the whole fleet every cycle.

Uses k6's browser module (`k6 run confirmation/confirm.js`, shelled out from `confirmation/confirm.py`)
rather than Playwright, to dogfood a Grafana product against Grafana's own Play instance. If `k6` isn't
on `PATH`, the service logs `confirm_unavailable` and falls back to unconfirmed results instead of
refusing to run.

Gotchas worth knowing before touching `confirm.js`:
- `browser.newPage()` needs its own `newContext()` per dashboard — reusing one after a previous context
  was left open raises `existing browser context must be closed`.
- Chained `locator(sel, { hasText })` text matching is broken on k6 v2.2.0 (`queryAll is not a
  function`); `confirm.js` reads panels via plain `page.evaluate()` + `querySelectorAll` instead.
- Candidates pass through a **file** (`CANDIDATES_FILE`, read via `open()` at init scope), not an env
  var — a full-fleet scan's candidate list is big enough to hit the OS's `execve()` size limit.

The Dockerfile builds `FROM grafana/k6:latest-with-browser` (k6 + a matched Chromium) plus plain
`apk add python3` — no pip dependencies anywhere in this service.
