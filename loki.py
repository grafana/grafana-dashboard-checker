#!/usr/bin/env python3
"""Pushes buffered JSON log lines to a Grafana Cloud Loki push API.

Used only by the GitHub Actions cron run (CHECKER_MODE=once); the continuous
server still just prints to stdout for the cluster's own log shipping.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


def push(url: str, username: str, token: str, labels: dict[str, str],
         lines: list[tuple[int, str]]) -> None:
    """Pushes lines as one stream. lines: [(unix_nanos, json_line), ...]."""
    if not lines:
        return
    payload = {
        "streams": [{
            "stream": labels,
            "values": [[str(ts_ns), line] for ts_ns, line in lines],
        }],
    }
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    req = urllib.request.Request(
        url.rstrip("/") + "/loki/api/v1/push",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    # Unredirected, so a redirect can't carry the credentials to another host.
    req.add_unredirected_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"loki push failed: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"loki push failed: HTTP {e.code} {body}") from e
