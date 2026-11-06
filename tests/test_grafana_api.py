"""Tests for grafana_api.py against local HTTP servers."""

import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import grafana_api as api


def serve(test: unittest.TestCase, handler: type) -> str:
    """Starts handler on a free local port for the test's duration; returns its URL."""
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_address[1]}"


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ClientAuthTest(unittest.TestCase):
    """The token goes to Grafana, and not to wherever Grafana redirects."""

    def test_token_sent_but_not_carried_across_a_redirect(self):
        seen = {}

        def handler_for(name, redirect_to=None):
            class H(QuietHandler):
                def do_GET(self):
                    seen[name] = dict(self.headers)
                    if redirect_to:
                        self.send_response(302)
                        self.send_header("Location", redirect_to() + "/elsewhere")
                        self.end_headers()
                        return
                    self.send_json(200, [])
            return H

        other = serve(self, handler_for("other"))
        grafana = serve(self, handler_for(
            "grafana", redirect_to=lambda: other.replace("127.0.0.1", "localhost")))
        res = api.Client(grafana, 5, token="glsa_test", org_id="7").get("/api/search")
        self.assertEqual(res.status, 200)
        self.assertEqual(seen["grafana"].get("Authorization"), "Bearer glsa_test")
        self.assertEqual(seen["grafana"].get("X-Grafana-Org-Id"), "7")
        self.assertNotIn("Authorization", seen["other"])
        self.assertNotIn("X-Grafana-Org-Id", seen["other"])

    def test_anonymous_by_default(self):
        client = api.Client("https://x", 1)
        self.assertIsNone(client.token)
        self.assertIsNone(client.org_id)


class ListDashboardsTest(unittest.TestCase):
    def test_reads_every_page(self):
        pages = []

        class H(QuietHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                page = int(query["page"][0])
                pages.append((query["type"][0], query["limit"][0], page))
                # Two full pages of 2, then a short one.
                items = {1: ["a", "b"], 2: ["c", "d"], 3: ["e"]}.get(page, [])
                self.send_json(200, [{"uid": u} for u in items])

        with mock.patch.object(api, "SEARCH_PAGE_SIZE", 2):
            dashboards = api.list_dashboards(api.Client(serve(self, H), 5))
        self.assertEqual([d["uid"] for d in dashboards], ["a", "b", "c", "d", "e"])
        self.assertEqual(pages, [("dash-db", "2", 1), ("dash-db", "2", 2), ("dash-db", "2", 3)])

    def test_failed_search_raises(self):
        class H(QuietHandler):
            def do_GET(self):
                self.send_json(401, {"message": "Unauthorized"})

        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            api.list_dashboards(api.Client(serve(self, H), 5))

    def test_unreachable_grafana_raises(self):
        srv = HTTPServer(("127.0.0.1", 0), QuietHandler)
        port = srv.server_address[1]
        srv.server_close()  # nothing listening on that port now
        with self.assertRaisesRegex(RuntimeError, "HTTP -2"):
            api.list_dashboards(api.Client(f"http://127.0.0.1:{port}", 5))


class PanelLinkTest(unittest.TestCase):
    def test_dashboard_and_panel(self):
        self.assertEqual(api.panel_link("https://play.example/", {"dashboard_uid": "abc"}),
                         "https://play.example/d/abc")
        self.assertEqual(api.panel_link("https://play.example",
                                        {"dashboard_uid": "abc", "panel_id": 3}),
                         "https://play.example/d/abc?viewPanel=panel-3")
        # Panel 0 is a real id, not "no panel".
        self.assertEqual(api.panel_link("https://play.example",
                                        {"dashboard_uid": "abc", "panel_id": 0}),
                         "https://play.example/d/abc?viewPanel=panel-0")


if __name__ == "__main__":
    unittest.main()
