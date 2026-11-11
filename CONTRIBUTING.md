# Contributing

Thanks for helping. Bug reports, fixes, and support for more Grafana setups are all welcome.

Everyone taking part is expected to follow Grafana Labs' [code of conduct][coc]. To report a security
problem, follow Grafana Labs' [security policy][security] rather than opening a public issue.

[coc]: https://github.com/grafana/.github/blob/main/CODE_OF_CONDUCT.md
[security]: https://github.com/grafana/.github/blob/main/SECURITY.md

## Before you start

For anything bigger than a small fix, open an issue first so we can agree on the approach.
Please don't include real tokens, internal hostnames or raw panel error messages from your stack in
issues, logs or test fixtures. Replace them with made-up values.

## Development

The code is all in `dashboard_checker/`:

| File | What it does |
| --- | --- |
| `server.py` | Settings, one scan (list dashboards, check them, report events), continuous mode with `/metrics` |
| `browser/scan.js` | The k6 browser script that opens each dashboard and finds red panels |
| `browser/scan.py` | Starts k6 and reads each dashboard's result as it finishes |
| `grafana_api.py` | The Grafana API calls: listing dashboards, building links |
| `loki.py` | Pushes the results to Loki in batches |

Run it with `python3 -m dashboard_checker`. The Python code uses only the standard library (Python 3.13). The browser check needs
[k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) with its browser module and a Chromium it
can launch. The Docker image has both.

Run the tests:

```sh
python3 -m unittest -v
```

This picks up every `tests/test_*.py`, so a new test file runs in CI without being listed anywhere.

The unit tests stand in a fake for k6. The token-mode smoke test runs the real image, with a service
account token, against a throwaway Grafana container (Docker required, about a minute):

```sh
docker build -t dashboard-checker:smoke .
python3 tests/smoke/token_smoke.py
```

It checks that every dashboard is checked by a single browser, that exactly the panel that errors is
reported, and that the token got past Grafana's login. `--keep` leaves Grafana running afterwards.

Run the linters (CI runs both on every PR). ruff checks the Python code, with its settings in
`ruff.toml`; ESLint checks `dashboard_checker/browser/scan.js`, with its settings in `eslint.config.mjs`:

```sh
python3 -m pip install -r requirements-dev.txt
ruff check .
```

```sh
npm ci --ignore-scripts
npm run lint
```

`ruff check --fix .` fixes what it safely can. npm is only for ESLint; the checker itself has no npm
dependencies.

`package.json` overrides ESLint's `minimatch` to 11.x for
[GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249), which has no fixed 10.x
release, while ESLint itself only asks for 10.x. Remove the override once ESLint depends on
`minimatch` 11 or later.

Try a change against a real Grafana:

```sh
CHECKER_MODE=once CHECKER_BASE_URL=https://your-stack.grafana.net CHECKER_LIMIT_DASHBOARDS=5 python3 -m dashboard_checker
```

## Pull requests

- Keep each PR to one change, with tests for the behavior it adds or fixes.
- Update [docs/reference.md](docs/reference.md) when you add or change a setting or a log field.
  Dashboards and alerts are built on the log fields, so treat renames as breaking.
- CI runs the tests and the token-mode smoke test on every PR, and checks that the image still
  builds for `linux/amd64` and `linux/arm64`. It must pass before merge.

## Releasing

Maintainers publish a release by tagging a commit on `main` with its version, following
[semantic versioning](https://semver.org):

```sh
git tag v0.1.0
git push origin v0.1.0
```

CI then publishes `ghcr.io/grafana/grafana-dashboard-checker` for both platforms, tagged
`0.1.0`, `0.1` and `latest`. Every push to `main` also publishes a `main` tag and one tagged with
the full commit SHA, but `latest` only ever points at a release.

Before tagging, check that the README and [docs/reference.md](docs/reference.md) match the release.
Renamed settings or log fields are breaking changes, so they need a new major version (or, before
1.0, a new minor version).
