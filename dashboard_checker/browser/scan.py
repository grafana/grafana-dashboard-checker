"""Checks dashboards in a real browser (k6), the way a visitor sees them: a
panel showing Grafana's red error icon is broken. This replaced an earlier
approach that replayed each panel's queries through /api/ds/query, which
couldn't see render errors and reported false positives.
Runs scan.js under k6 as a subprocess and reads its output as it arrives, so
each dashboard's result is available as soon as that dashboard is done.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable

SCAN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.js")

# What k6 and its Chromium get from our environment: enough to run, plus the
# scan's own settings. Not everything -- that would include LOKI_TOKEN and
# whatever else the caller happens to hold.
_ENV_PASSTHROUGH = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TZ",
                    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                    "https_proxy", "http_proxy", "no_proxy"}
_ENV_PASSTHROUGH_PREFIXES = ("K6_", "XDG_", "CHECKER_BROWSER_")


# After a kill, how long to wait for k6's last lines, and for k6 itself.
_DRAIN_AFTER_KILL_S = 10.0
_KILL_GRACE_S = 10.0


class BrowserUnavailable(Exception):
    """The k6 binary (with its browser module) isn't available here."""


def k6_env(base_url: str, dashboards_path: str, timeout_s: float, token: str | None,
           org_id: str | None, include_error_text: bool,
           environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment scan.js runs with (see _ENV_PASSTHROUGH)."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items()
           if k in _ENV_PASSTHROUGH or k.startswith(_ENV_PASSTHROUGH_PREFIXES)}
    env["DASHBOARDS_FILE"] = dashboards_path
    env["CHECKER_BASE_URL"] = base_url
    env["CHECKER_INCLUDE_ERROR_TEXT"] = "true" if include_error_text else "false"
    if token:
        env["CHECKER_GRAFANA_TOKEN"] = token
    if org_id:
        env["CHECKER_ORG_ID"] = str(org_id)
    # k6 stops itself a minute before we'd kill it, so it exits with its output intact.
    env.setdefault("CHECKER_BROWSER_MAX_DURATION", f"{max(60, int(timeout_s) - 60)}s")
    return env


def scan_dashboards(base_url: str, dashboards: Iterable[dict],
                    timeout_s: float | None = None, token: str | None = None,
                    org_id: str | None = None, include_error_text: bool = False,
                    on_result: Callable[[str, dict], None] | None = None,
                    ) -> tuple[dict[str, dict], bool]:
    """dashboards: dicts with at least a uid (as /api/search returns them).

    token/org_id: a Grafana service account token (and org) the browser sends
    to base_url's origin only; without one it browses anonymously.
    include_error_text: read each red panel's error message (off by default:
    it can contain hostnames, query text or other internals).
    on_result(uid, result): called with each dashboard's result as soon as
    k6 reports it, while the scan is still running.

    Returns ({dashboard_uid: result}, timed_out) where result is either
    {"red": [{panel_title, panel_id, tab, error}], "settled", "panels_seen",
    "rows_expanded", "tabs"} or {"load_failed": True, "error"}. Dashboards it
    didn't reach before the timeout are absent. Raises BrowserUnavailable only
    if k6 can't run at all.
    """
    dashboards = [{"uid": d["uid"]} for d in dashboards]
    if not dashboards:
        return {}, False
    if timeout_s is None:
        timeout_s = float(os.environ.get("CHECKER_BROWSER_TIMEOUT_SECONDS", "3600"))

    k6 = shutil.which("k6")
    if k6 is None:
        raise BrowserUnavailable("k6 binary not found on PATH")

    # A file, not an env var: hundreds of entries once blew past the OS's
    # execve() arg/env size limit ("Argument list too long").
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="dashboard-checker-dashboards-", delete=False
    ) as f:
        json.dump(dashboards, f)
        dashboards_path = f.name

    env = k6_env(base_url, dashboards_path, timeout_s, token, org_id, include_error_text)
    results: dict[str, dict] = {}
    # The last lines k6 printed, to recognise a missing browser module.
    tail: collections.deque[str] = collections.deque(maxlen=200)

    def handle(line: str) -> None:
        tail.append(line)
        row = _parse_k6_console_line(line)
        if row is None or "dashboard_uid" not in row:
            return
        uid = row.pop("dashboard_uid")
        results[uid] = row
        if on_result is not None:
            on_result(uid, row)

    timed_out = False
    try:
        try:
            proc = subprocess.Popen(
                # --log-format pins the shape _parse_k6_console_line expects.
                [k6, "run", "--log-format=logfmt", SCAN_SCRIPT],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
                # Its own process group, so a kill also reaches the Chromiums it starts.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise BrowserUnavailable(f"k6 not runnable: {exc}") from exc

        lines, reader = _read_lines(proc.stdout)
        deadline = time.monotonic() + timeout_s
        try:
            for line in _until(lines, deadline):
                handle(line)
            if proc.poll() is None and time.monotonic() >= deadline:
                timed_out = True
                _stop(proc)
                # Keep whatever dashboards finished before the kill.
                for line in _until(lines, time.monotonic() + _DRAIN_AFTER_KILL_S):
                    handle(line)
        finally:
            if proc.poll() is None:
                _stop(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_KILL_GRACE_S)
            # The pipe hits EOF once k6 is gone; close it once the reader is done.
            reader.join(timeout=_DRAIN_AFTER_KILL_S)
            proc.stdout.close()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(dashboards_path)

    output = "\n".join(tail)
    if "k6/browser" in output and "command while running browser" in output:
        raise BrowserUnavailable(f"k6 browser module unavailable: {output[-500:]}")

    # k6 hitting maxDuration exits on its own, short of the kill; same outcome.
    timed_out = timed_out or len(results) < len(dashboards)
    return results, timed_out


def _read_lines(stream) -> tuple[queue.Queue, threading.Thread]:
    """Reads stream on a thread into a queue, ending with None at EOF."""
    q: queue.Queue = queue.Queue()

    def pump() -> None:
        try:
            for line in stream:
                q.put(line.rstrip("\n"))
        finally:
            q.put(None)

    thread = threading.Thread(target=pump, daemon=True, name="k6-output")
    thread.start()
    return q, thread


def _until(lines: queue.Queue, deadline: float):
    """Yields lines from the queue until EOF or the deadline, whichever comes first."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            line = lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            return
        yield line


def _stop(proc: subprocess.Popen) -> None:
    """Stops k6 and everything it started: SIGTERM, then SIGKILL if it lingers."""
    for sig, wait_s in ((signal.SIGTERM, _KILL_GRACE_S), (signal.SIGKILL, None)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        if wait_s is None:
            return
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


def parse_k6_output(output: str) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for line in output.splitlines():
        row = _parse_k6_console_line(line)
        if row is None or "dashboard_uid" not in row:
            continue
        uid = row.pop("dashboard_uid")
        results[uid] = row
    return results


def _parse_k6_console_line(line: str) -> dict | None:
    """Pulls the JSON payload out of k6's `msg="..."` log wrapper."""
    marker = 'msg="'
    start = line.find(marker)
    if start == -1 or 'source=console' not in line:
        return None
    start += len(marker)
    end = line.rfind('" source=console')
    if end == -1 or end <= start:
        return None
    raw = line[start:end].replace('\\"', '"').replace("\\\\", "\\")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None
