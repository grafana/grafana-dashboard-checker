"""The Grafana HTTP API calls the checker makes: listing dashboards and
building links to them. Standard library only.

Anonymous unless given a service account token; see Client.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "dashboard-checker/0.1"

# /api/search returns at most this many per page.
SEARCH_PAGE_SIZE = 1000

# The oldest Grafana the browser check works on: 11.3 made the Scenes-based
# dashboards the default, and scan.js's selectors are theirs. Before that it
# goes wrong quietly: 10.4 to 11.2 find red panels but not their ids, and
# 10.0 and older find the panels but none of their errors, so every
# dashboard looks clean.
MIN_GRAFANA_VERSION = (11, 3)


class UnsupportedGrafana(Exception):
    """The Grafana is older than MIN_GRAFANA_VERSION."""


class HttpResult:
    """status is the HTTP status, or -1 for a timeout and -2 for a connection
    error; body is the parsed JSON response, or None if it wasn't JSON."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body


class Client:
    """Grafana HTTP API client. Anonymous unless given a service account token."""

    def __init__(self, base_url: str, timeout: float, token: str | None = None,
                 org_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token or None
        self.org_id = org_id or None

    def get(self, path: str) -> HttpResult:
        req = urllib.request.Request(
            self.base_url + path, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        # Unredirected: urllib copies ordinary headers onto a redirect, even to
        # another host, which would hand the token to whoever it points at.
        if self.token:
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        if self.org_id:
            req.add_unredirected_header("X-Grafana-Org-Id", str(self.org_id))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return HttpResult(resp.status, _parse_json(resp.read()))
        except urllib.error.HTTPError as e:
            return HttpResult(e.code, _parse_json(e.read() if e.fp else b""))
        except (TimeoutError, OSError) as e:  # includes socket.timeout, URLError
            return HttpResult(-1 if "timed out" in str(e).lower() else -2, None)


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None


def parse_version(version: str) -> tuple[int, int] | None:
    """(major, minor) from a Grafana version such as "13.4.0-42669893023"."""
    m = re.match(r"(\d+)\.(\d+)", version or "")
    return (int(m[1]), int(m[2])) if m else None


def check_version(client: Client) -> str | None:
    """Grafana's version from /api/health, or None if it doesn't say.

    Raises UnsupportedGrafana if it's older than MIN_GRAFANA_VERSION. An
    instance that hides its version gets the benefit of the doubt; the scan
    then fails if it recognises no panels at all (see server.py).
    """
    res = client.get("/api/health")
    version = res.body.get("version") if isinstance(res.body, dict) else None
    if not isinstance(version, str):
        return None
    parsed = parse_version(version)
    if parsed is not None and parsed < MIN_GRAFANA_VERSION:
        minimum = ".".join(map(str, MIN_GRAFANA_VERSION))
        raise UnsupportedGrafana(
            f"Grafana {version} isn't supported: the browser check needs {minimum} or later "
            f"(the Scenes-based dashboards). On older versions it misses panel errors "
            f"or can't link to the panel.")
    return version


def list_dashboards(client: Client) -> list[dict]:
    """Every dashboard the caller can see, as /api/search returns them."""
    out: list[dict] = []
    page = 1
    while True:
        res = client.get(f"/api/search?type=dash-db&limit={SEARCH_PAGE_SIZE}&page={page}")
        if res.status != 200 or not isinstance(res.body, list):
            raise RuntimeError(f"dashboard search failed (HTTP {res.status})")
        out.extend(res.body)
        if len(res.body) < SEARCH_PAGE_SIZE:
            return out
        page += 1


def panel_link(base_url: str, item: dict) -> str:
    """Link to item's dashboard (item["dashboard_uid"]), opened on item["panel_id"] if set."""
    link = f"{base_url.rstrip('/')}/d/{item['dashboard_uid']}"
    if item.get("panel_id") is not None:
        link += "?" + urllib.parse.urlencode({"viewPanel": f"panel-{item['panel_id']}"})
    return link
