"""Confirms check_dashboards.scan()'s ERROR panels in a real browser (k6), since
the query replay has produced real false positives it can't avoid otherwise.
Runs confirm.js as a subprocess and parses its stdout; only the small set of
already-flagged candidates is checked, not the whole fleet each cycle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Iterable

CONFIRM_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confirm.js")


class BrowserUnavailable(Exception):
    """The k6 binary (with its browser module) isn't available here."""


def confirm_broken_panels(base_url: str, candidates: Iterable[dict],
                           timeout_s: float = 180.0) -> list[dict]:
    """candidates: dicts with dashboard_uid, panel_id, panel_title.

    Returns the subset still broken in a real render, each with a
    confirmed_text field (Grafana's own error/no-data message). Raises
    BrowserUnavailable if k6 can't run at all; a single dashboard that fails
    to load just stays unconfirmed, not an exception.
    """
    candidates = list(candidates)
    if not candidates:
        return []

    k6 = shutil.which("k6")
    if k6 is None:
        raise BrowserUnavailable("k6 binary not found on PATH")

    # A file, not an env var: hundreds of candidates once blew past the OS's
    # execve() arg/env size limit ("Argument list too long").
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="dashboard-checker-candidates-", delete=False
    ) as f:
        json.dump(candidates, f)
        candidates_path = f.name

    env = dict(os.environ)
    env["CANDIDATES_FILE"] = candidates_path
    env["CHECKER_BASE_URL"] = base_url

    try:
        proc = subprocess.run(
            # --log-format pins the shape _parse_k6_console_line expects.
            [k6, "run", "--log-format=logfmt", CONFIRM_SCRIPT],
            env=env, capture_output=True, text=True, timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise BrowserUnavailable(f"k6 not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BrowserUnavailable(f"k6 run timed out after {timeout_s}s: {exc}") from exc
    finally:
        try:
            os.unlink(candidates_path)
        except OSError:
            pass

    if "k6/browser" in proc.stderr and "command while running browser" in proc.stderr:
        raise BrowserUnavailable(f"k6 browser module unavailable: {proc.stderr[:500]}")

    confirmed: list[dict] = []
    for line in proc.stdout.splitlines() + proc.stderr.splitlines():
        row = _parse_k6_console_line(line)
        if row is not None and row.get("confirmed"):
            confirmed.append({
                "dashboard_uid": row["dashboard_uid"],
                "panel_id": row["panel_id"],
                "panel_title": row["panel_title"],
                "confirmed_text": row["confirmed_text"],
            })
    return confirmed


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
