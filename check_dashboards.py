#!/usr/bin/env python3
"""Dashboard health checker, query replay (prototype).

Lists the dashboards on a Grafana instance (anonymously, or as a service account
when CHECKER_GRAFANA_TOKEN is set), runs each panel's queries through
/api/ds/query, and reports failing panels.

Prototype scope: print a report locally.
Raw error messages are never printed unless --show-raw is passed (they can
contain hostnames or query text and must not go to a public channel).

Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

USER_AGENT = "dashboard-checker/0.1"

# Data sources the checker never queries (billed per query).
SKIP_TYPES = {"grafana-bigquery-datasource"}
# Data sources that share a rate limited API quota. Queried one at a time.
THROTTLED_TYPES = {
    "grafana-github-datasource",
    "yesoreyeram-infinity-datasource",
    "grafana-googlesheets-datasource",
}
# Panel types that do not run data source queries.
NO_QUERY_PANEL_TYPES = {
    "row", "text", "news", "dashlist", "alertlist", "welcome", "gettingstarted",
    "annolist", "pluginlist", "debug",
}

OK = "ok"
NO_DATA = "no_data"
ERROR = "error"
INCONCLUSIVE = "inconclusive"
SKIPPED = "skipped"

# Categories that mean "could not tell", not "broken".
INCONCLUSIVE_CATEGORIES = {"rate_limited", "rate_limited_skip", "unresolved_variable",
                           "unresolved_datasource", "timeout"}

# Grafana sizes maxDataPoints from panel width; this is a typical full-width value.
DEFAULT_MAX_POINTS = 1000


class HttpResult:
    def __init__(self, status: int, body: Any, raw: str = ""):
        self.status = status
        self.body = body
        self.raw = raw


class Client:
    """Grafana HTTP API client. Anonymous unless given a service account token."""

    def __init__(self, base_url: str, timeout: float, token: str | None = None,
                 org_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token or None
        self.org_id = org_id or None

    def request(self, method: str, path: str, payload: Any = None) -> HttpResult:
        url = self.base_url + path
        data = None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # Unredirected: urllib copies ordinary headers onto a redirect, even to
        # another host, which would hand the token to whoever it points at.
        if self.token:
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        if self.org_id:
            req.add_unredirected_header("X-Grafana-Org-Id", str(self.org_id))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return HttpResult(resp.status, _parse_json(raw), raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace") if e.fp else ""
            return HttpResult(e.code, _parse_json(raw), raw)
        except (TimeoutError, OSError) as e:  # includes socket.timeout, URLError
            if "timed out" in str(e).lower():
                return HttpResult(-1, None, "timeout")
            return HttpResult(-2, None, "connection_error")


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def list_dashboards(client: Client) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        res = client.request("GET", f"/api/search?type=dash-db&limit=1000&page={page}")
        if res.status != 200 or not isinstance(res.body, list):
            raise RuntimeError(f"dashboard search failed (HTTP {res.status})")
        out.extend(res.body)
        if len(res.body) < 1000:
            return out
        page += 1


class DatasourceLookup:
    """Data sources visible to the caller, from the public frontend settings."""

    # Built-in pseudo data sources: dashboard JSON uses these names, the API uses these uids.
    BUILTIN_UIDS = {"-- Grafana --": "grafana", "grafana": "grafana"}
    TYPE_ALIASES = {"testdata": "grafana-testdata-datasource",
                    "postgres": "grafana-postgresql-datasource"}

    def __init__(self, settings: dict | None):
        self.by_key: dict[str, dict] = {}
        self.ordered: list[dict] = []
        self.default: dict | None = None
        settings = settings or {}
        for name, ds in (settings.get("datasources") or {}).items():
            if not isinstance(ds, dict):
                continue
            meta = ds.get("meta") or {}
            entry = {
                "uid": ds.get("uid") or name,
                "type": ds.get("type", ""),
                "name": ds.get("name") or name,
                # Frontend-only plugins (Static, Business Input, ...) have no backend,
                # so /api/ds/query cannot run them even though the panel works.
                "backend": bool(meta.get("backend", True)),
                "is_default": bool(ds.get("isDefault")),
            }
            if entry["uid"] in self.BUILTIN_UIDS:
                entry["uid"] = self.BUILTIN_UIDS[entry["uid"]]
            self.ordered.append(entry)
            for key in {name, entry["name"], entry["uid"], ds.get("uid")}:
                if key:
                    self.by_key[key] = entry
            if entry["is_default"]:
                self.default = entry
        default_name = settings.get("defaultDatasource")
        if self.default is None and default_name in self.by_key:
            self.default = self.by_key[default_name]

    def get(self, key: str | None) -> dict | None:
        if not key:
            return None
        if key in ("default",):
            return self.default
        return self.by_key.get(key)

    def first_of_type(self, ds_type: str, regex: str | None = None) -> dict | None:
        ds_type = self.TYPE_ALIASES.get(ds_type, ds_type)
        pattern = None
        if regex:
            try:
                pattern = re.compile(regex.strip("/"))
            except re.error:
                pattern = None
        candidates = [d for d in self.ordered
                      if self.TYPE_ALIASES.get(d["type"], d["type"]) == ds_type
                      and (pattern is None or pattern.fullmatch(d["name"]) or pattern.fullmatch(d["uid"]))]
        # Grafana prefers the default data source when it matches.
        for d in candidates:
            if d["is_default"]:
                return d
        return candidates[0] if candidates else None


def load_datasource_lookup(client: Client) -> DatasourceLookup:
    res = client.request("GET", "/api/frontend/settings")
    return DatasourceLookup(res.body if res.status == 200 and isinstance(res.body, dict) else None)


# --------------------------------------------------------------------------
# Panel extraction
# --------------------------------------------------------------------------

def iter_panels(dash: dict):
    for p in dash.get("panels") or []:
        if p.get("type") == "row":
            yield from (p.get("panels") or [])
        else:
            yield p
    for row in dash.get("rows") or []:  # legacy schema
        yield from (row.get("panels") or [])


def _first_option(var: dict) -> str | None:
    for o in var.get("options") or []:
        v = o.get("value")
        if v not in (None, "", "$__all"):
            return v
    return None


def variable_values(dash: dict, lookup: DatasourceLookup) -> dict[str, str]:
    """Best-effort default value for each dashboard variable, mimicking Grafana."""
    values: dict[str, str] = {}
    for var in (dash.get("templating") or {}).get("list") or []:
        name = var.get("name")
        vtype = var.get("type")
        if not name:
            continue
        current = (var.get("current") or {}).get("value")
        if isinstance(current, list):
            current = current[0] if current else None

        if vtype == "datasource":
            ds = lookup.get(current) if current not in (None, "", "$__all") else None
            if ds is None:
                ds = lookup.first_of_type(str(var.get("query") or ""), var.get("regex"))
            if ds is not None:
                values[name] = ds["uid"]
            continue

        if current in (None, "", "$__all"):
            if vtype in ("constant", "textbox") and isinstance(var.get("query"), str):
                current = var["query"]
            elif var.get("allValue"):
                current = var["allValue"]
            elif current == "$__all" or var.get("includeAll"):
                # Without the option list, "match everything" is the closest
                # equivalent of All for regex matchers.
                current = _first_option(var) or ".*"
            elif vtype == "custom" and isinstance(var.get("query"), str):
                current = var["query"].split(",")[0].split(" : ")[-1].strip() or None
            elif vtype == "interval" and isinstance(var.get("query"), str):
                current = var["query"].split(",")[0].strip() or None
            else:
                current = _first_option(var)
        if current is not None:
            values[name] = str(current)
    return values


# Variable names must start with a letter or underscore, so PromQL/regex
# replacement references such as "$1" in label_replace are not treated as variables.
_VAR_RE = re.compile(
    r"\$\{([A-Za-z_]\w*)(?:[.:][^}]*)?\}|\$([A-Za-z_]\w*)|\[\[([A-Za-z_]\w*)(?::\w+)?\]\]")


def interpolate(obj: Any, values: dict[str, str]) -> tuple[Any, set[str]]:
    """Replace dashboard variables. Returns (result, unresolved variable names).

    Grafana macros (names starting with __) are left for the backend.
    """
    unresolved: set[str] = set()

    def sub(text: str) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1) or m.group(2) or m.group(3)
            if name.startswith("__"):
                return m.group(0)
            if name in values:
                return values[name]
            unresolved.add(name)
            return m.group(0)
        return _VAR_RE.sub(repl, text)

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return sub(o)
        if isinstance(o, list):
            return [walk(i) for i in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o

    return walk(obj), unresolved


MIXED = {"uid": "-- Mixed --", "type": "mixed", "name": "-- Mixed --", "backend": True}
DASHBOARD = {"uid": "-- Dashboard --", "type": "dashboard", "name": "-- Dashboard --", "backend": False}
EXPR = {"uid": "__expr__", "type": "__expr__", "name": "Expression", "backend": True}
UNRESOLVED = {"uid": None, "type": "", "name": "unresolved", "backend": True}


def resolve_ds(ref: Any, lookup: DatasourceLookup, values: dict[str, str]) -> dict | None:
    """Resolve a datasource reference the way Grafana does.

    Returns None when the reference is absent (caller falls back to the panel or
    instance default), or UNRESOLVED when it names something we cannot resolve.
    """
    if ref is None or ref == {}:
        return None
    if isinstance(ref, str):
        ref = {"uid": ref}
    uid = ref.get("uid")
    ds_type = ref.get("type") or ""
    if isinstance(uid, str) and "$" in uid:
        uid, _ = interpolate(uid, values)
        if "$" in uid:
            return UNRESOLVED
    if uid in ("-- Mixed --",) or ds_type == "mixed":
        return MIXED
    if uid in ("-- Dashboard --",) or ds_type == "dashboard":
        return DASHBOARD
    if uid == "__expr__" or ds_type == "__expr__":
        return EXPR
    if uid in DatasourceLookup.BUILTIN_UIDS:
        return lookup.get("grafana") or {"uid": "grafana", "type": "datasource",
                                         "name": "-- Grafana --", "backend": True}
    hit = lookup.get(uid)
    if hit:
        return hit
    if ds_type:
        # Grafana falls back to a data source of the same type when the uid is
        # missing or unknown (common in dashboards imported from other stacks).
        hit = lookup.first_of_type(ds_type)
        if hit:
            return hit
    if not uid:
        return None
    return {"uid": uid, "type": ds_type, "name": uid, "backend": True}


# --------------------------------------------------------------------------
# Time range and interval
# --------------------------------------------------------------------------

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}


def _duration_s(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*([smhdwMy])\s*", text or "")
    return int(m.group(1)) * _UNITS[m.group(2)] if m else None


def parse_time(text: Any, now_ms: int) -> int | None:
    """Parse 'now', 'now-6h', 'now-1d/d', epoch ms, or ISO time into epoch ms."""
    if isinstance(text, (int, float)):
        return int(text)
    text = str(text or "").strip()
    if text.isdigit():
        return int(text)
    m = re.fullmatch(r"now(?:([+-])(\d+)([smhdwMy]))?(?:/[smhdwMy])?", text)
    if m:
        if not m.group(1):
            return now_ms
        delta = int(m.group(2)) * _UNITS[m.group(3)] * 1000
        return now_ms - delta if m.group(1) == "-" else now_ms + delta
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def panel_time_range(dash: dict, panel: dict, now_ms: int) -> tuple[int, int]:
    tr = dash.get("time") or {}
    to_ms = parse_time(tr.get("to", "now"), now_ms) or now_ms
    from_ms = parse_time(tr.get("from", "now-6h"), now_ms) or (to_ms - 6 * 3600 * 1000)
    # Panel overrides: relative time ("Last 28d") and time shift.
    rel = _duration_s(str(panel.get("timeFrom") or "").replace("now-", ""))
    if rel:
        from_ms = to_ms - rel * 1000
    shift = _duration_s(str(panel.get("timeShift") or ""))
    if shift:
        from_ms -= shift * 1000
        to_ms -= shift * 1000
    return from_ms, to_ms


def panel_interval_ms(panel: dict, from_ms: int, to_ms: int, max_points: int) -> int:
    """Approximate Grafana's interval: range / maxDataPoints, floored by the panel min interval.

    Rounded up to a whole second: a sub-second value breaks Prometheus's
    $__rate_interval macro expansion ("41m19" instead of "41m19s" -> parse error).
    """
    interval = max(1000, (to_ms - from_ms) // max(1, max_points))
    min_s = _duration_s(str(panel.get("interval") or "").lstrip(">"))
    if min_s:
        interval = max(interval, min_s * 1000)
    interval = -(-interval // 1000) * 1000  # ceil to the nearest whole second
    return int(interval)


# --------------------------------------------------------------------------
# Task building
# --------------------------------------------------------------------------

def build_tasks(dash: dict, meta: dict, lookup: DatasourceLookup,
                now_ms: int | None = None) -> list[dict]:
    now_ms = now_ms or int(time.time() * 1000)
    values = variable_values(dash, lookup)
    tasks: list[dict] = []
    for panel in iter_panels(dash):
        if panel.get("type") in NO_QUERY_PANEL_TYPES:
            continue
        from_ms, to_ms = panel_time_range(dash, panel, now_ms)
        max_points = int(panel.get("maxDataPoints") or DEFAULT_MAX_POINTS)
        base = {
            "dashboard_uid": meta.get("uid"),
            "dashboard_title": meta.get("title"),
            "folder": meta.get("folderTitle", "General"),
            "panel_id": panel.get("id"),
            "panel_title": panel.get("title") or "(untitled)",
            "panel_type": panel.get("type"),
            "from": str(from_ms),
            "to": str(to_ms),
            "interval_ms": panel_interval_ms(panel, from_ms, to_ms, max_points),
            "max_points": max_points,
        }
        if panel.get("libraryPanel") and not panel.get("targets"):
            tasks.append({**base, "ds": None, "queries": [], "status": SKIPPED,
                          "category": "library_panel"})
            continue
        targets = [t for t in (panel.get("targets") or []) if not t.get("hide")]
        if not targets:
            continue

        panel_ds = resolve_ds(panel.get("datasource"), lookup, values) or lookup.default
        resolved = []  # (target, ds)
        for t in targets:
            ds = resolve_ds(t.get("datasource"), lookup, values)
            if ds is None:
                ds = panel_ds if panel_ds is not MIXED else lookup.default
            resolved.append((t, ds))

        real = [ds for _, ds in resolved if ds not in (EXPR, DASHBOARD, MIXED)]
        primary = real[0] if real else (resolved[0][1] if resolved else None)

        if any(ds is DASHBOARD for _, ds in resolved):
            continue  # reuses another panel's results
        if primary is None or any(ds is UNRESOLVED or ds is None for _, ds in resolved):
            tasks.append({**base, "ds": None, "queries": [], "status": INCONCLUSIVE,
                          "category": "unresolved_datasource"})
            continue

        task = {**base, "ds": primary, "ds_list": real or [primary], "queries": []}
        if any(ds["type"] in SKIP_TYPES for ds in real):
            task.update(status=SKIPPED, category="skipped_billed")
            tasks.append(task)
            continue
        if any(not ds.get("backend", True) for ds in real):
            task.update(status=SKIPPED, category="frontend_only_datasource")
            tasks.append(task)
            continue

        queries, unresolved = interpolate([t for t, _ in resolved], values)
        if unresolved:
            task.update(status=INCONCLUSIVE, category="unresolved_variable",
                        detail=", ".join(sorted(unresolved)))
            tasks.append(task)
            continue
        # All of a panel's queries go in one request, the way the panel runs them.
        # Expressions and mixed panels only work when sent together.
        for i, (q, (_, ds)) in enumerate(zip(queries, resolved)):
            q = copy.deepcopy(q)
            q.setdefault("refId", chr(ord("A") + i))
            q["datasource"] = {"uid": ds["uid"], "type": ds["type"]}
            q["intervalMs"] = base["interval_ms"]
            q["maxDataPoints"] = max_points
            task["queries"].append(q)
        tasks.append(task)
    return tasks


# --------------------------------------------------------------------------
# Execution and classification
# --------------------------------------------------------------------------

def categorize_error(message: str) -> str:
    m = (message or "").lower()
    if re.search(r"rate limit|quota|too many|429", m):
        return "rate_limited"
    if re.search(r"timeout|timed out|deadline", m):
        return "timeout"
    if re.search(r"not found|no such|does not exist|unknown data ?source|missing data ?source", m):
        return "datasource_not_found"
    if re.search(r"unauthori[sz]ed|forbidden|permission|denied|access", m):
        return "permission"
    if re.search(r"connection|refused|unreachable|dial|dns|no route|eof", m):
        return "backend_unreachable"
    return "query_error"


def classify_response(res: HttpResult, ref_ids: list[str]) -> tuple[str, str, str]:
    """Return (status, category, raw_detail)."""
    if res.status == -1:
        # A slow query is not a broken panel. Retried once, then reported as inconclusive.
        return INCONCLUSIVE, "timeout", "timeout"
    if res.status == -2:
        return ERROR, "checker_connection_error", "connection_error"
    if res.status == 429:
        return INCONCLUSIVE, "rate_limited", "HTTP 429"
    if res.status in (401, 403):
        return ERROR, "permission", f"HTTP {res.status}"
    if res.status == 404:
        return ERROR, "datasource_not_found", "HTTP 404"
    if res.status >= 500:
        return ERROR, "http_5xx", f"HTTP {res.status}"

    body = res.body if isinstance(res.body, dict) else {}
    results = body.get("results") or {}
    if res.status >= 400 and not results:
        msg = body.get("message", "") if isinstance(body, dict) else ""
        return ERROR, categorize_error(msg), msg or f"HTTP {res.status}"

    errors = []
    has_data = False
    for ref in ref_ids:
        r = results.get(ref)
        if r is None:
            continue
        if r.get("error"):
            errors.append(r["error"])
            continue
        for frame in r.get("frames") or []:
            for col in ((frame.get("data") or {}).get("values") or []):
                if col:
                    has_data = True
    if errors:
        cats = [categorize_error(e) for e in errors]
        category = cats[0]
        status = INCONCLUSIVE if category in INCONCLUSIVE_CATEGORIES else ERROR
        return status, category, errors[0]
    if not results:
        return NO_DATA, "no_results", ""
    return (OK, "ok", "") if has_data else (NO_DATA, "no_data", "")


def run_task(client: Client, task: dict, blocked: set[str], lock: threading.Lock) -> dict:
    if "status" in task:  # already decided while building
        return task
    uids = {d["uid"] for d in task.get("ds_list") or [task["ds"]]}
    with lock:
        is_blocked = bool(uids & blocked)
    if is_blocked:
        task.update(status=INCONCLUSIVE, category="rate_limited_skip", detail="")
        return task
    payload = {"queries": task["queries"], "from": task["from"], "to": task["to"]}
    ref_ids = [q["refId"] for q in task["queries"]]
    res = client.request("POST", "/api/ds/query", payload)
    status, category, detail = classify_response(res, ref_ids)
    if category == "timeout":
        res = client.request("POST", "/api/ds/query", payload)
        status, category, detail = classify_response(res, ref_ids)
    if category == "rate_limited":
        with lock:
            blocked.update(uids)
    task.update(status=status, category=category, detail=detail)
    return task


def execute(client: Client, tasks: list[dict], concurrency: int, delay: float) -> list[dict]:
    blocked: set[str] = set()
    lock = threading.Lock()
    results: list[dict] = []
    pending = [t for t in tasks if "status" not in t]
    done = [t for t in tasks if "status" in t]

    def is_throttled(t: dict) -> bool:
        return any(d["type"] in THROTTLED_TYPES for d in t.get("ds_list") or [t["ds"]])

    throttled = [t for t in pending if is_throttled(t)]
    normal = [t for t in pending if not is_throttled(t)]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(run_task, client, t, blocked, lock) for t in normal]
        # Throttled sources: one request at a time with a pause between requests.
        for t in throttled:
            results.append(run_task(client, t, blocked, lock))
            time.sleep(delay)
        results.extend(f.result() for f in futures)
    return done + results


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def panel_link(base_url: str, t: dict) -> str:
    link = f"{base_url.rstrip('/')}/d/{t['dashboard_uid']}"
    if t.get("panel_id") is not None:
        link += "?" + urllib.parse.urlencode({"viewPanel": f"panel-{t['panel_id']}"})
    return link


def report(base_url: str, dashboards: int, results: list[dict], show_raw: bool) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    failing = [r for r in results if r["status"] == ERROR]
    dash_failing = {r["dashboard_uid"] for r in failing}

    lines = [
        f"Checked {dashboards} dashboards, {len(results)} panel query groups on {base_url}",
        f"  ok: {counts[OK]}  error: {counts[ERROR]}  no_data: {counts[NO_DATA]}  "
        f"inconclusive: {counts[INCONCLUSIVE]}  skipped: {counts[SKIPPED]}",
        f"  {len(dash_failing)} dashboards with failing panels",
        "",
    ]

    if failing:
        lines.append("ERRORS (grouped by data source)")
        by_ds: dict[str, list[dict]] = defaultdict(list)
        for r in failing:
            by_ds[(r["ds"] or {}).get("name", "unknown")].append(r)
        for ds_name, items in sorted(by_ds.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- {ds_name}: {len(items)} panels")
            for r in sorted(items, key=lambda x: (x["dashboard_title"], x["panel_title"])):
                line = (f"    {r['dashboard_title']} / {r['panel_title']}"
                        f" [{r['category']}] {panel_link(base_url, r)}")
                if show_raw and r.get("detail"):
                    line += f"\n      raw: {r['detail']}"
                lines.append(line)
        lines.append("")

    inconclusive = [r for r in results if r["status"] == INCONCLUSIVE]
    if inconclusive:
        by_cat: dict[str, int] = defaultdict(int)
        for r in inconclusive:
            by_cat[r["category"]] += 1
        lines.append("INCONCLUSIVE (could not tell)")
        lines.extend(f"- {cat}: {n}" for cat, n in sorted(by_cat.items()))
        lines.append("")

    skipped = [r for r in results if r["status"] == SKIPPED]
    if skipped:
        by_cat = defaultdict(int)
        for r in skipped:
            by_cat[r["category"]] += 1
        lines.append("SKIPPED")
        lines.extend(f"- {cat}: {n}" for cat, n in sorted(by_cat.items()))
        lines.append("")

    no_data = [r for r in results if r["status"] == NO_DATA]
    if no_data:
        lines.append(f"NO DATA (warnings, {len(no_data)} panels; first 20)")
        for r in no_data[:20]:
            lines.append(f"- {r['dashboard_title']} / {r['panel_title']} {panel_link(base_url, r)}")
    return "\n".join(lines)


def to_json(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        out.append({
            "dashboard_uid": r["dashboard_uid"],
            "dashboard_title": r["dashboard_title"],
            "panel_id": r["panel_id"],
            "panel_title": r["panel_title"],
            "datasource": (r["ds"] or {}).get("name"),
            "datasource_type": (r["ds"] or {}).get("type"),
            "status": r["status"],
            "category": r["category"],
        })
    return out


# --------------------------------------------------------------------------
# Scan (discovery + task building + execution, reused by the CLI and the
# continuous server)
# --------------------------------------------------------------------------

def scan(base_url: str, concurrency: int, delay: float, timeout: float,
         limit_dashboards: int = 0, token: str | None = None,
         org_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """Run one full scan against base_url. Returns (dashboard_metas, panel_results)."""
    client = Client(base_url, timeout, token, org_id)
    lookup = load_datasource_lookup(client)
    dashboards = list_dashboards(client)
    if limit_dashboards:
        dashboards = dashboards[:limit_dashboards]

    tasks: list[dict] = []
    for meta in dashboards:
        res = client.request("GET", f"/api/dashboards/uid/{meta['uid']}")
        if res.status != 200 or not isinstance(res.body, dict):
            tasks.append({
                "dashboard_uid": meta["uid"], "dashboard_title": meta.get("title"),
                "folder": meta.get("folderTitle", "General"), "panel_id": None,
                "panel_title": "(whole dashboard)", "ds": None, "queries": [],
                "status": ERROR, "category": "dashboard_load_failed",
                "detail": f"HTTP {res.status}",
            })
            continue
        tasks.extend(build_tasks(res.body.get("dashboard") or {}, meta, lookup))

    results = execute(client, tasks, concurrency, delay)
    return dashboards, results


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-url", default=os.environ.get("CHECKER_BASE_URL"),
                    help="Grafana URL (default: $CHECKER_BASE_URL)")
    ap.add_argument("--org-id", default=os.environ.get("CHECKER_ORG_ID"),
                    help="Grafana org to check (default: $CHECKER_ORG_ID, else the token's own)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests to rate limited data sources")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--limit-dashboards", type=int, default=0,
                    help="check only the first N dashboards (for testing)")
    ap.add_argument("--json", dest="json_path", help="write machine readable results here")
    ap.add_argument("--show-raw", action="store_true",
                    help="include raw error text in the report (local use only)")
    args = ap.parse_args(argv)
    if not args.base_url:
        ap.error("--base-url or CHECKER_BASE_URL is required")

    # The token comes from the environment only: a command-line flag would
    # show up in the process list and shell history.
    dashboards, results = scan(args.base_url, args.concurrency, args.delay,
                                args.timeout, args.limit_dashboards,
                                token=os.environ.get("CHECKER_GRAFANA_TOKEN"),
                                org_id=args.org_id)
    print(report(args.base_url, len(dashboards), results, args.show_raw))
    if args.json_path:
        with open(args.json_path, "w") as f:
            json.dump(to_json(results), f, indent=2)
    return 1 if any(r["status"] == ERROR for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
