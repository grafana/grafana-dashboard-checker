"""Confirms check_dashboards.scan()'s ERROR panels in a real browser (k6), since
the query replay has produced real false positives it can't avoid otherwise.
Runs confirm.js as a subprocess and parses its stdout; only dashboards with
already-flagged panels are loaded, not the whole fleet each cycle.
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


def confirm_broken_dashboards(base_url: str, candidates: Iterable[dict],
                              timeout_s: float | None = None) -> tuple[dict[str, dict], bool]:
    """candidates: dicts with dashboard_uid, panel_id, panel_title.

    Checks each candidate's dashboard once, as a whole: broken if any panel
    still shows Grafana's red error icon after the settle window. Returns
    ({dashboard_uid: {"broken": bool, "errors": [{panel_title}]}}, timed_out)
    for the dashboards it finished; ones it didn't reach, or couldn't load, are
    absent so the caller can fall back to the API verdict. Raises
    BrowserUnavailable only if k6 can't run at all.
    """
    candidates = list(candidates)
    if not candidates:
        return {}, False
    if timeout_s is None:
        timeout_s = float(os.environ.get("CHECKER_CONFIRM_TIMEOUT_SECONDS", "3600"))

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
    # k6 stops itself a minute before we'd kill it, so it exits with its output intact.
    env.setdefault("CHECKER_CONFIRM_MAX_DURATION", f"{max(60, int(timeout_s) - 60)}s")

    timed_out = False
    try:
        proc = subprocess.run(
            # --log-format pins the shape _parse_k6_console_line expects.
            [k6, "run", "--log-format=logfmt", CONFIRM_SCRIPT],
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
            os.unlink(candidates_path)
        except OSError:
            pass

    if "k6/browser" in stderr and "command while running browser" in stderr:
        raise BrowserUnavailable(f"k6 browser module unavailable: {stderr[:500]}")

    return parse_k6_output(stdout + "\n" + stderr), timed_out


def parse_k6_output(output: str) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for line in output.splitlines():
        row = _parse_k6_console_line(line)
        if row is None or "dashboard_uid" not in row or row.get("load_failed"):
            continue
        results[row["dashboard_uid"]] = {
            "broken": bool(row.get("broken")),
            "errors": row.get("errors") or [],
        }
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
