# Contributing

Thanks for helping. Bug reports, fixes, and support for more Grafana setups are all welcome.

## Before you start

For anything bigger than a small fix, open an issue first so we can agree on the approach.
Please don't include real tokens, internal hostnames or raw panel error messages from your stack in
issues, logs or test fixtures. Replace them with made-up values.

## Development

The Python code uses only the standard library (Python 3.13). The browser check needs
[k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) with its browser module and a Chromium it
can launch. The Docker image has both.

Run the tests:

```sh
python3 -m unittest -v tests.test_grafana_api tests.test_browser_scan tests.test_loki
```

Run the linters (CI runs both on every PR). ruff checks the Python code, with its settings in
`ruff.toml`; ESLint checks `browser/scan.js`, with its settings in `eslint.config.mjs`:

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

Try a change against a real Grafana:

```sh
CHECKER_MODE=once CHECKER_BASE_URL=https://your-stack.grafana.net CHECKER_LIMIT_DASHBOARDS=5 python3 server.py
```

## Pull requests

- Keep each PR to one change, with tests for the behavior it adds or fixes.
- Update [docs/reference.md](docs/reference.md) when you add or change a setting or a log field.
  Dashboards and alerts are built on the log fields, so treat renames as breaking.
- CI runs the tests on every PR, and checks that the image still builds for `linux/amd64` and
  `linux/arm64`. It must pass before merge.

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
