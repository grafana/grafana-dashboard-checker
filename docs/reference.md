# Reference

Details for maintaining the checker. For an overview, see the [README](../README.md).

## How it checks (`dashboard_checker/browser/`)

Every dashboard from `/api/search` gets loaded in a real headless Chromium page and checked for Grafana's
red panel-status error icon (`[data-testid="data-testid Panel status error"]`) — what a human visitor
actually sees. ("No data" placeholders don't count; that's a warning, not an error.) Any panel showing it
makes the dashboard broken.

For each dashboard, `dashboard_checker/browser/scan.js`:

1. Loads it in a tall (6000px) viewport, so lazy-loaded panels render without scrolling.
2. Waits until no panel has been loading for 3s, capped at 30s. A dashboard that hits the cap (live or
   streaming panels that never finish) is reported as `dashboard_unsettled`, not as clean.
3. Expands every collapsed row, repeating if that reveals more, and waits again.
4. Clicks through every tab (Grafana only renders the selected one), doing steps 2–3 in each.
5. Records every red panel's title, id and tab. With `CHECKER_INCLUDE_ERROR_TEXT=true` it also records the
   error message: it clicks the red icon to open the message, reads it, and closes it again (`null` if it
   couldn't, and always `null` when the setting is off).

Uses k6's browser module (`k6 run dashboard_checker/browser/scan.js`, shelled out from `dashboard_checker/browser/scan.py`) rather than
Playwright, to dogfood a Grafana product against Grafana's own Play instance. Dashboards are split across
`CHECKER_BROWSER_VUS` (3) browsers; each is its own Chromium, so more than a few starves a 2-vCPU
runner. The whole pass gets `CHECKER_BROWSER_TIMEOUT_SECONDS` (3600); dashboards it doesn't reach in time
are counted in `dashboards_unchecked` alongside a `scan_timed_out` event. When time runs out, k6 and every
Chromium it started are stopped together; results from dashboards that already finished are kept. If `k6` isn't on `PATH`, the
scan fails (`scan_failed`) rather than reporting nothing as broken. So does a scan where k6 exits with an
error of its own before time runs out (it crashes, is killed, or Chromium won't start): the dashboards it
finished are still reported, and `scan_failed` carries k6's `exit_code`. k6's own error message is only
included in `detail` when `CHECKER_INCLUDE_ERROR_TEXT` is on, since it can quote hostnames or query text.

Other browser settings: `CHECKER_BROWSER_NAV_TIMEOUT_MS` (20000), `CHECKER_BROWSER_SETTLE_CAP_MS`
(30000), `CHECKER_BROWSER_STABLE_MS` (3000).

`CHECKER_SHARD=i/n` (e.g. `2/4`) scans only the dashboards whose uid hashes to shard `i` of `n`, so a
large stack can be split across parallel jobs. For scale: one pass over play.grafana.org takes over an
hour on a 2-vCPU runner, so it's scanned as four shards, each with a 30-minute browser budget
(`CHECKER_BROWSER_TIMEOUT_SECONDS=1800`).

## Settings

Read once at startup; a bad or missing value exits with code 2 before anything is scanned.

| Env var | Default | Meaning |
| --- | --- | --- |
| `CHECKER_BASE_URL` | required | Your Grafana's URL, e.g. `https://your-stack.grafana.net` |
| `CHECKER_GRAFANA_TOKEN` | unset (anonymous) | Service account token. Viewer is enough |
| `CHECKER_ORG_ID` | the token's org | Org to scan, sent as `X-Grafana-Org-Id` |
| `CHECKER_INCLUDE_ERROR_TEXT` | `false` | Record each red panel's error message (see below) |
| `CHECKER_FOLDERS` | all | Comma-separated folder titles or uids to scan |
| `CHECKER_EXCLUDE_FOLDERS` | none | Folder titles or uids to skip |
| `CHECKER_TAGS` | all | Scan only dashboards with at least one of these tags |
| `CHECKER_EXCLUDE_TAGS` | none | Skip dashboards with any of these tags |
| `CHECKER_EXCLUDE_UIDS` | none | Skip these dashboard uids |
| `CHECKER_LIMIT_DASHBOARDS` | `0` (no limit) | Scan only the first N, after filtering |
| `CHECKER_SHARD` | unset | `i/n`: scan only shard `i` of `n` (see above) |
| `CHECKER_MODE` | `serve` | `once` for a single scan (below), `serve` for continuous mode |
| `CHECKER_SCAN_ID` | generated | `once` mode only: the `scan_id` on every event (see below) |

Filters: root-level dashboards are in the folder `General`. Folders match a dashboard's immediate
folder only, not its parents. Excludes win over includes.

### Authentication

Without a token the checker sees what an anonymous visitor sees. With `CHECKER_GRAFANA_TOKEN`, the API
calls send it as a bearer token, and the browser adds it to requests for `CHECKER_BASE_URL`'s own origin
only (via `page.route`), never to CDNs or other hosts a dashboard loads from. The Python client doesn't
carry it across redirects. k6 and Chromium get an allowlisted environment (`PATH`, `HOME`, proxy and
TLS settings, `K6_*`, `CHECKER_BROWSER_*` and the scan's own settings), not the checker's full one, so
`LOKI_TOKEN` never reaches the browser.

The checker only reads, so give it the least access that works:

- A service account with the **Viewer** role for `CHECKER_GRAFANA_TOKEN`, and a Loki access policy scoped
  to `logs:write` for `LOKI_TOKEN`.
- Pass tokens through the environment (`docker run -e NAME`, with no value), never as command-line
  arguments or in files committed to a repository.

### Error text

Off by default. A panel's error message often includes data source hostnames, query text, or errors
from internal services, which you may not want in Loki or anywhere broader. Turn it on when the output
stays somewhere private, or the stack is public anyway, as Play is.

## Known limits

- Variables use their saved defaults, same as a visitor landing on the dashboard.
- App pages (`/a/...`) aren't covered.
- Needs Grafana 11.3 or later; see [Supported Grafana versions](#supported-grafana-versions).
- With a token, Grafana Live (websocket) panels may not authenticate, since only HTTP requests carry it.

## Supported Grafana versions

Grafana **11.3 or later**, self-hosted or Grafana Cloud. 11.3 made the Scenes-based dashboards the
default, and the browser check reads their markup (`data-testid` attributes and `data-viz-panel-key`).

## Scheduled mode (`CHECKER_MODE=once`)

A single scan, no HTTP server: for a cron job, such as a scheduled CI workflow that runs the image this
repo's CI builds (`.github/workflows/ci.yml`). It pushes to Loki instead of just printing, since nothing
ships a short-lived job's logs. Exits 1 if discovery, the browser (including k6 crashing), or the Loki push failed; exits 0 even
when panels are broken -- that's a finding for the dashboard to show, not a reason to fail the job.

Each dashboard's lines go out as soon as the browser finishes it, and reach Loki in batches while the scan
is still running: every 500 lines, or once the oldest unsent line is 30 s old. A push that hits a rate
limit (429), a server error (5xx) or a network error is tried up to 3 times, 2 s then 4 s apart. A batch
that still fails stays queued and goes out with the next one; any line that never arrives fails the run
(exit 1), and a `loki_push_failed` line on stderr says how many are waiting.

Env vars: `LOKI_URL`, `LOKI_USERNAME`, `LOKI_TOKEN` (use an access policy scoped to logs:write only),
`LOKI_JOB_LABEL` (default `dashboard-checker`, the stream's `job` label). If `LOKI_URL` is set, the other two are required and checked before the scan
starts. A sharded run also labels its stream `shard` (e.g. `2/4`).

Log lines (`event=`):

| Event | One per | Fields |
| --- | --- | --- |
| `panel_failed` | red panel | `dashboard_uid`, `dashboard_title`, `folder`, `panel_id`, `panel_title`, `tab`, `error`, `link` |
| `dashboard_unsettled` | dashboard still loading at the cap | `dashboard_uid`, `dashboard_title`, `folder`, `link` |
| `dashboard_load_failed` | dashboard the browser couldn't load | the same, plus `error`, `duration_s` |
| `dashboard_checked` | dashboard the browser loaded | `dashboard_uid`, `dashboard_title`, `folder`, `panels_seen`, `panels_red`, `rows_expanded`, `tabs_visited`, `settled`, `duration_s`, `nav_s` (page load), `settle_s` (waiting for panels) |
| `scan_timed_out` | scan that ran out of time | `shard`, `dashboards_checked`, `dashboards_total` |
| `scan_complete` | scan | `shard`, `dashboards_total`, `dashboards_checked`, `dashboards_broken`, `dashboards_unsettled`, `dashboards_load_failed`, `dashboards_unchecked`, `panels_seen`, `panels_red`, `rows_expanded`, `tabs_visited`, `duration_s`, `grafana_version` (`null` if the instance hides it) |
| `scan_failed` | scan that couldn't run or finish | `shard`, `error`, `detail`; when k6 crashed (`error` is `ScanCrashed`), also `exit_code`, `dashboards_checked`, `dashboards_total`; when no panels were recognised (`NoPanelsRecognised`), also `dashboards_checked`, `dashboards_total`, `grafana_version`. `UnsupportedGrafana` means the version was below 11.3 |

`shard` is `null` when the run isn't sharded. Every event above also has a `scan_id`, the same for all of
one scan's events. Set `CHECKER_SCAN_ID` to the same value in each shard's job (for example the CI run's
id) so a sharded scan's events share one; otherwise each scan generates its own, like
`20261106T060000Z-3fa9c1`. It's a field, not a stream label, so filter on it after `| json`:
`{job="dashboard-checker"} | json | scan_id="20261106T060000Z-3fa9c1" | event="panel_failed"`.

Events come in the order dashboards finish, which isn't the order `/api/search` listed them in.

Build dashboards from `scan_complete` (via LogQL `unwrap` for counts/trends) and `panel_failed` (the
broken-dashboards list/table). With shards, each one sends its own `scan_complete`, so add them up
across the `shard` label, e.g.
`sum(sum_over_time({job="dashboard-checker"} | json | event="scan_complete" | unwrap dashboards_broken [1d]))`.
`dashboard_checked` shows where scan time goes, e.g. `topk(10, ... | event="dashboard_checked" | unwrap duration_s ...)`.

## Continuous mode

`dashboard_checker/server.py` loops the same scan and serves the results. This is what the Dockerfile runs
by default (`python3 -m dashboard_checker`).

```sh
CHECKER_BASE_URL=https://your-stack.grafana.net CHECKER_SCAN_INTERVAL_SECONDS=86400 python3 -m dashboard_checker
```

- `GET /metrics` — Prometheus gauges prefixed `dashboard_checker_`: `up`, `last_scan_timestamp_seconds`,
  `last_scan_duration_seconds`, `last_scan_success`, `dashboards_scanned`, `dashboards_checked`,
  `panels_red`, `dashboard_broken{dashboard_uid,...}` (one series per broken dashboard),
  `dashboards_unsettled`, `dashboards_load_failed`, `dashboards_unchecked`.
- `GET /healthz` / `GET /readyz` (503 until the first scan completes).
- Every log line above is also printed to stdout as JSON, for Loki.

Env vars: the [settings](#settings) above, plus `CHECKER_SCAN_INTERVAL_SECONDS` (900),
`CHECKER_TIMEOUT_SECONDS` (60, for `/api/search`) and `CHECKER_PORT` (8080).

## Test

```sh
python3 -m unittest -v tests.test_grafana_api tests.test_browser_scan tests.test_loki
```
