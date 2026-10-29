"""Tests for the browser confirmation pass: confirm.py's k6 handling and how
server.py turns its per-dashboard verdicts into events. k6 itself is mocked."""

import json
import subprocess
import unittest
from unittest import mock

import check_dashboards as cd
import confirmation.confirm as confirm
import server


def k6_console_line(payload: dict) -> str:
    """What k6 --log-format=logfmt prints for console.log(JSON.stringify(payload))."""
    raw = json.dumps(payload).replace("\\", "\\\\").replace('"', '\\"')
    return f'time="2026-10-29T17:20:00Z" level=info msg="{raw}" source=console'


CANDIDATES = [{"dashboard_uid": "a", "panel_id": 1, "panel_title": "Latency"}]


class ParseK6OutputTest(unittest.TestCase):
    def test_keeps_checked_dashboards_and_drops_load_failures(self):
        output = "\n".join([
            'time="2026-10-29T17:19:00Z" level=info msg="starting" source=browser',
            k6_console_line({"dashboard_uid": "a", "broken": True,
                             "errors": [{"panel_title": "Lat\"ency"}]}),
            k6_console_line({"dashboard_uid": "b", "broken": False, "errors": []}),
            k6_console_line({"dashboard_uid": "c", "load_failed": True, "error": "timeout"}),
        ])
        self.assertEqual(confirm.parse_k6_output(output), {
            "a": {"broken": True, "errors": [{"panel_title": 'Lat"ency'}]},
            "b": {"broken": False, "errors": []},
        })


@mock.patch.object(confirm.shutil, "which", return_value="/usr/bin/k6")
class ConfirmBrokenDashboardsTest(unittest.TestCase):
    def test_timeout_keeps_dashboards_finished_before_the_kill(self, _which):
        partial = k6_console_line({"dashboard_uid": "a", "broken": True, "errors": []})
        # TimeoutExpired carries bytes even though run() was asked for text.
        exc = subprocess.TimeoutExpired(cmd="k6", timeout=60, output=partial.encode(),
                                        stderr=b"")
        with mock.patch.object(confirm.subprocess, "run", side_effect=exc):
            verdicts, timed_out = confirm.confirm_broken_dashboards(
                "https://play.example", CANDIDATES, timeout_s=60)
        self.assertTrue(timed_out)
        self.assertEqual(verdicts, {"a": {"broken": True, "errors": []}})

    def test_k6_stops_itself_before_the_kill_timeout(self, _which):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(confirm.subprocess, "run", return_value=done) as run:
            confirm.confirm_broken_dashboards("https://play.example", CANDIDATES, timeout_s=600)
        self.assertEqual(run.call_args.kwargs["env"]["CHECKER_CONFIRM_MAX_DURATION"], "540s")
        self.assertEqual(run.call_args.kwargs["timeout"], 600)


def api_error(uid, panel_id, title):
    return {"dashboard_uid": uid, "dashboard_title": uid.upper(), "folder": "F",
            "panel_id": panel_id, "panel_title": title, "status": cd.ERROR,
            "category": "query_error"}


class RunScanOnceTest(unittest.TestCase):
    def run_scan(self, verdicts, timed_out=False):
        results = [
            api_error("broken", 1, "Latency"),
            api_error("broken", 2, "Throughput"),
            api_error("fine", 3, "Errors"),
            api_error("unchecked", 4, "Uptime"),
            api_error("broken", 5, "(untitled)"),
        ]
        state, sink = server.State(), []
        with mock.patch.object(server.checker, "scan", return_value=([{}, {}, {}], results)), \
                mock.patch.object(server.confirm, "confirm_broken_dashboards",
                                  return_value=(verdicts, timed_out)), \
                mock.patch("builtins.print"):
            server.run_scan_once(state, "https://play.example", 1, 0, 1, 0, True, log_sink=sink)
        return state, [json.loads(line) for _, line in sink]

    def test_dashboard_verdicts_decide_broken_false_positive_and_unconfirmed(self):
        state, events = self.run_scan({
            "broken": {"broken": True, "errors": [{"panel_title": "Latency"}, {"panel_title": ""}]},
            "fine": {"broken": False, "errors": []},
        }, timed_out=True)

        failed = {e["panel_id"]: e for e in events if e["event"] == "panel_failed"}
        self.assertEqual(sorted(failed), [1, 2, 4, 5])
        self.assertEqual((failed[1]["confirmed"], failed[1]["panel_red"]), (True, True))
        # Same broken dashboard, but this panel wasn't one of the red ones.
        self.assertEqual((failed[2]["confirmed"], failed[2]["panel_red"]), (True, False))
        # The browser never got to it: the API verdict stands, marked unconfirmed.
        self.assertEqual((failed[4]["confirmed"], failed[4]["panel_red"]), (False, None))
        # "(untitled)" in the API results is an empty title on the page.
        self.assertEqual(failed[5]["panel_red"], True)

        fps = [e for e in events if e["event"] == "api_false_positive"]
        self.assertEqual([e["panel_id"] for e in fps], [3])

        timeout = next(e for e in events if e["event"] == "confirm_timed_out")
        self.assertEqual((timeout["dashboards_checked"], timeout["dashboards_flagged"]), (2, 3))

        summary = events[-1]
        self.assertEqual(summary["event"], "scan_complete")
        self.assertEqual((summary["dashboards_broken"], summary["dashboards_unconfirmed"],
                          summary["api_false_positives"]), (2, 1, 1))
        self.assertEqual(state.unconfirmed_dashboard_count, 1)

    def test_confirm_unavailable_reaches_the_log_sink(self):
        with mock.patch.object(server.checker, "scan",
                               return_value=([{}], [api_error("broken", 1, "Latency")])), \
                mock.patch.object(server.confirm, "confirm_broken_dashboards",
                                  side_effect=confirm.BrowserUnavailable("no k6")), \
                mock.patch("builtins.print"):
            sink = []
            server.run_scan_once(server.State(), "https://play.example", 1, 0, 1, 0, True,
                                 log_sink=sink)
        events = [json.loads(line)["event"] for _, line in sink]
        self.assertIn("confirm_unavailable", events)
        self.assertIn("panel_failed", events)


if __name__ == "__main__":
    unittest.main()
