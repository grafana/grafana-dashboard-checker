"""Tests for loki.py: pushing (against a local HTTP server) and batching, and
for run_once's use of them."""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import loki
import server


class FakeLoki:
    """A local stand-in for Loki's push API. statuses: replies, in order; then 204."""

    def __init__(self, test: unittest.TestCase, statuses=()):
        self.requests = []
        replies = list(statuses)
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((self.path, dict(self.headers), json.loads(body)))
                self.send_response(replies.pop(0) if replies else 204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        test.addCleanup(srv.server_close)
        test.addCleanup(srv.shutdown)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"

    def lines(self):
        """Every line pushed so far, in order."""
        return [line for _, _, body in self.requests
                for stream in body["streams"] for _, line in stream["values"]]


class PushTest(unittest.TestCase):
    def test_pushes_one_stream_with_basic_auth(self):
        fake = FakeLoki(self)
        loki.push(fake.url, "123", "tok", {"job": "j"}, [(1, "a"), (2, "b")])
        path, headers, body = fake.requests[0]
        self.assertEqual(path, "/loki/api/v1/push")
        self.assertEqual(headers["Authorization"],
                         "Basic " + base64.b64encode(b"123:tok").decode())
        self.assertEqual(body, {"streams": [{"stream": {"job": "j"},
                                             "values": [["1", "a"], ["2", "b"]]}]})

    def test_which_failures_are_worth_retrying(self):
        for status, retryable in ((429, True), (500, True), (503, True),
                                  (400, False), (401, False)):
            fake = FakeLoki(self, [status])
            with self.assertRaises(loki.PushError) as ctx:
                loki.push(fake.url, "u", "t", {"job": "j"}, [(1, "a")])
            self.assertEqual(ctx.exception.retryable, retryable, status)

    def test_unreachable_loki_is_worth_retrying(self):
        srv = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = srv.server_address[1]
        srv.server_close()
        with self.assertRaises(loki.PushError) as ctx:
            loki.push(f"http://127.0.0.1:{port}", "u", "t", {"job": "j"}, [(1, "a")])
        self.assertTrue(ctx.exception.retryable)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class BatcherTest(unittest.TestCase):
    def batcher(self, pusher, **kw):
        self.clock, self.sleeps = FakeClock(), []
        return loki.Batcher("http://loki", "u", "t", {"job": "j"}, pusher=pusher,
                            clock=self.clock, sleep=self.sleeps.append, **kw)

    def test_pushes_each_time_a_batch_fills_up(self):
        batches = []
        b = self.batcher(lambda *args: batches.append([line for _, line in args[-1]]),
                         max_lines=3)
        for i in range(7):
            b.append((i, f"l{i}"))
        self.assertEqual(batches, [["l0", "l1", "l2"], ["l3", "l4", "l5"]])
        self.assertTrue(b.close())
        self.assertEqual(batches[-1], ["l6"])
        self.assertEqual(b.pushed, 7)

    def test_pushes_once_the_oldest_line_has_waited_long_enough(self):
        batches = []
        b = self.batcher(lambda *args: batches.append(len(args[-1])), max_age_s=30)
        b.append((1, "a"))
        self.clock.now = 10
        b.append((2, "b"))
        self.assertEqual(batches, [])
        self.clock.now = 31
        b.append((3, "c"))
        self.assertEqual(batches, [3])

    def test_retries_with_backoff_then_succeeds(self):
        calls = []

        def flaky(*args):
            calls.append(len(args[-1]))
            if len(calls) < 3:
                raise loki.PushError("HTTP 503", retryable=True)

        b = self.batcher(flaky, max_lines=1)
        b.append((1, "a"))
        self.assertEqual(calls, [1, 1, 1])
        self.assertEqual(self.sleeps, [2.0, 4.0])
        self.assertEqual((b.pushed, b.pending), (1, 0))

    def test_a_failed_batch_goes_out_with_the_next_one(self):
        state = {"up": False}
        pushed = []

        def pusher(*args):
            if not state["up"]:
                raise loki.PushError("HTTP 401", retryable=False)
            pushed.extend(line for _, line in args[-1])

        b = self.batcher(pusher, max_lines=2)
        with mock.patch("sys.stderr"):
            b.append((1, "a"))
            b.append((2, "b"))  # fills the batch; the push fails
        self.assertEqual(self.sleeps, [])  # 401 isn't retried
        self.assertEqual(b.pending, 2)
        state["up"] = True
        b.append((3, "c"))  # 3 >= 2: tries again, with the old lines first
        self.assertEqual(pushed, ["a", "b", "c"])
        self.assertTrue(b.close())

    def test_close_reports_lines_that_never_arrived(self):
        def down(*args):
            raise loki.PushError("unreachable", retryable=True)

        b = self.batcher(down)
        b.append((1, "a"))
        with mock.patch("sys.stderr"):
            self.assertFalse(b.close())
        self.assertEqual(b.pending, 1)
        self.assertIn("unreachable", b.last_error)


def loki_cfg(url):
    return {"url": url, "username": "u", "token": "t", "job": "j"}


class RunOnceTest(unittest.TestCase):
    def run_once(self, fake_url, events=3, shard=None):
        def fake_scan(state, cfg, log_sink=None):
            for i in range(events):
                server._emit(log_sink, {"event": "dashboard_checked", "n": i})
            state.last_scan_ok = True

        cfg = server.Config(base_url="https://play.example", shard=shard)
        with mock.patch.object(server, "run_scan_once", side_effect=fake_scan), \
                mock.patch("builtins.print"):
            return server.run_once(cfg, loki_cfg(fake_url))

    def test_pushes_in_batches_with_the_shard_label(self):
        fake = FakeLoki(self)
        with mock.patch.object(loki, "BATCH_MAX_LINES", 2):
            self.assertEqual(self.run_once(fake.url, events=5, shard=(2, 4)), 0)
        self.assertEqual(len(fake.requests), 3)  # 2 + 2 during the scan, 1 at the end
        self.assertEqual(fake.requests[0][2]["streams"][0]["stream"], {"job": "j", "shard": "2/4"})
        self.assertEqual([json.loads(line)["n"] for line in fake.lines()], [0, 1, 2, 3, 4])

    def test_lines_that_never_reach_loki_fail_the_run(self):
        fake = FakeLoki(self, [401] * 10)
        with mock.patch("sys.stderr"):
            self.assertEqual(self.run_once(fake.url), 1)


if __name__ == "__main__":
    unittest.main()
