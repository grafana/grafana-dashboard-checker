"""Tests for the browser scan: browser/scan.py's k6 handling and how server.py
turns its per-dashboard results into events. k6 itself is mocked."""

import json
import subprocess
import unittest
from unittest import mock

import browser.scan as scan
import server


def k6_console_line(payload: dict) -> str:
    """What k6 --log-format=logfmt prints for console.log(JSON.stringify(payload))."""
    raw = json.dumps(payload).replace("\\", "\\\\").replace('"', '\\"')
    return f'time="2026-10-30T17:20:00Z" level=info msg="{raw}" source=console'


DASHBOARDS = [{"uid": "a", "title": "A", "folderTitle": "F"}]


class ParseK6OutputTest(unittest.TestCase):
    def test_keeps_console_lines_keyed_by_dashboard(self):
        output = "\n".join([
            'time="2026-10-30T17:19:00Z" level=info msg="starting" source=browser',
            k6_console_line({"dashboard_uid": "a", "red": [{"panel_title": 'Lat"ency',
                                                            "panel_id": 3, "tab": None}],
                             "settled": True, "panels_seen": 4}),
            k6_console_line({"dashboard_uid": "b", "load_failed": True, "error": "timeout"}),
        ])
        self.assertEqual(scan.parse_k6_output(output), {
            "a": {"red": [{"panel_title": 'Lat"ency', "panel_id": 3, "tab": None}],
                  "settled": True, "panels_seen": 4},
            "b": {"load_failed": True, "error": "timeout"},
        })


@mock.patch.object(scan.shutil, "which", return_value="/usr/bin/k6")
class ScanDashboardsTest(unittest.TestCase):
    def test_timeout_keeps_dashboards_finished_before_the_kill(self, _which):
        partial = k6_console_line({"dashboard_uid": "a", "red": [], "settled": True})
        # TimeoutExpired carries bytes even though run() was asked for text.
        exc = subprocess.TimeoutExpired(cmd="k6", timeout=60, output=partial.encode(),
                                        stderr=b"")
        with mock.patch.object(scan.subprocess, "run", side_effect=exc):
            results, timed_out = scan.scan_dashboards(
                "https://play.example", DASHBOARDS + [{"uid": "b"}], timeout_s=60)
        self.assertTrue(timed_out)
        self.assertEqual(list(results), ["a"])

    def test_k6_stopping_at_max_duration_counts_as_timed_out(self, _which):
        out = k6_console_line({"dashboard_uid": "a", "red": [], "settled": True})
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with mock.patch.object(scan.subprocess, "run", return_value=done) as run:
            _, timed_out = scan.scan_dashboards(
                "https://play.example", DASHBOARDS + [{"uid": "b"}], timeout_s=600)
        self.assertTrue(timed_out)
        # k6 stops itself a minute before the kill timeout.
        self.assertEqual(run.call_args.kwargs["env"]["CHECKER_BROWSER_MAX_DURATION"], "540s")
        self.assertEqual(run.call_args.kwargs["timeout"], 600)


def meta(uid):
    return {"uid": uid, "title": uid.upper(), "folderTitle": "F"}


class ShardTest(unittest.TestCase):
    def test_parse(self):
        self.assertIsNone(server.parse_shard(""))
        self.assertEqual(server.parse_shard("2/4"), (2, 4))
        for bad in ("0/4", "5/4", "4", "a/b"):
            with self.assertRaises(ValueError):
                server.parse_shard(bad)

    def test_membership_ignores_list_position(self):
        self.assertTrue(server.in_shard("anything", None))
        owners = [i for i in (1, 2, 3, 4) if server.in_shard("abc123", (i, 4))]
        self.assertEqual(len(owners), 1)


class RunScanOnceTest(unittest.TestCase):
    def run_scan(self, dashboards, results, timed_out=False, shard=None):
        state, sink = server.State(), []
        with mock.patch.object(server.checker, "list_dashboards", return_value=dashboards), \
                mock.patch.object(server.browser_scan, "scan_dashboards",
                                  return_value=(results, timed_out)) as scan_dashboards, \
                mock.patch("builtins.print"):
            server.run_scan_once(state, "https://play.example", 1, 0, log_sink=sink, shard=shard)
        self.scanned = [d["uid"] for d in scan_dashboards.call_args.args[1]]
        return state, [json.loads(line) for _, line in sink]

    def test_red_panels_unsettled_load_failures_and_unchecked(self):
        state, events = self.run_scan(
            [meta("broken"), meta("clean"), meta("slow"), meta("gone"), meta("unreached")],
            {
                "broken": {"red": [{"panel_title": "Latency", "panel_id": 3, "tab": "Overview"},
                                   {"panel_title": "", "panel_id": 4, "tab": None}],
                           "settled": True, "panels_seen": 5, "rows_expanded": 1,
                           "tabs": ["Overview"], "duration_s": 12.3, "nav_s": 2.1,
                           "settle_s": 9.8},
                "clean": {"red": [], "settled": True, "panels_seen": 2},
                "slow": {"red": [], "settled": False, "panels_seen": 6},
                "gone": {"load_failed": True, "error": "net::ERR_ABORTED", "duration_s": 20.0},
            },
            timed_out=True,
        )

        failed = [e for e in events if e["event"] == "panel_failed"]
        self.assertEqual([(e["dashboard_uid"], e["panel_id"], e["panel_title"], e["tab"])
                          for e in failed],
                         [("broken", 3, "Latency", "Overview"), ("broken", 4, "(untitled)", None)])
        self.assertEqual(failed[0]["link"], "https://play.example/d/broken?viewPanel=panel-3")

        self.assertEqual([e["dashboard_uid"] for e in events if e["event"] == "dashboard_unsettled"],
                         ["slow"])
        self.assertEqual([(e["dashboard_uid"], e["duration_s"]) for e in events
                          if e["event"] == "dashboard_load_failed"], [("gone", 20.0)])
        checked = {e["dashboard_uid"]: e for e in events if e["event"] == "dashboard_checked"}
        self.assertEqual(list(checked), ["broken", "clean", "slow"])
        self.assertEqual(
            {k: checked["broken"][k] for k in ("panels_seen", "panels_red", "rows_expanded",
                                               "tabs_visited", "settled", "duration_s",
                                               "nav_s", "settle_s")},
            {"panels_seen": 5, "panels_red": 2, "rows_expanded": 1, "tabs_visited": 1,
             "settled": True, "duration_s": 12.3, "nav_s": 2.1, "settle_s": 9.8})
        self.assertFalse(checked["slow"]["settled"])
        self.assertIsNone(checked["clean"]["duration_s"])
        timeout = next(e for e in events if e["event"] == "scan_timed_out")
        self.assertEqual((timeout["dashboards_checked"], timeout["dashboards_total"]), (4, 5))

        summary = events[-1]
        self.assertEqual(summary["event"], "scan_complete")
        self.assertEqual(
            {k: summary[k] for k in ("dashboards_total", "dashboards_checked", "dashboards_broken",
                                     "dashboards_unsettled", "dashboards_load_failed",
                                     "dashboards_unchecked", "panels_seen", "panels_red",
                                     "rows_expanded", "tabs_visited")},
            {"dashboards_total": 5, "dashboards_checked": 3, "dashboards_broken": 1,
             "dashboards_unsettled": 1, "dashboards_load_failed": 1, "dashboards_unchecked": 1,
             "panels_seen": 13, "panels_red": 2, "rows_expanded": 1, "tabs_visited": 1})
        self.assertIsNone(summary["shard"])
        self.assertEqual([d["uid"] for d in state.broken_dashboards], ["broken"])
        self.assertTrue(state.last_scan_ok)

    def test_shard_scans_only_its_dashboards(self):
        dashboards = [meta(f"d{i}") for i in range(40)]
        scanned = []
        for i in (1, 2, 3):
            _, events = self.run_scan(dashboards, {}, shard=(i, 3))
            scanned.append(self.scanned)
            summary = events[-1]
            self.assertEqual(summary["shard"], f"{i}/3")
            self.assertEqual(summary["dashboards_total"], len(self.scanned))
        # Every dashboard in exactly one shard, none of them empty.
        self.assertEqual(sorted(sum(scanned, [])), sorted(d["uid"] for d in dashboards))
        self.assertTrue(all(scanned))

    def test_no_browser_fails_the_scan(self):
        state, sink = server.State(), []
        with mock.patch.object(server.checker, "list_dashboards", return_value=[meta("a")]), \
                mock.patch.object(server.browser_scan, "scan_dashboards",
                                  side_effect=scan.BrowserUnavailable("no k6")), \
                mock.patch("builtins.print"):
            server.run_scan_once(state, "https://play.example", 1, 0, log_sink=sink)
        events = [json.loads(line) for _, line in sink]
        self.assertEqual([e["event"] for e in events], ["scan_failed"])
        self.assertFalse(state.last_scan_ok)

    def test_metrics_render(self):
        state, _ = self.run_scan([meta("broken")], {
            "broken": {"red": [{"panel_title": "x", "panel_id": 1, "tab": None}],
                       "settled": True, "panels_seen": 1},
        })
        body = server.render_metrics(state).decode()
        self.assertIn('play_dashboard_checker_dashboard_broken{dashboard_uid="broken",'
                      'dashboard_title="BROKEN",folder="F"} 1', body)
        self.assertIn("play_dashboard_checker_panels_red 1", body)
        self.assertIn("play_dashboard_checker_dashboards_unchecked 0", body)


if __name__ == "__main__":
    unittest.main()
