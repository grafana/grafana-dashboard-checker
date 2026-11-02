"""Checks dashboards in a real browser (k6), the way an anonymous visitor sees
them: a panel showing Grafana's red error icon is broken. The query replay in
check_dashboards.py can't see render errors and produced false positives on
what it could see, so this is the whole check now.
Runs scan.js as a subprocess and parses its stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Iterable

SCAN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.js")


class BrowserUnavailable(Exception):
    """The k6 binary (with its browser module) isn't available here."""


def scan_dashboards(base_url: str, dashboards: Iterable[dict],
                    timeout_s: float | None = None) -> tuple[dict[str, dict], bool]:
    """dashboards: dicts with at least a uid (as /api/search returns them).

    Returns ({dashboard_uid: result}, timed_out) where result is either
    {"red": [{panel_title, panel_id, tab}], "settled", "panels_seen",
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

    env = dict(os.environ)
    env["DASHBOARDS_FILE"] = dashboards_path
    env["CHECKER_BASE_URL"] = base_url
    # k6 stops itself a minute before we'd kill it, so it exits with its output intact.
    env.setdefault("CHECKER_BROWSER_MAX_DURATION", f"{max(60, int(timeout_s) - 60)}s")

    timed_out = False
    try:
        proc = subprocess.run(
            # --log-format pins the shape _parse_k6_console_line expects.
            [k6, "run", "--log-format=logfmt", SCAN_SCRIPT],
            env=env, capture_output=True, text=True, timeout=timeout_s,
        )
        stdout, stderr = proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        raise BrowserUnavailable(f"k6 not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # Keep whatever dashboards finished before the kill.
        timed_out = True
        stdout, stderr = _text(exc.stdout), _text(exc.stderr)
    finally:
        try:
            os.unlink(dashboards_path)
        except OSError:
            pass

    if "k6/browser" in stderr and "command while running browser" in stderr:
        raise BrowserUnavailable(f"k6 browser module unavailable: {stderr[:500]}")

    results = parse_k6_output(stdout + "\n" + stderr)
    # k6 hitting maxDuration exits on its own, short of the kill; same outcome.
    timed_out = timed_out or len(results) < len(dashboards)
    return results, timed_out


def parse_k6_output(output: str) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for line in output.splitlines():
        row = _parse_k6_console_line(line)
        if row is None or "dashboard_uid" not in row:
            continue
        uid = row.pop("dashboard_uid")
        results[uid] = row
    return results


def _text(value: bytes | str | None) -> str:
    # TimeoutExpired carries bytes even when run() was asked for text.
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


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
