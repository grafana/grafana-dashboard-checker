# Dashboard checker

Finds broken dashboards on a Grafana stack. It opens every dashboard in a headless browser, the way a
person would, and reports each panel showing Grafana's red error icon. "No data" panels don't count.

It started out checking [play.grafana.org](https://play.grafana.org), which it still scans every day,
but it works on any Grafana that uses the current (Scenes-based) dashboards UI.

## How it works

1. Lists dashboards from `/api/search`, optionally filtered by folder, tag or uid.
2. For each dashboard, k6's browser module (`browser/scan.js`) loads the page in headless Chromium,
   waits for the panels to finish loading, expands collapsed rows, and clicks through every tab.
3. Any panel with the red error icon marks the dashboard as broken.
4. Results are printed as JSON log lines, and can be pushed to Loki or served as Prometheus metrics.

## Scan your own stack

> [!WARNING]
> The scan runs **every query on every dashboard it opens**, just as if someone viewed it. Against
> data sources that bill per query (BigQuery, Athena, CloudWatch, ...) that costs money, and it adds
> load to your data sources. Start small with `CHECKER_LIMIT_DASHBOARDS` or the folder/tag filters,
> and exclude anything expensive.

1. Create a [service account](https://grafana.com/docs/grafana/latest/administration/service-accounts/)
   with the **Viewer** role and add a token. The checker only reads. Skip this if your dashboards are
   public to anonymous visitors.
2. Build the image and run one scan:

   ```sh
   docker build -t dashboard-checker .
   ```

   ```sh
   export CHECKER_GRAFANA_TOKEN=glsa_...   # the token from step 1
   docker run --rm \
     -e CHECKER_MODE=once \
     -e CHECKER_BASE_URL=https://your-stack.grafana.net \
     -e CHECKER_GRAFANA_TOKEN \
     -e CHECKER_LIMIT_DASHBOARDS=10 \
     dashboard-checker
   ```

   Pass the token as `-e CHECKER_GRAFANA_TOKEN` with no value, as above, so it never appears in the
   command line. The browser sends it only to your Grafana's own host.

3. Look for `panel_failed` lines in the output. Each one names the dashboard and panel, with a link.
   Add `-e CHECKER_INCLUDE_ERROR_TEXT=true` to also record each panel's error message. It's off by
   default because error messages can contain hostnames, query text or other internal details.

To keep the results, set `LOKI_URL`, `LOKI_USERNAME` and `LOKI_TOKEN` to push them to Loki, or run
without `CHECKER_MODE=once` to serve `/metrics` and scan on a timer.

Every setting, the log line format, the filters and the known limits are in
[docs/reference.md](docs/reference.md).

## Play's deployment

`.github/workflows/scan.yml` scans play.grafana.org daily at 06:00 UTC, split across four parallel
jobs, and pushes the results to Play's Grafana Cloud Loki. It only runs in the
`grafana/play-dashboard-checker` repository.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a security issue, see [SECURITY.md](SECURITY.md).
