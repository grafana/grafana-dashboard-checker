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
python3 -m unittest -v tests.test_check_dashboards tests.test_browser_scan
```

Try a change against a real Grafana:

```sh
CHECKER_MODE=once CHECKER_BASE_URL=https://your-stack.grafana.net CHECKER_LIMIT_DASHBOARDS=5 python3 server.py
```

## Pull requests

- Keep each PR to one change, with tests for the behavior it adds or fixes.
- Update [docs/reference.md](docs/reference.md) when you add or change a setting or a log field.
  Dashboards and alerts are built on the log fields, so treat renames as breaking.
- CI runs the tests on every PR. It must pass before merge.
