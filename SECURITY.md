# Security policy

## Reporting a vulnerability

Please don't open a public issue for security problems. Email security-team@grafana.com instead, with
steps to reproduce and the version or commit you tested.

## Handling credentials

The checker is designed to need only read access:

- Use a service account with the **Viewer** role for `CHECKER_GRAFANA_TOKEN`, and a Loki access policy
  scoped to `logs:write` for `LOKI_TOKEN`.
- Pass tokens through the environment (`docker run -e NAME` with no value), never as command-line
  arguments or in files committed to a repository.
- The browser sends the Grafana token only to `CHECKER_BASE_URL`'s own origin. k6 and Chromium don't
  receive `LOKI_TOKEN` or other environment variables outside an allowlist.
- Panel error messages are left out unless `CHECKER_INCLUDE_ERROR_TEXT=true`, because they can contain
  internal hostnames or query text.
