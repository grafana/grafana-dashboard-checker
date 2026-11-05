"""Tests for check_dashboards.py using a local fake Grafana server."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import check_dashboards as cd

DATASOURCES = {
    "Prom": {"uid": "prom", "type": "prometheus", "name": "Prom", "isDefault": True,
             "meta": {"backend": True}},
    "Usage": {"uid": "usage", "type": "prometheus", "name": "grafanacloud-usage"},
    "Static": {"uid": "static", "type": "marcusolsson-static-datasource", "name": "Static",
               "meta": {"backend": False}},
    "Infinity": {"uid": "inf", "type": "yesoreyeram-infinity-datasource", "name": "Infinity"},
    "Gone": {"uid": "gone", "type": "mysql", "name": "Gone"},
    "GitHub": {"uid": "gh", "type": "grafana-github-datasource", "name": "GitHub"},
    "BigQuery": {"uid": "bq", "type": "grafana-bigquery-datasource", "name": "BigQuery"},
    "Sheets": {"uid": "gs", "type": "grafana-googlesheets-datasource", "name": "Sheets"},
    "-- Mixed --": {"uid": "-- Mixed --", "type": "datasource", "name": "-- Mixed --"},
}


def panel(pid, title, ds_uid, ds_type, targets=None, ptype="timeseries"):
    return {
        "id": pid, "title": title, "type": ptype,
        "datasource": {"uid": ds_uid, "type": ds_type},
        "targets": targets if targets is not None else [{"refId": "A", "expr": "up"}],
    }


DASHBOARDS = {
    "healthy": {"title": "Healthy", "panels": [
        panel(1, "OK panel", "prom", "prometheus"),
        panel(2, "Empty panel", "prom", "prometheus", [{"refId": "A", "expr": "empty"}]),
        panel(3, "Text", "prom", "prometheus", [], ptype="text"),
    ]},
    "broken": {"title": "Broken", "panels": [
        panel(1, "Missing source", "gone", "mysql", [{"refId": "A", "rawSql": "select 1"}]),
        panel(2, "Error panel", "prom", "prometheus", [{"refId": "A", "expr": "boom"}]),
        {"type": "row", "title": "r", "collapsed": True, "panels": [
            panel(5, "Nested ok", "prom", "prometheus")]},
    ]},
    "billed": {"title": "Billed", "panels": [
        panel(1, "BQ panel", "bq", "grafana-bigquery-datasource",
              [{"refId": "A", "rawSql": "select * from big"}]),
    ]},
    "throttled": {"title": "Throttled", "panels": [
        panel(1, "GH one", "gh", "grafana-github-datasource", [{"refId": "A", "q": "limit"}]),
        panel(2, "GH two", "gh", "grafana-github-datasource", [{"refId": "A", "q": "x"}]),
        panel(3, "Sheets", "gs", "grafana-googlesheets-datasource", [{"refId": "A", "q": "ok"}]),
    ]},
    "fixes": {"title": "Fixes", "templating": {"list": [
        {"name": "ds", "type": "datasource", "query": "prometheus", "current": {}, "options": [],
         "regex": "(?!grafanacloud-usage).+"},
        {"name": "env", "type": "query", "includeAll": True, "allValue": ".*", "current": {}},
        {"name": "site", "type": "query", "includeAll": True, "current": {}}]},
        "panels": [
            {"id": 1, "title": "Label replace", "type": "timeseries",
             "datasource": {"uid": "$ds"},
             "targets": [{"refId": "A", "datasource": {"type": "prometheus", "uid": "${ds}"},
                          "expr": 'label_replace(up{env=~"$env",site=~"$site"}, "w", "$1", "g", "(.*)")'}]},
            {"id": 2, "title": "Static panel", "type": "table",
             "datasource": {"type": "marcusolsson-static-datasource", "uid": "static"},
             "targets": [{"refId": "A"}]},
            {"id": 3, "title": "Mixed with expression", "type": "table",
             "datasource": {"type": "mixed", "uid": "-- Mixed --"},
             "targets": [
                 {"refId": "Q1", "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": "inf"}},
                 {"refId": "A", "datasource": {"type": "__expr__", "uid": "__expr__"},
                  "type": "sql", "expression": "SELECT * FROM Q1"}]},
            {"id": 4, "title": "No datasource", "type": "timeseries",
             "targets": [{"refId": "A", "expr": "up"}]},
            {"id": 5, "title": "Long range", "type": "timeseries", "timeFrom": "28d",
             "datasource": {"uid": "prom", "type": "prometheus"},
             "targets": [{"refId": "A", "expr": "up"}]},
        ]},
    "vars": {"title": "Vars", "templating": {"list": [
        {"name": "inst", "type": "custom", "current": {"value": "host1"}}]},
        "panels": [
            panel(1, "Uses var", "prom", "prometheus", [{"refId": "A", "expr": "up{i=\"$inst\"}"}]),
            panel(2, "Missing var", "prom", "prometheus", [{"refId": "A", "expr": "up{i=\"$nope\"}"}]),
        ]},
}

SEEN_QUERIES: list = []
SEEN_REQUESTS: list = []


def ds_response(query):
    """Fake backend: behavior is driven by the query text."""
    uid = query["datasource"]["uid"]
    text = json.dumps(query)
    if uid == "usage":
        return None
    if uid == "__expr__":
        return {query["refId"]: {"status": 200, "frames": [{"data": {"values": [["x"]]}}]}}
    if uid == "inf":
        return {query["refId"]: {"status": 200, "frames": [{"data": {"values": [["y"]]}}]}}
    if uid == "gone":
        return None  # signals HTTP 404
    if "boom" in text:
        return {"A": {"status": 500, "error": "connection refused to 10.1.2.3:3306"}}
    if "limit" in text:
        return "429"
    if "empty" in text:
        return {"A": {"status": 200, "frames": [{"data": {"values": [[], []]}}]}}
    return {"A": {"status": 200, "frames": [{"data": {"values": [[1, 2], [3, 4]]}}]}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/search"):
            return self._send(200, [
                {"uid": k, "title": v["title"], "type": "dash-db", "folderTitle": "F"}
                for k, v in DASHBOARDS.items()])
        if self.path.startswith("/api/frontend/settings"):
            return self._send(200, {"datasources": DATASOURCES})
        if self.path.startswith("/api/dashboards/uid/"):
            uid = self.path.rsplit("/", 1)[1]
            return self._send(200, {"dashboard": DASHBOARDS[uid]})
        self._send(404, {})

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        SEEN_QUERIES.extend(payload["queries"])
        SEEN_REQUESTS.append(payload)
        results = {}
        status = 200
        for q in payload["queries"]:
            r = ds_response(q)
            if r is None:
                return self._send(404, {"message": "data source not found"})
            if r == "429":
                return self._send(429, {"message": "rate limit"})
            results.update(r)
        self._send(status, {"results": results})


class CheckerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_checker(self):
        SEEN_QUERIES.clear()
        SEEN_REQUESTS.clear()
        client = cd.Client(self.base, 5)
        lookup = cd.load_datasource_lookup(client)
        tasks = []
        dashboards = cd.list_dashboards(client)
        for meta in dashboards:
            res = client.request("GET", f"/api/dashboards/uid/{meta['uid']}")
            tasks.extend(cd.build_tasks(res.body["dashboard"], meta, lookup))
        results = cd.execute(client, tasks, concurrency=2, delay=0)
        return dashboards, results

    def find(self, results, dash, panel_title):
        hits = [r for r in results
                if r["dashboard_title"] == dash and r["panel_title"] == panel_title]
        self.assertEqual(len(hits), 1, f"{dash}/{panel_title}: {hits}")
        return hits[0]

    def test_classification(self):
        _, results = self.run_checker()
        self.assertEqual(self.find(results, "Healthy", "OK panel")["status"], cd.OK)
        self.assertEqual(self.find(results, "Healthy", "Empty panel")["status"], cd.NO_DATA)
        missing = self.find(results, "Broken", "Missing source")
        self.assertEqual((missing["status"], missing["category"]),
                         (cd.ERROR, "datasource_not_found"))
        err = self.find(results, "Broken", "Error panel")
        self.assertEqual((err["status"], err["category"]),
                         (cd.ERROR, "backend_unreachable"))
        self.assertEqual(self.find(results, "Broken", "Nested ok")["status"], cd.OK)

    def test_text_panels_not_queried(self):
        _, results = self.run_checker()
        self.assertFalse([r for r in results if r["panel_title"] == "Text"])

    def test_bigquery_skipped_and_never_queried(self):
        _, results = self.run_checker()
        r = self.find(results, "Billed", "BQ panel")
        self.assertEqual((r["status"], r["category"]), (cd.SKIPPED, "skipped_billed"))
        self.assertFalse([q for q in SEEN_QUERIES if q["datasource"]["uid"] == "bq"])

    def test_rate_limit_is_inconclusive_and_stops_source(self):
        _, results = self.run_checker()
        one = self.find(results, "Throttled", "GH one")
        two = self.find(results, "Throttled", "GH two")
        self.assertEqual((one["status"], one["category"]), (cd.INCONCLUSIVE, "rate_limited"))
        self.assertEqual((two["status"], two["category"]),
                         (cd.INCONCLUSIVE, "rate_limited_skip"))
        # A different throttled source is unaffected.
        self.assertEqual(self.find(results, "Throttled", "Sheets")["status"], cd.OK)
        # Only one GitHub request was sent before the source was blocked.
        self.assertEqual(len([q for q in SEEN_QUERIES if q["datasource"]["uid"] == "gh"]), 1)

    def test_variables(self):
        _, results = self.run_checker()
        self.assertEqual(self.find(results, "Vars", "Uses var")["status"], cd.OK)
        sent = [q for q in SEEN_QUERIES if q.get("expr") == 'up{i="host1"}']
        self.assertEqual(len(sent), 1)
        missing = self.find(results, "Vars", "Missing var")
        self.assertEqual((missing["status"], missing["category"]),
                         (cd.INCONCLUSIVE, "unresolved_variable"))

    def test_report_hides_raw_errors_by_default(self):
        dashboards, results = self.run_checker()
        text = cd.report(self.base, len(dashboards), results, show_raw=False)
        self.assertNotIn("10.1.2.3", text)
        self.assertIn("backend_unreachable", text)
        raw = cd.report(self.base, len(dashboards), results, show_raw=True)
        self.assertIn("10.1.2.3", raw)

    def test_interpolate_leaves_grafana_macros(self):
        out, unresolved = cd.interpolate("select $__timeFilter(t) $x ${y} [[z]]",
                                         {"x": "1", "y": "2", "z": "3"})
        self.assertEqual(out, "select $__timeFilter(t) 1 2 3")
        self.assertFalse(unresolved)


    # --- Fixes for false positives seen on Play Dev ---

    def test_label_replace_dollar_one_is_not_a_variable(self):
        _, results = self.run_checker()
        r = self.find(results, "Fixes", "Label replace")
        self.assertEqual(r["status"], cd.OK, r)
        sent = [q for q in SEEN_QUERIES if "label_replace" in q.get("expr", "")][0]
        self.assertIn('"$1"', sent["expr"])
        self.assertIn('env=~".*"', sent["expr"])

    def test_datasource_variable_resolves_by_type_and_regex(self):
        _, results = self.run_checker()
        sent = [q for q in SEEN_QUERIES if "label_replace" in q.get("expr", "")][0]
        self.assertEqual(sent["datasource"]["uid"], "prom")  # not grafanacloud-usage

    def test_frontend_only_datasource_is_skipped(self):
        _, results = self.run_checker()
        r = self.find(results, "Fixes", "Static panel")
        self.assertEqual((r["status"], r["category"]), (cd.SKIPPED, "frontend_only_datasource"))
        self.assertFalse([q for q in SEEN_QUERIES if q["datasource"]["uid"] == "static"])

    def test_mixed_panel_with_expression_is_one_request(self):
        _, results = self.run_checker()
        r = self.find(results, "Fixes", "Mixed with expression")
        self.assertEqual(r["status"], cd.OK, r)
        req = [p for p in SEEN_REQUESTS if any(q["refId"] == "Q1" for q in p["queries"])]
        self.assertEqual(len(req), 1)
        self.assertEqual({q["refId"] for q in req[0]["queries"]}, {"Q1", "A"})

    def test_missing_datasource_uses_default(self):
        _, results = self.run_checker()
        self.assertEqual(self.find(results, "Fixes", "No datasource")["status"], cd.OK)

    def test_long_relative_range_scales_interval(self):
        _, results = self.run_checker()
        r = self.find(results, "Fixes", "Long range")
        span = int(r["to"]) - int(r["from"])
        self.assertEqual(span, 28 * 86400 * 1000)
        self.assertGreaterEqual(r["interval_ms"], span // cd.DEFAULT_MAX_POINTS)
        # Whole-second interval: a sub-second remainder breaks the Prometheus
        # datasource's $__interval/$__rate_interval macro expansion server-side
        # (seen live on playdev: it emits "41m19" instead of "41m19s", which
        # Prometheus rejects as a parse error -- a false "broken panel").
        self.assertEqual(r["interval_ms"] % 1000, 0)

    def test_interval_ms_rounds_up_to_whole_seconds(self):
        # 2419200000ms / 1000 points = 2419200ms = 2419.2s: the exact span/maxPoints
        # combination that produced a non-whole-second interval against a real
        # playdev dashboard (grafana_slo_app-fzmog1ehsf38xy3u4wwff, panel 1).
        interval = cd.panel_interval_ms({}, 0, 2_419_200_000, 1000)
        self.assertEqual(interval, 2_420_000)
        self.assertEqual(interval % 1000, 0)

    def test_timeout_is_inconclusive(self):
        status, category, _ = cd.classify_response(cd.HttpResult(-1, None, "timeout"), ["A"])
        self.assertEqual((status, category), (cd.INCONCLUSIVE, "timeout"))



class ClientAuthTest(unittest.TestCase):
    """The token goes to Grafana, and not to wherever Grafana redirects."""

    def test_token_sent_but_not_carried_across_a_redirect(self):
        seen = {}

        def server_for(name, redirect_to=None):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    seen[name] = dict(self.headers)
                    if redirect_to:
                        self.send_response(302)
                        self.send_header("Location", redirect_to() + "/elsewhere")
                        self.end_headers()
                        return
                    body = b"[]"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass
            srv = HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            return f"http://127.0.0.1:{srv.server_address[1]}"

        other = server_for("other")
        grafana = server_for("grafana", redirect_to=lambda: other.replace("127.0.0.1", "localhost"))
        res = cd.Client(grafana, 5, token="glsa_test", org_id="7").request("GET", "/api/search")
        self.assertEqual(res.status, 200)
        self.assertEqual(seen["grafana"].get("Authorization"), "Bearer glsa_test")
        self.assertEqual(seen["grafana"].get("X-Grafana-Org-Id"), "7")
        self.assertNotIn("Authorization", seen["other"])
        self.assertNotIn("X-Grafana-Org-Id", seen["other"])

    def test_anonymous_by_default(self):
        self.assertIsNone(cd.Client("https://x", 1).token)


if __name__ == "__main__":
    unittest.main()
