"""Pushes JSON log lines to a Grafana Cloud Loki push API, in batches as a
scan runs.

Used only by CHECKER_MODE=once; the continuous server still just prints to
stdout for the cluster's own log shipping.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

# A batch goes out once it has this many lines, or its oldest line is this old.
BATCH_MAX_LINES = 500
BATCH_MAX_AGE_S = 30.0
# Tries per push for errors that may pass (rate limits, 5xx, network), with
# the wait doubling between them: 2 s, 4 s.
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S = 2.0


class PushError(RuntimeError):
    """A push Loki rejected or that didn't arrive. retryable: worth trying again."""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def push(url: str, username: str, token: str, labels: dict[str, str],
         lines: list[tuple[int, str]]) -> None:
    """Pushes lines as one stream, once. lines: [(unix_nanos, json_line), ...]."""
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
                raise PushError(f"loki push failed: HTTP {resp.status}", retryable=True)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        # 429 and 5xx can pass; other 4xx (bad auth, a rejected line) won't.
        raise PushError(f"loki push failed: HTTP {e.code} {body}",
                        retryable=e.code == 429 or e.code >= 500) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PushError(f"loki push failed: {e}", retryable=True) from e


class Batcher:
    """Collects (unix_nanos, line) pairs and pushes them to Loki in batches.

    Lines go out once BATCH_MAX_LINES have built up or the oldest has waited
    BATCH_MAX_AGE_S, so results reach Loki while the scan is still running.
    A batch that still fails after PUSH_ATTEMPTS stays buffered and goes out
    with the next one; close() pushes whatever is left and says whether
    everything arrived.

    Has append(), so it can stand in for the list run_scan_once writes to.
    """

    def __init__(self, url: str, username: str, token: str, labels: dict[str, str], *,
                 max_lines: int | None = None, max_age_s: float | None = None,
                 attempts: int = PUSH_ATTEMPTS, backoff_s: float = PUSH_BACKOFF_S,
                 pusher: Callable[..., None] = push,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._target = (url, username, token, labels)
        self._max_lines = max_lines or BATCH_MAX_LINES
        self._max_age_s = max_age_s or BATCH_MAX_AGE_S
        self._attempts, self._backoff_s = attempts, backoff_s
        self._push, self._clock, self._sleep = pusher, clock, sleep
        self._buf: list[tuple[int, str]] = []
        self._oldest: float | None = None
        self.pushed = 0
        self.last_error: str | None = None

    def append(self, item: tuple[int, str]) -> None:
        if not self._buf:
            self._oldest = self._clock()
        self._buf.append(item)
        if (len(self._buf) >= self._max_lines
                or self._clock() - self._oldest >= self._max_age_s):
            self.flush()

    def flush(self) -> bool:
        """Pushes everything buffered. False if it's still buffered afterwards."""
        if not self._buf:
            return True
        batch = self._buf
        for attempt in range(1, self._attempts + 1):
            try:
                self._push(*self._target, batch)
            except PushError as e:
                self.last_error = str(e)
                if not e.retryable or attempt == self._attempts:
                    break
                self._sleep(self._backoff_s * 2 ** (attempt - 1))
            else:
                self.pushed += len(batch)
                self._buf, self._oldest = [], None
                return True
        # Keep the lines for the next try, and wait a full interval before it.
        self._oldest = self._clock()
        print(json.dumps({"event": "loki_push_failed", "lines_pending": len(self._buf),
                          "error": self.last_error}), file=sys.stderr, flush=True)
        return False

    def close(self) -> bool:
        """Pushes what's left. True if every line reached Loki."""
        return self.flush()

    @property
    def pending(self) -> int:
        return len(self._buf)
