"""Token-mode smoke test: the built image against a real Grafana.

Play's daily scan covers anonymous mode against play.grafana.org. This covers
what it doesn't: a scan with a service account token, where scan.js routes
every request to Grafana through page.route to add the token. It's also what
a k6 base image bump has to get past.

It starts Grafana (anonymous access off) on a Docker network of its own,
provisions three dashboards (one with a panel that errors), creates a Viewer
service account token, runs the checker image in once mode with that token
and one browser, and checks the events it prints:

- every dashboard was checked: one browser takes all three, so a hang after
  a browser's first dashboard (#30) shows up as a timeout;
- exactly the erroring panel is reported, and the working one isn't;
- panels were seen at all, which only happens if the token got past login;
- the run exits 0.

Usage: python3 tests/smoke/token_smoke.py [--image dashboard-checker:smoke]
Needs Docker. Not part of the unit tests: run it on its own.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Pinned by digest, so a Grafana release can't break CI by surprise; bump it by hand.
GRAFANA_IMAGE = ("grafana/grafana:13.3.1"
                 "@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf")
TESTDATA = {"type": "grafana-testdata-datasource", "uid": "smoke-testdata"}
BROKEN_UID, BROKEN_PANEL_ID = "smoke-broken", 2
DASHBOARD_UIDS = [BROKEN_UID, "smoke-clean-1", "smoke-clean-2"]


def docker(*args: str, env: dict[str, str] | None = None, timeout: float = 300) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True,
                         env=env, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {out.stderr.strip()[-500:]}")
    return out.stdout.strip()


class Grafana:
    """Just enough of Grafana's HTTP API, as the admin."""

    def __init__(self, url: str, password: str):
        self.url = url
        self.auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")

    def wait_until_up(self, timeout_s: float = 180) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.call("GET", "/api/health").get("database") == "ok":
                    # The admin user can lag the health check by a moment.
                    self.call("GET", "/api/org")
                    return
            except (urllib.error.URLError, ConnectionError, OSError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError(f"Grafana didn't come up within {timeout_s:.0f}s")


def panel(panel_id: int, title: str, scenario: str, x: int) -> dict:
    return {"id": panel_id, "type": "timeseries", "title": title, "datasource": TESTDATA,
            "gridPos": {"x": x, "y": 0, "w": 12, "h": 8},
            "targets": [{"refId": "A", "datasource": TESTDATA, "scenarioId": scenario}]}


def provision(grafana: Grafana) -> str:
    """Dashboards and a Viewer token; returns the token."""
    grafana.call("POST", "/api/datasources", {"name": "TestData", "access": "proxy", **TESTDATA})
    for uid in DASHBOARD_UIDS:
        panels = [panel(1, "Works", "random_walk", 0)]
        if uid == BROKEN_UID:
            panels.append(panel(BROKEN_PANEL_ID, "Fails", "server_error_500", 12))
        grafana.call("POST", "/api/dashboards/db", {"overwrite": True, "dashboard": {
            "uid": uid, "title": uid, "schemaVersion": 39, "panels": panels}})
    account = grafana.call("POST", "/api/serviceaccounts",
                           {"name": "dashboard-checker-smoke", "role": "Viewer"})
    token = grafana.call("POST", f"/api/serviceaccounts/{account['id']}/tokens",
                         {"name": "smoke"})
    return token["key"]


def check(events: list[dict], exit_code: int) -> list[str]:
    """What's wrong with the run, if anything."""
    problems = []
    if exit_code != 0:
        problems.append(f"checker exited {exit_code}, expected 0")
    kinds = [e.get("event") for e in events]
    for bad in ("scan_failed", "scan_timed_out"):
        if bad in kinds:
            problems.append(f"got {bad}: {next(e for e in events if e['event'] == bad)}")
    summary = next((e for e in events if e.get("event") == "scan_complete"), None)
    if summary is None:
        return [*problems, "no scan_complete event"]
    want = {"dashboards_total": 3, "dashboards_checked": 3, "dashboards_broken": 1,
            "dashboards_load_failed": 0, "dashboards_unchecked": 0, "panels_red": 1}
    got = {k: summary.get(k) for k in want}
    if got != want:
        problems.append(f"scan_complete was {got}, expected {want}")
    failed = [(e.get("dashboard_uid"), e.get("panel_id"))
              for e in events if e.get("event") == "panel_failed"]
    if failed != [(BROKEN_UID, BROKEN_PANEL_ID)]:
        problems.append(f"red panels were {failed}, expected [({BROKEN_UID!r}, {BROKEN_PANEL_ID})]")
    seen = {e.get("dashboard_uid"): e.get("panels_seen")
            for e in events if e.get("event") == "dashboard_checked"}
    # Without the token every dashboard is Grafana's login page: no panels.
    want_seen = {uid: 2 if uid == BROKEN_UID else 1 for uid in DASHBOARD_UIDS}
    if seen != want_seen:
        problems.append(f"panels seen per dashboard were {seen}, expected {want_seen}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", default="dashboard-checker:smoke",
                        help="checker image to test (default: %(default)s)")
    parser.add_argument("--grafana-image", default=GRAFANA_IMAGE)
    parser.add_argument("--keep", action="store_true",
                        help="leave Grafana running afterwards, for debugging")
    args = parser.parse_args()

    suffix = secrets.token_hex(4)
    network, container = f"checker-smoke-{suffix}", f"checker-smoke-grafana-{suffix}"
    # A throwaway container's admin password, never written anywhere.
    password = secrets.token_urlsafe(18)
    docker("network", "create", network)
    try:
        docker("run", "-d", "--name", container, "--network", network,
               "--network-alias", "grafana", "-p", "127.0.0.1::3000",
               "-e", "GF_SECURITY_ADMIN_PASSWORD", "-e", "GF_AUTH_ANONYMOUS_ENABLED=false",
               "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
               "-e", "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
               args.grafana_image,
               env={**os.environ, "GF_SECURITY_ADMIN_PASSWORD": password}, timeout=600)
        host_port = docker("port", container, "3000/tcp").splitlines()[0]
        grafana = Grafana(f"http://{host_port}", password)
        grafana.wait_until_up()
        token = provision(grafana)
        print(f"Grafana {grafana.call('GET', '/api/health').get('version')} ready with "
              f"{len(DASHBOARD_UIDS)} dashboards; running {args.image}", flush=True)

        # The token goes to docker by name (-e NAME), never on the command line.
        run = subprocess.run(
            ["docker", "run", "--rm", "--network", network,
             "-e", "CHECKER_MODE=once", "-e", "CHECKER_BASE_URL=http://grafana:3000",
             "-e", "CHECKER_GRAFANA_TOKEN", "-e", "CHECKER_BROWSER_VUS=1",
             # Three dashboards take well under a minute; a hang shouldn't take 10.
             "-e", "CHECKER_BROWSER_TIMEOUT_SECONDS=180", args.image],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "CHECKER_GRAFANA_TOKEN": token})
        events = []
        for line in run.stdout.splitlines():
            with contextlib.suppress(ValueError):  # k6's own lines aren't JSON
                events.append(json.loads(line))
        problems = check(events, run.returncode)
        if problems:
            print("Token-mode smoke test FAILED:", *(f"  - {p}" for p in problems),
                  "--- checker stdout ---", run.stdout[-4000:],
                  "--- checker stderr ---", run.stderr[-4000:], sep="\n", file=sys.stderr)
            return 1
        summary = events[-1]
        print(f"Token-mode smoke test passed: {summary['dashboards_checked']} dashboards "
              f"checked, {summary['panels_red']} red panel found, in {summary['duration_s']}s.")
        return 0
    finally:
        if args.keep:
            print(f"Left {container} running on {network}.", file=sys.stderr)
        else:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
