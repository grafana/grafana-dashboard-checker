"""Tests for the browser scan: browser/scan.py's k6 handling (against a fake
k6 script) and how server.py turns per-dashboard results into events."""

import json
import os
import sys
import tempfile
import time
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


# Stands in for k6: prints the lines it's given (to stderr, like k6), with
# sleeps between them. The plan comes in through K6_FAKE_PLAN, which the
# allowlisted environment passes on.
FAKE_K6 = """\
import json, os, subprocess, sys, time
plan = json.loads(os.environ["K6_FAKE_PLAN"])
if plan.get("env_out"):
    with open(plan["env_out"], "w") as f:
        json.dump(dict(os.environ), f)
if plan.get("child_pid_out"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(plan["child_pid_out"], "w") as f:
        f.write(str(child.pid))
for step in plan["steps"]:
    if "sleep" in step:
        time.sleep(step["sleep"])
    else:
        print(step["line"], file=sys.stderr, flush=True)
"""


def result_line(uid, **result):
    return k6_console_line({"dashboard_uid": uid, "red": [], "settled": True, **result})


class ScanDashboardsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.k6 = os.path.join(self.dir.name, "k6")
        with open(self.k6, "w") as f:
            f.write(f"#!{sys.executable}\n" + FAKE_K6)
        os.chmod(self.k6, 0o755)

    def scan(self, steps, dashboards, timeout_s=30, environ=None, **kw):
        plan = {"steps": steps, **kw.pop("plan_extra", {})}
        env = {"PATH": os.environ.get("PATH", ""), **(environ or {}),
               "K6_FAKE_PLAN": json.dumps(plan)}
        with mock.patch.object(scan.shutil, "which", return_value=self.k6), \
                mock.patch.dict(scan.os.environ, env, clear=True):
            return scan.scan_dashboards("https://play.example", dashboards,
                                        timeout_s=timeout_s, **kw)

    def test_results_arrive_while_k6_is_still_running(self):
        arrived = []
        results, timed_out = self.scan(
            [{"line": result_line("a")}, {"sleep": 1.5}, {"line": result_line("b")}],
            [{"uid": "a"}, {"uid": "b"}],
            on_result=lambda uid, r: arrived.append((uid, time.monotonic())))
        self.assertEqual([uid for uid, _ in arrived], ["a", "b"])
        # "a" was handed over before k6 got round to "b", not at the end.
        self.assertGreater(arrived[1][1] - arrived[0][1], 1.0)
        self.assertEqual(set(results), {"a", "b"})
        self.assertFalse(timed_out)

    def test_timeout_stops_k6_and_what_it_started_but_keeps_finished_dashboards(self):
        pid_file = os.path.join(self.dir.name, "child.pid")
        started = time.monotonic()
        results, timed_out = self.scan(
            [{"line": result_line("a")}, {"sleep": 60}, {"line": result_line("b")}],
            [{"uid": "a"}, {"uid": "b"}], timeout_s=2,
            plan_extra={"child_pid_out": pid_file})
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(timed_out)
        self.assertEqual(list(results), ["a"])
        # k6's own children (Chromium, in real life) went with it.
        with open(pid_file) as f:
            child = int(f.read())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail("k6's child process outlived the timeout")

    def test_k6_exiting_short_of_every_dashboard_counts_as_timed_out(self):
        # What k6 hitting its own maxDuration looks like.
        results, timed_out = self.scan([{"line": result_line("a")}],
                                       [{"uid": "a"}, {"uid": "b"}])
        self.assertEqual(list(results), ["a"])
        self.assertTrue(timed_out)

    def test_k6_gets_an_allowlisted_env_and_the_scan_settings(self):
        env_file = os.path.join(self.dir.name, "env.json")
        self.scan([], DASHBOARDS, timeout_s=600, token="glsa_test", org_id="4",
                  environ={"HOME": "/h", "K6_BROWSER_HEADLESS": "true",
                           "CHECKER_BROWSER_VUS": "2", "LOKI_TOKEN": "secret",
                           "AWS_SECRET_ACCESS_KEY": "s"},
                  plan_extra={"env_out": env_file})
        with open(env_file) as f:
            k6_env = json.load(f)
        self.assertNotIn("LOKI_TOKEN", k6_env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", k6_env)
        self.assertEqual({k: k6_env[k] for k in ("HOME", "K6_BROWSER_HEADLESS",
                                                 "CHECKER_BROWSER_VUS", "CHECKER_GRAFANA_TOKEN",
                                                 "CHECKER_ORG_ID", "CHECKER_INCLUDE_ERROR_TEXT",
                                                 "CHECKER_BASE_URL",
                                                 "CHECKER_BROWSER_MAX_DURATION")},
                         {"HOME": "/h", "K6_BROWSER_HEADLESS": "true",
                          "CHECKER_BROWSER_VUS": "2", "CHECKER_GRAFANA_TOKEN": "glsa_test",
                          "CHECKER_ORG_ID": "4", "CHECKER_INCLUDE_ERROR_TEXT": "false",
                          "CHECKER_BASE_URL": "https://play.example",
                          # k6 stops itself a minute before the kill.
                          "CHECKER_BROWSER_MAX_DURATION": "540s"})

    def test_anonymous_scan_sends_no_token(self):
        env = scan.k6_env("https://x.example", "/tmp/d.json", 600, None, None, False,
                          environ={"CHECKER_GRAFANA_TOKEN": "stray"})
        self.assertNotIn("CHECKER_GRAFANA_TOKEN", env)

    def test_missing_k6_or_browser_module_raises(self):
        with mock.patch.object(scan.shutil, "which", return_value=None), \
                self.assertRaises(scan.BrowserUnavailable):
            scan.scan_dashboards("https://play.example", DASHBOARDS, timeout_s=5)
        with mock.patch.object(scan.shutil, "which", return_value="/nonexistent/k6"), \
                self.assertRaises(scan.BrowserUnavailable):
            scan.scan_dashboards("https://play.example", DASHBOARDS, timeout_s=5)
        with self.assertRaises(scan.BrowserUnavailable):
            self.scan([{"line": 'level=error msg="k6/browser: command while running browser"'}],
                      DASHBOARDS)


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


def config(**kw):
    return server.Config(base_url="https://play.example", timeout=1, **kw)


class RunScanOnceTest(unittest.TestCase):
    def run_scan(self, dashboards, results, timed_out=False, **cfg):
        state, sink = server.State(), []
        self.lines_during_scan = []

        def fake_scan(base_url, dashboards, **kw):
            # Like scan_dashboards: each result goes to on_result as it "finishes".
            for uid, r in results.items():
                kw["on_result"](uid, dict(r))
                self.lines_during_scan.append(len(sink))
            return results, timed_out

        with mock.patch.object(server.grafana_api, "list_dashboards", return_value=dashboards), \
                mock.patch.object(server.browser_scan, "scan_dashboards",
                                  side_effect=fake_scan) as scan_dashboards, \
                mock.patch("builtins.print"):
            server.run_scan_once(state, config(**cfg), log_sink=sink)
        self.scan_call = scan_dashboards.call_args
        self.scanned = [d["uid"] for d in scan_dashboards.call_args.args[1]]
        return state, [json.loads(line) for _, line in sink]

    def test_red_panels_unsettled_load_failures_and_unchecked(self):
        state, events = self.run_scan(
            [meta("broken"), meta("clean"), meta("slow"), meta("gone"), meta("unreached")],
            {
                "broken": {"red": [{"panel_title": "Latency", "panel_id": 3, "tab": "Overview",
                                    "error": "Unauthorized"},
                                   {"panel_title": "", "panel_id": 4, "tab": None, "error": None}],
                           "settled": True, "panels_seen": 5, "rows_expanded": 1,
                           "tabs": ["Overview"], "duration_s": 12.3, "nav_s": 2.1,
                           "settle_s": 9.8},
                "clean": {"red": [], "settled": True, "panels_seen": 2},
                "slow": {"red": [], "settled": False, "panels_seen": 6},
                "gone": {"load_failed": True, "error": "net::ERR_ABORTED", "duration_s": 20.0},
            },
            timed_out=True,
            include_error_text=True,
        )

        failed = [e for e in events if e["event"] == "panel_failed"]
        self.assertEqual([(e["dashboard_uid"], e["panel_id"], e["panel_title"], e["tab"],
                           e["error"]) for e in failed],
                         [("broken", 3, "Latency", "Overview", "Unauthorized"),
                          ("broken", 4, "(untitled)", None, None)])
        self.assertEqual(failed[0]["link"], "https://play.example/d/broken?viewPanel=panel-3")

        unsettled = [e["dashboard_uid"] for e in events if e["event"] == "dashboard_unsettled"]
        self.assertEqual(unsettled, ["slow"])
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

    def test_events_go_out_as_each_dashboard_finishes(self):
        _, events = self.run_scan([meta("a"), meta("b")], {
            "a": {"red": [], "settled": True, "panels_seen": 1},
            "b": {"red": [], "settled": True, "panels_seen": 1},
        })
        # a's dashboard_checked was already in the sink when b finished.
        self.assertEqual(self.lines_during_scan, [1, 2])
        self.assertEqual([e["event"] for e in events],
                         ["dashboard_checked", "dashboard_checked", "scan_complete"])

    def test_every_event_carries_the_scan_id(self):
        _, events = self.run_scan([meta("broken"), meta("x")], {
            "broken": {"red": [{"panel_title": "p", "panel_id": 1, "tab": None}],
                       "settled": False, "panels_seen": 1},
        }, timed_out=True, scan_id="run-42")
        self.assertEqual({e["event"] for e in events},
                         {"dashboard_checked", "dashboard_unsettled", "panel_failed",
                          "scan_timed_out", "scan_complete"})
        self.assertEqual({e["scan_id"] for e in events}, {"run-42"})
        # Right after "event", so it's easy to spot in a raw log line.
        self.assertEqual(list(events[0])[:2], ["event", "scan_id"])

    def test_scan_id_is_generated_per_scan_when_not_set(self):
        _, first = self.run_scan([meta("a")], {})
        _, second = self.run_scan([meta("a")], {})
        ids = {first[-1]["scan_id"], second[-1]["scan_id"]}
        self.assertEqual(len(ids), 2)
        for scan_id in ids:
            self.assertRegex(scan_id, r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")

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
        self.assertEqual(sorted(uid for shard in scanned for uid in shard),
                         sorted(d["uid"] for d in dashboards))
        self.assertTrue(all(scanned))

    def test_no_browser_fails_the_scan(self):
        state, sink = server.State(), []
        with mock.patch.object(server.grafana_api, "list_dashboards", return_value=[meta("a")]), \
                mock.patch.object(server.browser_scan, "scan_dashboards",
                                  side_effect=scan.BrowserUnavailable("no k6")), \
                mock.patch("builtins.print"):
            server.run_scan_once(state, config(), log_sink=sink)
        events = [json.loads(line) for _, line in sink]
        self.assertEqual([e["event"] for e in events], ["scan_failed"])
        self.assertIn("scan_id", events[0])
        self.assertFalse(state.last_scan_ok)

    def test_metrics_render(self):
        state, _ = self.run_scan([meta("broken")], {
            "broken": {"red": [{"panel_title": "x", "panel_id": 1, "tab": None}],
                       "settled": True, "panels_seen": 1},
        })
        body = server.render_metrics(state).decode()
        self.assertIn('dashboard_checker_dashboard_broken{dashboard_uid="broken",'
                      'dashboard_title="BROKEN",folder="F"} 1', body)
        self.assertIn("\ndashboard_checker_panels_red 1", body)
        self.assertIn("\ndashboard_checker_dashboards_unchecked 0", body)

    def test_error_text_is_dropped_unless_asked_for(self):
        results = {"broken": {"red": [{"panel_title": "x", "panel_id": 1, "tab": None,
                                       "error": "dial tcp 10.0.0.5:5432: refused"}],
                              "settled": True, "panels_seen": 1}}
        _, events = self.run_scan([meta("broken")], results)
        self.assertFalse(self.scan_call.kwargs["include_error_text"])
        failed = next(e for e in events if e["event"] == "panel_failed")
        self.assertIsNone(failed["error"])

        _, events = self.run_scan([meta("broken")], results, include_error_text=True)
        self.assertTrue(self.scan_call.kwargs["include_error_text"])
        failed = next(e for e in events if e["event"] == "panel_failed")
        self.assertEqual(failed["error"], "dial tcp 10.0.0.5:5432: refused")

    def test_token_reaches_api_client_and_browser(self):
        with mock.patch.object(server.grafana_api, "Client") as client:
            self.run_scan([meta("a")], {}, token="glsa_test", org_id="3")
        client.assert_called_once_with("https://play.example", 1, "glsa_test", "3")
        self.assertEqual((self.scan_call.kwargs["token"], self.scan_call.kwargs["org_id"]),
                         ("glsa_test", "3"))

    def test_filters_apply_before_the_browser(self):
        dashboards = [
            {"uid": "a", "folderTitle": "Prod", "folderUid": "f1", "tags": ["team-a"]},
            {"uid": "b", "folderTitle": "Prod", "folderUid": "f1", "tags": ["bigquery"]},
            {"uid": "c", "folderTitle": "Sandbox", "folderUid": "f2", "tags": []},
            {"uid": "d", "tags": ["team-a"]},  # root folder
            {"uid": "e", "folderTitle": "Prod", "folderUid": "f1", "tags": []},
        ]
        self.run_scan(dashboards, {}, filters=server.Filters(
            folders=frozenset({"f1", "General"}), exclude_tags=frozenset({"bigquery"}),
            exclude_uids=frozenset({"e"})))
        self.assertEqual(self.scanned, ["a", "d"])
        self.run_scan(dashboards, {}, filters=server.Filters(tags=frozenset({"team-a"}),
                                                             exclude_folders=frozenset({"General"})))
        self.assertEqual(self.scanned, ["a"])


class ConfigTest(unittest.TestCase):
    def test_base_url_is_required(self):
        for bad in ({}, {"CHECKER_BASE_URL": ""}, {"CHECKER_BASE_URL": "play.grafana.org"}):
            with self.assertRaises(ValueError):
                server.Config.from_env(bad)

    def test_reads_everything_and_hides_the_token(self):
        cfg = server.Config.from_env({
            "CHECKER_BASE_URL": "https://x.example/", "CHECKER_GRAFANA_TOKEN": "glsa_test",
            "CHECKER_ORG_ID": "2", "CHECKER_SHARD": "1/2", "CHECKER_INCLUDE_ERROR_TEXT": "true",
            "CHECKER_FOLDERS": "Prod, ops ,", "CHECKER_EXCLUDE_UIDS": "u1",
        })
        self.assertEqual(cfg.base_url, "https://x.example")
        self.assertEqual((cfg.token, cfg.org_id, cfg.shard), ("glsa_test", "2", (1, 2)))
        self.assertTrue(cfg.include_error_text)
        self.assertEqual(cfg.filters.folders, {"Prod", "ops"})
        self.assertEqual(cfg.filters.exclude_uids, {"u1"})
        self.assertNotIn("glsa_test", repr(cfg))

    def test_error_text_defaults_off_and_rejects_typos(self):
        self.assertFalse(server.Config.from_env({"CHECKER_BASE_URL": "https://x"}).include_error_text)
        with self.assertRaises(ValueError):
            server.Config.from_env({"CHECKER_BASE_URL": "https://x",
                                    "CHECKER_INCLUDE_ERROR_TEXT": "ture"})

    def test_scan_id(self):
        self.assertIsNone(server.Config.from_env({"CHECKER_BASE_URL": "https://x"}).scan_id)
        self.assertEqual(server.Config.from_env({"CHECKER_BASE_URL": "https://x",
                                                 "CHECKER_SCAN_ID": " 123-1 "}).scan_id, "123-1")
        for bad in ('a"b', "a b", "x" * 129, "a}b"):
            with self.assertRaises(ValueError):
                server.Config.from_env({"CHECKER_BASE_URL": "https://x", "CHECKER_SCAN_ID": bad})

    def test_scan_id_is_rejected_in_continuous_mode(self):
        env = {"CHECKER_BASE_URL": "https://x", "CHECKER_SCAN_ID": "fixed"}
        with mock.patch.dict(server.os.environ, env, clear=True), \
                mock.patch.object(server, "run_scan_once") as run, \
                mock.patch("sys.stderr"):
            self.assertEqual(server.main(), 2)
        run.assert_not_called()

    def test_loki_credentials_checked_up_front(self):
        self.assertIsNone(server.loki_settings({}))
        with self.assertRaises(ValueError):
            server.loki_settings({"LOKI_URL": "https://logs.example", "LOKI_USERNAME": "1"})
        self.assertEqual(server.loki_settings({"LOKI_URL": "u", "LOKI_USERNAME": "1",
                                               "LOKI_TOKEN": "t"})["job"], "dashboard-checker")

    def test_main_exits_2_on_bad_config_without_scanning(self):
        with mock.patch.dict(server.os.environ, {"CHECKER_BASE_URL": ""}, clear=True), \
                mock.patch.object(server, "run_scan_once") as run, \
                mock.patch("sys.stderr"):
            self.assertEqual(server.main(), 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
