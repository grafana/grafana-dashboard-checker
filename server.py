#!/usr/bin/env python3
"""Scans every public dashboard in a browser on a timer, serving /metrics,
/healthz, /readyz.

Dashboards come from /api/search; browser.scan.scan_dashboards() (see
browser/scan.py) loads each one as an anonymous visitor and reports the panels
showing Grafana's red error icon. Per-panel detail goes to stdout as JSON
lines, not metric labels.

CHECKER_MODE=once runs a single scan and exits instead of serving forever
(see run_once()) -- used by the GitHub Actions cron run, which pushes those
JSON lines to Loki directly since there's no cluster log shipping there.
CHECKER_SHARD=i/n scans only that share of the dashboards (see parse_shard()),
so the workflow can split one scan across parallel jobs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import browser.scan as browser_scan
import check_dashboards as checker

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
        self.broken_dashboards: list[dict] = []  # [{uid, title, folder}]
        self.dashboard_count = 0
        self.dashboards_checked = 0
        self.panels_red = 0
        self.unsettled_count = 0
        self.load_failed_count = 0
        self.unchecked_count = 0


def parse_shard(spec: str) -> tuple[int, int] | None:
    """"2/4" -> (2, 4); empty means no sharding."""
    if not spec:
        return None
    index, _, count = spec.partition("/")
    i, n = int(index), int(count)
    if not 1 <= i <= n:
        raise ValueError(f"CHECKER_SHARD must be i/n with 1 <= i <= n, got {spec!r}")
    return i, n


def in_shard(uid: str, shard: tuple[int, int] | None) -> bool:
    # By uid hash rather than list position, so a dashboard added between two
    # jobs' searches can't shift every other dashboard into another shard.
    if shard is None:
        return True
    i, n = shard
    return zlib.crc32(uid.encode()) % n == i - 1


def _emit(log_sink: list[tuple[int, str]] | None, event: dict) -> None:
    line = json.dumps(event)
    print(line, flush=True)
    if log_sink is not None:
        log_sink.append((time.time_ns(), line))


def run_scan_once(state: State, base_url: str, timeout: float, limit_dashboards: int,
                   log_sink: list[tuple[int, str]] | None = None,
                   shard: tuple[int, int] | None = None) -> None:
    started = time.time()
    shard_label = f"{shard[0]}/{shard[1]}" if shard else None
    try:
        dashboards = [d for d in checker.list_dashboards(checker.Client(base_url, timeout))
                      if in_shard(d["uid"], shard)]
        if limit_dashboards:
            dashboards = dashboards[:limit_dashboards]
        results, timed_out = browser_scan.scan_dashboards(base_url, dashboards)
    except Exception as exc:  # search unreachable, or no browser to scan with
        duration = time.time() - started
        with state.lock:
            state.last_scan_ts = time.time()
            state.last_scan_duration_s = duration
            state.last_scan_ok = False
            state.last_scan_error = type(exc).__name__
            state.ready = True
        _emit(log_sink, {"event": "scan_failed", "shard": shard_label,
                          "error": type(exc).__name__, "detail": str(exc)[:500]})
        return

    if timed_out:
        _emit(log_sink, {"event": "scan_timed_out", "shard": shard_label,
                          "dashboards_checked": len(results),
                          "dashboards_total": len(dashboards)})

    broken: list[dict] = []
    panels_seen = panels_red = rows_expanded = tabs_visited = 0
    unsettled = load_failed = 0
    for meta in dashboards:
        r = results.get(meta["uid"])
        if r is None:
            continue  # not reached before the timeout; counted as unchecked
        d = {"uid": meta["uid"], "title": meta.get("title") or "",
             "folder": meta.get("folderTitle") or "General"}
        base = {"dashboard_uid": d["uid"], "dashboard_title": d["title"], "folder": d["folder"]}
        if r.get("load_failed"):
            load_failed += 1
            _emit(log_sink, {"event": "dashboard_load_failed", **base,
                              "link": checker.panel_link(base_url, base),
                              "error": r.get("error"), "duration_s": r.get("duration_s")})
            continue
        panels_seen += r.get("panels_seen", 0)
        rows_expanded += r.get("rows_expanded", 0)
        tabs_visited += len(r.get("tabs") or [])
        # Every dashboard, clean or not, so slow ones can be found.
        _emit(log_sink, {"event": "dashboard_checked", **base,
                          "panels_seen": r.get("panels_seen", 0),
                          "panels_red": len(r.get("red") or []),
                          "rows_expanded": r.get("rows_expanded", 0),
                          "tabs_visited": len(r.get("tabs") or []),
                          "settled": r.get("settled", True),
                          **{k: r.get(k) for k in ("duration_s", "nav_s", "settle_s")}})
        if not r.get("settled", True):
            # Something never finished loading inside the cap; red or not, say so.
            unsettled += 1
            _emit(log_sink, {"event": "dashboard_unsettled", **base,
                              "link": checker.panel_link(base_url, base)})
        if r.get("red"):
            broken.append(d)
        for panel in r.get("red") or []:
            panels_red += 1
            p = {**base, "panel_id": panel.get("panel_id")}
            _emit(log_sink, {"event": "panel_failed", **p,
                              "panel_title": panel.get("panel_title") or "(untitled)",
                              "tab": panel.get("tab"), "link": checker.panel_link(base_url, p)})

    duration = time.time() - started
    unchecked = len(dashboards) - len(results)
    _emit(log_sink, {
        "event": "scan_complete", "shard": shard_label, "dashboards_total": len(dashboards),
        "dashboards_checked": len(results) - load_failed, "dashboards_broken": len(broken),
        "dashboards_unsettled": unsettled, "dashboards_load_failed": load_failed,
        "dashboards_unchecked": unchecked, "panels_seen": panels_seen, "panels_red": panels_red,
        "rows_expanded": rows_expanded, "tabs_visited": tabs_visited,
        "duration_s": round(duration, 1),
    })

    with state.lock:
        state.last_scan_ts = time.time()
        state.last_scan_duration_s = duration
        state.last_scan_ok = True
        state.last_scan_error = None
        state.broken_dashboards = broken
        state.dashboard_count = len(dashboards)
        state.dashboards_checked = len(results) - load_failed
        state.panels_red = panels_red
        state.unsettled_count = unsettled
        state.load_failed_count = load_failed
        state.unchecked_count = unchecked
        state.ready = True


def run_once(base_url: str, timeout: float, limit_dashboards: int,
             shard: tuple[int, int] | None = None) -> int:
    """Single scan with no HTTP server, for the GitHub Actions cron run.

    Pushes the buffered log lines to Loki when LOKI_URL is set (unset is for
    local testing without credentials); a push failure fails the run.
    """
    state = State()
    log_sink: list[tuple[int, str]] = []
    run_scan_once(state, base_url, timeout, limit_dashboards, log_sink=log_sink, shard=shard)

    loki_url = os.environ.get("LOKI_URL")
    if loki_url:
        import loki
        labels = {"job": os.environ.get("LOKI_JOB_LABEL", "play-dashboard-checker")}
        if shard:
            # One stream per shard: the jobs push concurrently.
            labels["shard"] = f"{shard[0]}/{shard[1]}"
        loki.push(loki_url, os.environ["LOKI_USERNAME"], os.environ["LOKI_TOKEN"], labels, log_sink)

    return 0 if state.last_scan_ok else 1


def scan_loop(state: State, base_url: str, interval_s: int, timeout: float,
              limit_dashboards: int, stop: threading.Event,
              shard: tuple[int, int] | None = None) -> None:
    while not stop.is_set():
        run_scan_once(state, base_url, timeout, limit_dashboards, shard=shard)
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
        broken = list(state.broken_dashboards)
        dashboard_count = state.dashboard_count
        dashboards_checked = state.dashboards_checked
        panels_red = state.panels_red
        unsettled_count = state.unsettled_count
        load_failed_count = state.load_failed_count
        unchecked_count = state.unchecked_count

    gauge("up", "1 if the process is serving requests.")
    lines.append(f"{METRIC_PREFIX}_up 1")

    gauge("last_scan_timestamp_seconds", "Unix time the last scan finished.")
    lines.append(f"{METRIC_PREFIX}_last_scan_timestamp_seconds {last_ts or 0}")

    gauge("last_scan_duration_seconds", "Wall clock time of the last scan.")
    lines.append(f"{METRIC_PREFIX}_last_scan_duration_seconds {duration}")

    gauge("last_scan_success", "1 if the last scan completed, 0 if discovery or the browser failed.")
    lines.append(f"{METRIC_PREFIX}_last_scan_success {1 if ok else 0}")

    gauge("dashboards_scanned", "Public dashboards found by the last scan.")
    lines.append(f"{METRIC_PREFIX}_dashboards_scanned {dashboard_count}")

    gauge("dashboards_checked", "Dashboards the browser loaded and checked in the last scan.")
    lines.append(f"{METRIC_PREFIX}_dashboards_checked {dashboards_checked}")

    gauge("panels_red", "Panels showing Grafana's red error icon in the last scan.")
    lines.append(f"{METRIC_PREFIX}_panels_red {panels_red}")

    lines.append(f"# HELP {METRIC_PREFIX}_dashboard_broken 1 if the dashboard has a panel showing"
                 " Grafana's red error icon.")
    lines.append(f"# TYPE {METRIC_PREFIX}_dashboard_broken gauge")
    for d in broken:
        lines.append(
            f'{METRIC_PREFIX}_dashboard_broken{{dashboard_uid="{_label(d["uid"])}",'
            f'dashboard_title="{_label(d["title"])}",folder="{_label(d["folder"])}"}} 1'
        )

    gauge("dashboards_unsettled", "Dashboards with a panel still loading when the settle cap ran out.")
    lines.append(f"{METRIC_PREFIX}_dashboards_unsettled {unsettled_count}")

    gauge("dashboards_load_failed", "Dashboards the browser couldn't load.")
    lines.append(f"{METRIC_PREFIX}_dashboards_load_failed {load_failed_count}")

    gauge("dashboards_unchecked", "Dashboards the browser didn't reach before the scan timed out.")
    lines.append(f"{METRIC_PREFIX}_dashboards_unchecked {unchecked_count}")

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
    timeout = float(os.environ.get("CHECKER_TIMEOUT_SECONDS", "60"))
    port = int(os.environ.get("CHECKER_PORT", "8080"))
    limit_dashboards = int(os.environ.get("CHECKER_LIMIT_DASHBOARDS", "0"))
    shard = parse_shard(os.environ.get("CHECKER_SHARD", ""))

    if os.environ.get("CHECKER_MODE", "serve") == "once":
        return run_once(base_url, timeout, limit_dashboards, shard)

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
        args=(state, base_url, interval_s, timeout, limit_dashboards, stop, shard),
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
