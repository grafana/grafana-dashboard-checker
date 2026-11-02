# Play dashboard checker

Finds broken dashboards on [play.grafana.org](https://play.grafana.org). It opens every public dashboard
the way an anonymous visitor would and reports any panel showing Grafana's red error icon. Results go to
Play's Grafana Cloud Loki once a day.

## How it works

1. A scheduled GitHub Actions workflow (`.github/workflows/scan.yml`) runs every day at 06:00 UTC.
2. It lists every dashboard from `/api/search`.
3. For each dashboard, k6's browser module (`browser/scan.js`) loads the page in headless Chromium,
   waits for the panels to finish loading, expands collapsed rows, and clicks through every tab.
4. Any panel with the red error icon marks the dashboard as broken. "No data" panels don't count.
5. Each broken panel, plus a summary of the whole scan, is pushed to Loki as a log line, ready for a
   Grafana dashboard to show.

Settings, log fields, other run modes and tests are in [docs/reference.md](docs/reference.md).
