#!/usr/bin/env python3
"""Runs check_dashboards.scan() on a timer, serving /metrics, /healthz, /readyz.

ERROR-status panels are confirmed by confirm.confirm_broken_panels() (see
confirmation/confirm.py) before being reported broken; unconfirmed ones are
logged as api_false_positive instead of being dropped. Per-panel detail goes
to stdout as JSON lines, not metric labels.

CHECKER_MODE=once runs a single scan and exits instead of serving forever
(see run_once()) -- used by the GitHub Actions cron run, which pushes those
JSON lines to Loki directly since there's no cluster log shipping there.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import check_dashboards as checker
import confirmation.confirm as confirm

METRIC_PREFIX = "play_dashboard_checker"

_label_sanitize_re = re.compile(r'["\\\n]')


def _label(value: str, max_len: int = 200) -> str:
    """Escape a string for use as a Prometheus label value."""
    value = _label_sanitize_re.sub(" ", value or "")
    if len(value) > max_len:
        value = value[: max_len - 1] + "…"
    return value.replace("\\", "\\\\").replace('"', '\\"')


class State:
    """Latest scan results, shared between the scan loop and the HTTP server."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ready = False
        self.last_scan_ts: float | None = None
        self.last_scan_duration_s: float = 0.0
        self.last_scan_ok = False
        self.last_scan_error: str | None = None
        self.panel_counts: dict[str, int] = {}
        self.broken_dashboards: list[dict] = []  # [{uid, title, folder}]
        self.dashboard_count = 0
        self.confirmation_enabled = False
        self.api_false_positive_count = 0


def _emit(log_sink: list[tuple[int, str]] | None, event: dict) -> None:
    line = json.dumps(event)
    print(line, flush=True)
    if log_sink is not None:
        log_sink.append((time.time_ns(), line))


def run_scan_once(state: State, base_url: str, concurrency: int, delay: float,
                   timeout: float, limit_dashboards: int, want_confirm: bool,
                   log_sink: list[tuple[int, str]] | None = None) -> None:
    started = time.time()
    try:
        dashboards, results = checker.scan(base_url, concurrency, delay, timeout,
                                            limit_dashboards)
    except Exception as exc:  # discovery itself failed (search/settings unreachable)
        duration = time.time() - started
        with state.lock:
            state.last_scan_ts = time.time()
            state.last_scan_duration_s = duration
            state.last_scan_ok = False
            state.last_scan_error = type(exc).__name__
            state.ready = True
        _emit(log_sink, {"event": "scan_failed", "error": type(exc).__name__,
                          "detail": str(exc)[:500]})
        return

    counts: dict[str, int] = {}
    by_dashboard: dict[str, dict] = {}
    error_candidates: list[dict] = []
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        by_dashboard.setdefault(r["dashboard_uid"], {
            "uid": r["dashboard_uid"], "title": r.get("dashboard_title") or "",
            "folder": r.get("folder") or "General", "broken": False,
        })
        if r["status"] == checker.ERROR:
            error_candidates.append(r)

    confirmation_enabled = want_confirm
    confirmed_keys: set[tuple] = set()
    confirmed_text: dict[tuple, str] = {}
    if confirmation_enabled and error_candidates:
        try:
            confirmed = confirm.confirm_broken_panels(base_url, error_candidates)
            confirmed_keys = {(c["dashboard_uid"], c["panel_id"]) for c in confirmed}
            confirmed_text = {(c["dashboard_uid"], c["panel_id"]): c["confirmed_text"]
                               for c in confirmed}
        except confirm.BrowserUnavailable as exc:
            confirmation_enabled = False
            print(json.dumps({"event": "confirm_unavailable", "detail": str(exc)[:500]}),
                  flush=True)
        except Exception as exc:
            # k6 itself broke; fall back to trusting the API result this cycle.
            confirmation_enabled = False
            print(json.dumps({"event": "confirm_failed", "error": type(exc).__name__,
                               "detail": str(exc)[:500]}), flush=True)

    api_false_positives = 0
    for r in error_candidates:
        key = (r["dashboard_uid"], r["panel_id"])
        is_confirmed = (not confirmation_enabled) or key in confirmed_keys
        if is_confirmed:
            by_dashboard[r["dashboard_uid"]]["broken"] = True
            _emit(log_sink, {
                "event": "panel_failed",
                "dashboard_uid": r["dashboard_uid"],
                "dashboard_title": r.get("dashboard_title"),
                "folder": r.get("folder") or "General",
                "panel_id": r.get("panel_id"),
                "panel_title": r.get("panel_title"),
                "category": r.get("category"),
                "link": checker.panel_link(base_url, r),
                "confirmed_text": confirmed_text.get(key),
            })
        else:
            api_false_positives += 1
            _emit(log_sink, {
                "event": "api_false_positive",
                "dashboard_uid": r["dashboard_uid"],
                "dashboard_title": r.get("dashboard_title"),
                "panel_id": r.get("panel_id"),
                "panel_title": r.get("panel_title"),
                "category": r.get("category"),
                "link": checker.panel_link(base_url, r),
            })

    duration = time.time() - started
    broken = [d for d in by_dashboard.values() if d["broken"]]
    _emit(log_sink, {
        "event": "scan_complete", "dashboards_total": len(dashboards),
        "panels": len(results), "dashboards_broken": len(broken),
        "confirmation_enabled": confirmation_enabled, "api_false_positives": api_false_positives,
        "duration_s": round(duration, 1), **{f"status_{k}": v for k, v in counts.items()},
    })

    with state.lock:
        state.last_scan_ts = time.time()
        state.last_scan_duration_s = duration
        state.last_scan_ok = True
        state.last_scan_error = None
        state.panel_counts = counts
        state.broken_dashboards = broken
        state.dashboard_count = len(dashboards)
        state.confirmation_enabled = confirmation_enabled
        state.api_false_positive_count = api_false_positives
        state.ready = True


def run_once(base_url: str, concurrency: int, delay: float, timeout: float,
             limit_dashboards: int, want_confirm: bool) -> int:
    """Single scan with no HTTP server, for the GitHub Actions cron run.

    Pushes the buffered log lines to Loki when LOKI_URL is set (unset is for
    local testing without credentials); a push failure fails the run.
    """
    state = State()
    log_sink: list[tuple[int, str]] = []
    run_scan_once(state, base_url, concurrency, delay, timeout, limit_dashboards, want_confirm,
                  log_sink=log_sink)

    loki_url = os.environ.get("LOKI_URL")
    if loki_url:
        import loki
        labels = {"job": os.environ.get("LOKI_JOB_LABEL", "play-dashboard-checker")}
        loki.push(loki_url, os.environ["LOKI_USERNAME"], os.environ["LOKI_TOKEN"], labels, log_sink)

    return 0 if state.last_scan_ok else 1


def scan_loop(state: State, base_url: str, interval_s: int, concurrency: int,
              delay: float, timeout: float, limit_dashboards: int, want_confirm: bool,
              stop: threading.Event) -> None:
    while not stop.is_set():
        run_scan_once(state, base_url, concurrency, delay, timeout, limit_dashboards, want_confirm)
        stop.wait(interval_s)


def render_metrics(state: State) -> bytes:
    lines: list[str] = []

    def gauge(name: str, help_text: str) -> None:
        lines.append(f"# HELP {METRIC_PREFIX}_{name} {help_text}")
        lines.append(f"# TYPE {METRIC_PREFIX}_{name} gauge")

    with state.lock:
        ready = state.ready
        last_ts = state.last_scan_ts
        duration = state.last_scan_duration_s
        ok = state.last_scan_ok
        counts = dict(state.panel_counts)
        broken = list(state.broken_dashboards)
        dashboard_count = state.dashboard_count
        confirmation_enabled = state.confirmation_enabled
        api_false_positive_count = state.api_false_positive_count

    gauge("up", "1 if the process is serving requests.")
    lines.append(f"{METRIC_PREFIX}_up 1")

    gauge("last_scan_timestamp_seconds", "Unix time the last scan finished.")
    lines.append(f"{METRIC_PREFIX}_last_scan_timestamp_seconds {last_ts or 0}")

    gauge("last_scan_duration_seconds", "Wall clock time of the last scan.")
    lines.append(f"{METRIC_PREFIX}_last_scan_duration_seconds {duration}")

    gauge("last_scan_success", "1 if the last scan completed, 0 if discovery itself failed.")
    lines.append(f"{METRIC_PREFIX}_last_scan_success {1 if ok else 0}")

    gauge("dashboards_scanned", "Dashboards covered by the last scan.")
    lines.append(f"{METRIC_PREFIX}_dashboards_scanned {dashboard_count}")

    lines.append(f"# HELP {METRIC_PREFIX}_panels_total Panel query groups by status in the last scan.")
    lines.append(f"# TYPE {METRIC_PREFIX}_panels_total gauge")
    for status in (checker.OK, checker.ERROR, checker.NO_DATA, checker.INCONCLUSIVE, checker.SKIPPED):
        lines.append(f'{METRIC_PREFIX}_panels_total{{status="{status}"}} {counts.get(status, 0)}')

    lines.append(f"# HELP {METRIC_PREFIX}_dashboard_broken 1 if the dashboard has a browser-confirmed failing panel"
                 " (or any failing panel, when confirmation is disabled).")
    lines.append(f"# TYPE {METRIC_PREFIX}_dashboard_broken gauge")
    for d in broken:
        lines.append(
            f'{METRIC_PREFIX}_dashboard_broken{{dashboard_uid="{_label(d["uid"])}",'
            f'dashboard_title="{_label(d["title"])}",folder="{_label(d["folder"])}"}} 1'
        )

    gauge("confirmation_enabled", "1 if the headless-browser confirmation pass ran for the last scan.")
    lines.append(f"{METRIC_PREFIX}_confirmation_enabled {1 if confirmation_enabled else 0}")

    gauge("api_false_positives", "ERROR-status panels the API replay flagged that the browser did not confirm.")
    lines.append(f"{METRIC_PREFIX}_api_false_positives {api_false_positive_count}")

    if not ready:
        lines.append(f"# {METRIC_PREFIX}: no scan has completed yet")

    return ("\n".join(lines) + "\n").encode()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path == "/metrics":
                self._send(200, render_metrics(state), "text/plain; version=0.0.4")
            elif self.path == "/healthz":
                self._send(200, b"ok")
            elif self.path == "/readyz":
                with state.lock:
                    ready = state.ready
                self._send(200 if ready else 503, b"ok" if ready else b"not ready")
            else:
                self._send(404, b"not found")

        def log_message(self, fmt: str, *args) -> None:  # noqa: A002
            pass  # access logs add noise; scan_complete/panel_failed lines carry signal.

    return Handler


def main() -> int:
    base_url = os.environ.get("CHECKER_BASE_URL", "https://play.grafana.org")
    interval_s = int(os.environ.get("CHECKER_SCAN_INTERVAL_SECONDS", "900"))
    concurrency = int(os.environ.get("CHECKER_CONCURRENCY", "4"))
    delay = float(os.environ.get("CHECKER_THROTTLE_DELAY_SECONDS", "1.0"))
    timeout = float(os.environ.get("CHECKER_TIMEOUT_SECONDS", "60"))
    port = int(os.environ.get("CHECKER_PORT", "8080"))
    limit_dashboards = int(os.environ.get("CHECKER_LIMIT_DASHBOARDS", "0"))
    want_confirm = os.environ.get("CHECKER_CONFIRM_WITH_BROWSER", "true").lower() not in ("0", "false", "no")

    if os.environ.get("CHECKER_MODE", "serve") == "once":
        return run_once(base_url, concurrency, delay, timeout, limit_dashboards, want_confirm)

    state = State()
    stop = threading.Event()

    # Serve /healthz before the first scan runs, so a slow scan can't delay it.
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))
    http_thread = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    http_thread.start()
    print(json.dumps({"event": "server_started", "base_url": base_url,
                       "interval_s": interval_s, "port": port}), flush=True)

    scanner = threading.Thread(
        target=scan_loop,
        args=(state, base_url, interval_s, concurrency, delay, timeout, limit_dashboards,
              want_confirm, stop),
        daemon=True, name="scan-loop",
    )
    scanner.start()

    try:
        while scanner.is_alive():
            scanner.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
