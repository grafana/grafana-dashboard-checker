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

All settings come from the environment and are read and checked once, up
front, by Config.from_env(); see docs/reference.md for the full list.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import grafana_api, loki
from .browser import scan as browser_scan

METRIC_PREFIX = "dashboard_checker"

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


_SCAN_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def parse_scan_id(value: str) -> str | None:
    """CHECKER_SCAN_ID: letters, digits and . _ : - only, so it's safe in a LogQL filter."""
    value = value.strip()
    if not value:
        return None
    if not _SCAN_ID_RE.fullmatch(value):
        raise ValueError("CHECKER_SCAN_ID may only use letters, digits and . _ : - "
                         f"(at most 128), got {value!r}")
    return value


def new_scan_id() -> str:
    """Sorts by start time, e.g. 20261106T060000Z-3fa9c1."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(v.strip() for v in (value or "").split(",") if v.strip())


def _bool(name: str, value: str | None) -> bool:
    v = (value or "").strip().lower()
    if v in ("", "0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"{name} must be true or false, got {value!r}")


@dataclass(frozen=True)
class Filters:
    """Which dashboards to scan, by folder (title or uid), tag or uid.

    Empty include sets mean everything; excludes win over includes. Dashboards
    at the root have the folder "General". Folders match the dashboard's
    immediate folder only, not its parents.
    """
    folders: frozenset[str] = frozenset()
    exclude_folders: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    exclude_tags: frozenset[str] = frozenset()
    exclude_uids: frozenset[str] = frozenset()

    def allows(self, meta: dict) -> bool:
        folder = {meta.get("folderTitle") or "General", meta.get("folderUid") or ""} - {""}
        tags = set(meta.get("tags") or [])
        if self.folders and not folder & self.folders:
            return False
        if self.tags and not tags & self.tags:
            return False
        return not (folder & self.exclude_folders or tags & self.exclude_tags
                    or meta.get("uid") in self.exclude_uids)


@dataclass(frozen=True)
class Config:
    base_url: str
    # A service account token (Viewer is enough); None browses anonymously.
    token: str | None = field(default=None, repr=False)
    org_id: str | None = None
    timeout: float = 60.0
    limit_dashboards: int = 0
    shard: tuple[int, int] | None = None
    filters: Filters = Filters()
    # Panel error messages can carry hostnames, query text or other internals,
    # so they're only recorded when asked for.
    include_error_text: bool = False
    # Shared by every event of one scan; None generates one per scan. Set it
    # to the same value in each shard's job so their events group together.
    scan_id: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        base_url = (env.get("CHECKER_BASE_URL") or "").strip().rstrip("/")
        if not re.match(r"^https?://[^/?#]+", base_url):
            raise ValueError("CHECKER_BASE_URL must be set to your Grafana's URL, "
                             f"e.g. https://example.grafana.net (got {base_url!r})")
        return cls(
            base_url=base_url,
            token=env.get("CHECKER_GRAFANA_TOKEN") or None,
            org_id=env.get("CHECKER_ORG_ID") or None,
            timeout=float(env.get("CHECKER_TIMEOUT_SECONDS", "60")),
            limit_dashboards=int(env.get("CHECKER_LIMIT_DASHBOARDS", "0")),
            shard=parse_shard(env.get("CHECKER_SHARD", "")),
            filters=Filters(
                folders=_csv(env.get("CHECKER_FOLDERS")),
                exclude_folders=_csv(env.get("CHECKER_EXCLUDE_FOLDERS")),
                tags=_csv(env.get("CHECKER_TAGS")),
                exclude_tags=_csv(env.get("CHECKER_EXCLUDE_TAGS")),
                exclude_uids=_csv(env.get("CHECKER_EXCLUDE_UIDS")),
            ),
            include_error_text=_bool("CHECKER_INCLUDE_ERROR_TEXT",
                                     env.get("CHECKER_INCLUDE_ERROR_TEXT")),
            scan_id=parse_scan_id(env.get("CHECKER_SCAN_ID", "")),
        )


def in_shard(uid: str, shard: tuple[int, int] | None) -> bool:
    # By uid hash rather than list position, so a dashboard added between two
    # jobs' searches can't shift every other dashboard into another shard.
    if shard is None:
        return True
    i, n = shard
    return zlib.crc32(uid.encode()) % n == i - 1


def _emit(log_sink, event: dict) -> None:
    """Prints event as a JSON line, and hands it to log_sink (anything with append)."""
    line = json.dumps(event)
    print(line, flush=True)
    if log_sink is not None:
        log_sink.append((time.time_ns(), line))


@dataclass
class _Tally:
    """Running totals for scan_complete, built up as dashboards finish."""
    broken: list[dict] = field(default_factory=list)
    reported: int = 0
    panels_seen: int = 0
    panels_red: int = 0
    rows_expanded: int = 0
    tabs_visited: int = 0
    unsettled: int = 0
    load_failed: int = 0


def _report_dashboard(emit, base_url: str, include_error_text: bool, meta: dict, r: dict,
                      tally: _Tally) -> None:
    """Emits one finished dashboard's events and adds it to the totals."""
    tally.reported += 1
    d = {"uid": meta["uid"], "title": meta.get("title") or "",
         "folder": meta.get("folderTitle") or "General"}
    base = {"dashboard_uid": d["uid"], "dashboard_title": d["title"], "folder": d["folder"]}
    if r.get("load_failed"):
        tally.load_failed += 1
        emit({"event": "dashboard_load_failed", **base,
              "link": grafana_api.panel_link(base_url, base),
              "error": r.get("error"), "duration_s": r.get("duration_s")})
        return
    tally.panels_seen += r.get("panels_seen", 0)
    tally.rows_expanded += r.get("rows_expanded", 0)
    tally.tabs_visited += len(r.get("tabs") or [])
    # Every dashboard, clean or not, so slow ones can be found.
    emit({"event": "dashboard_checked", **base,
          "panels_seen": r.get("panels_seen", 0),
          "panels_red": len(r.get("red") or []),
          "rows_expanded": r.get("rows_expanded", 0),
          "tabs_visited": len(r.get("tabs") or []),
          "settled": r.get("settled", True),
          **{k: r.get(k) for k in ("duration_s", "nav_s", "settle_s")}})
    if not r.get("settled", True):
        # Something never finished loading inside the cap; red or not, say so.
        tally.unsettled += 1
        emit({"event": "dashboard_unsettled", **base,
              "link": grafana_api.panel_link(base_url, base)})
    if r.get("red"):
        tally.broken.append(d)
    for panel in r.get("red") or []:
        tally.panels_red += 1
        p = {**base, "panel_id": panel.get("panel_id")}
        emit({"event": "panel_failed", **p,
              "panel_title": panel.get("panel_title") or "(untitled)",
              "tab": panel.get("tab"),
              "error": panel.get("error") if include_error_text else None,
              "link": grafana_api.panel_link(base_url, p)})


def run_scan_once(state: State, cfg: Config, log_sink=None) -> None:
    """One scan: list dashboards, check them in the browser, report.

    Each dashboard's events go out as soon as the browser finishes it, so a
    log_sink that ships lines as they come (see loki.Batcher) doesn't wait for
    the whole scan. Every event carries the scan's scan_id.
    """
    started = time.time()
    base_url, shard = cfg.base_url, cfg.shard
    shard_label = f"{shard[0]}/{shard[1]}" if shard else None
    scan_id = cfg.scan_id or new_scan_id()

    def emit(event: dict) -> None:
        _emit(log_sink, {"event": event.pop("event"), "scan_id": scan_id, **event})

    tally = _Tally()
    try:
        client = grafana_api.Client(base_url, cfg.timeout, cfg.token, cfg.org_id)
        dashboards = [d for d in grafana_api.list_dashboards(client)
                      if in_shard(d["uid"], shard) and cfg.filters.allows(d)]
        if cfg.limit_dashboards:
            dashboards = dashboards[:cfg.limit_dashboards]
        metas = {d["uid"]: d for d in dashboards}

        def on_result(uid: str, r: dict) -> None:
            if uid in metas:
                _report_dashboard(emit, base_url, cfg.include_error_text, metas[uid], r, tally)

        _, timed_out = browser_scan.scan_dashboards(
            base_url, dashboards, token=cfg.token, org_id=cfg.org_id,
            include_error_text=cfg.include_error_text, on_result=on_result)
    except Exception as exc:  # search unreachable, no browser, or k6 crashed
        duration = time.time() - started
        with state.lock:
            state.last_scan_ts = time.time()
            state.last_scan_duration_s = duration
            state.last_scan_ok = False
            state.ready = True
        event = {"event": "scan_failed", "shard": shard_label,
                 "error": type(exc).__name__, "detail": str(exc)[:500]}
        if isinstance(exc, browser_scan.ScanCrashed):
            # The dashboards it finished have already gone out; say how far it got.
            event.update(exit_code=exc.exit_code, dashboards_checked=tally.reported,
                         dashboards_total=len(dashboards))
        emit(event)
        return

    reported = tally.reported
    if timed_out:
        emit({"event": "scan_timed_out", "shard": shard_label,
              "dashboards_checked": reported, "dashboards_total": len(dashboards)})

    duration = time.time() - started
    unchecked = len(dashboards) - reported
    emit({
        "event": "scan_complete", "shard": shard_label, "dashboards_total": len(dashboards),
        "dashboards_checked": reported - tally.load_failed,
        "dashboards_broken": len(tally.broken), "dashboards_unsettled": tally.unsettled,
        "dashboards_load_failed": tally.load_failed, "dashboards_unchecked": unchecked,
        "panels_seen": tally.panels_seen, "panels_red": tally.panels_red,
        "rows_expanded": tally.rows_expanded, "tabs_visited": tally.tabs_visited,
        "duration_s": round(duration, 1),
    })

    with state.lock:
        state.last_scan_ts = time.time()
        state.last_scan_duration_s = duration
        state.last_scan_ok = True
        state.broken_dashboards = tally.broken
        state.dashboard_count = len(dashboards)
        state.dashboards_checked = reported - tally.load_failed
        state.panels_red = tally.panels_red
        state.unsettled_count = tally.unsettled
        state.load_failed_count = tally.load_failed
        state.unchecked_count = unchecked
        state.ready = True


def loki_settings(env: dict[str, str] | None = None) -> dict[str, str] | None:
    """LOKI_* settings, or None when LOKI_URL is unset (local runs without credentials).

    Checked before the scan, so a missing credential fails in seconds rather
    than after an hour of scanning.
    """
    env = os.environ if env is None else env
    if not env.get("LOKI_URL"):
        return None
    missing = [k for k in ("LOKI_USERNAME", "LOKI_TOKEN") if not env.get(k)]
    if missing:
        raise ValueError(f"LOKI_URL is set but {', '.join(missing)} is not")
    return {"url": env["LOKI_URL"], "username": env["LOKI_USERNAME"], "token": env["LOKI_TOKEN"],
            "job": env.get("LOKI_JOB_LABEL") or "dashboard-checker"}


def run_once(cfg: Config, loki_cfg: dict[str, str] | None) -> int:
    """Single scan with no HTTP server, for the GitHub Actions cron run.

    With Loki configured, the log lines go out in batches while the scan runs
    (see loki.Batcher). Exits 1 if the scan failed or any line never reached Loki.
    """
    state = State()
    batcher = None
    if loki_cfg:
        labels = {"job": loki_cfg["job"]}
        if cfg.shard:
            # One stream per shard: the jobs push concurrently.
            labels["shard"] = f"{cfg.shard[0]}/{cfg.shard[1]}"
        batcher = loki.Batcher(loki_cfg["url"], loki_cfg["username"], loki_cfg["token"], labels)

    run_scan_once(state, cfg, log_sink=batcher)

    if batcher is not None and not batcher.close():
        print(f"loki push failed: {batcher.pending} lines never arrived "
              f"({batcher.last_error})", file=sys.stderr, flush=True)
        return 1
    return 0 if state.last_scan_ok else 1


def scan_loop(state: State, cfg: Config, interval_s: int, stop: threading.Event) -> None:
    while not stop.is_set():
        run_scan_once(state, cfg)
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

    gauge("last_scan_success",
          "1 if the last scan completed, 0 if discovery or the browser failed.")
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

    gauge("dashboards_unsettled",
          "Dashboards with a panel still loading when the settle cap ran out.")
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

        def do_GET(self) -> None:  # name and signature set by http.server
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

        def log_message(self, fmt: str, *args) -> None:
            pass  # access logs add noise; scan_complete/panel_failed lines carry signal.

    return Handler


def main() -> int:
    try:
        cfg = Config.from_env()
        mode = os.environ.get("CHECKER_MODE", "serve")
        if mode not in ("serve", "once"):
            raise ValueError(f"CHECKER_MODE must be serve or once, got {mode!r}")
        if mode == "serve" and cfg.scan_id:
            # Every scan in the loop would share it; each gets its own instead.
            raise ValueError("CHECKER_SCAN_ID only applies to CHECKER_MODE=once")
        loki_cfg = loki_settings() if mode == "once" else None
        interval_s = int(os.environ.get("CHECKER_SCAN_INTERVAL_SECONDS", "900"))
        port = int(os.environ.get("CHECKER_PORT", "8080"))
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    base_url = cfg.base_url

    if mode == "once":
        return run_once(cfg, loki_cfg)

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
        args=(state, cfg, interval_s, stop),
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

