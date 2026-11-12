"""Checks on dashboards/results.json, the ready-made results dashboard.

It's imported into other people's Grafana, pointed at their Loki and their
stream, so nothing in it may assume a particular data source, job label or
Grafana version newer than the checker supports.
"""

import json
import os
import re
import unittest

from dashboard_checker import grafana_api

PATH = os.path.join(os.path.dirname(__file__), "..", "dashboards", "results.json")


def panels(dashboard):
    for p in dashboard["panels"]:
        yield p
        yield from p.get("panels", [])


class ResultsDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH, encoding="utf-8") as f:
            cls.dashboard = json.load(f)
        cls.targets = [(p["title"], t) for p in panels(cls.dashboard) for t in p.get("targets", [])]

    def test_every_query_reads_the_stream_variable(self):
        self.assertTrue(self.targets)
        for title, t in self.targets:
            with self.subTest(panel=title):
                self.assertTrue(t["expr"].startswith("$stream") or "($stream" in t["expr"]
                                or "(count_over_time($stream" in t["expr"], t["expr"])
                # LOKI_JOB_LABEL is the user's: no stream may be spelled out.
                self.assertNotRegex(t["expr"], r"\{\s*\w+\s*=")

    def test_every_query_uses_the_data_source_variable(self):
        for p in panels(self.dashboard):
            if not p.get("targets"):  # rows and the text panel run no queries
                continue
            with self.subTest(panel=p["title"]):
                self.assertEqual(p["datasource"], {"type": "loki", "uid": "${datasource}"})
                for t in p.get("targets", []):
                    self.assertEqual(t["datasource"], p["datasource"])
        names = {v["name"]: v for v in self.dashboard["templating"]["list"]}
        self.assertEqual(names["datasource"]["type"], "datasource")
        self.assertEqual(names["datasource"]["query"], "loki")
        # Defaults to the checker's own default LOKI_JOB_LABEL.
        self.assertEqual(names["stream"]["type"], "textbox")
        self.assertEqual(names["stream"]["query"], '{job="dashboard-checker"}')

    def test_imports_on_the_oldest_supported_grafana(self):
        # Classic JSON, at a schema version Grafana 11.3 understands.
        self.assertNotIn("apiVersion", self.dashboard)
        self.assertLessEqual(self.dashboard["schemaVersion"], 39)
        self.assertEqual(grafana_api.MIN_GRAFANA_VERSION, (11, 3),
                         "re-check the dashboard on the new minimum Grafana version")
        for p in panels(self.dashboard):
            for tr in p.get("transformations", []):
                if tr["id"] == "extractFields":
                    # On Grafana 11 keepTime adds a second "Time" field, which
                    # breaks grouping runs by scan_id.
                    self.assertFalse(tr["options"].get("keepTime"), p["title"])

    def test_only_fields_the_checker_sends(self):
        # Every event and field the dashboard reads is one docs/reference.md lists.
        with open(os.path.join(os.path.dirname(PATH), "..", "docs", "reference.md"),
                  encoding="utf-8") as f:
            reference = f.read()
        text = json.dumps(self.dashboard)
        events = {e for name in re.findall(r'event=~?\\"([a-z_|]+)\\"', text)
                  for e in name.split("|")}
        fields = set(re.findall(r"unwrap (\w+)", text))
        # And the scan_complete fields the runs panels group by scan_id.
        for p in panels(self.dashboard):
            for tr in p.get("transformations", []):
                if tr["id"] == "groupBy":
                    fields |= set(tr["options"]["fields"]) - {"Time"}
        self.assertGreater(len(events), 5)
        self.assertGreater(len(fields), 5)
        for name in events | fields:
            self.assertTrue(f"`{name}`" in reference, f"{name} isn't in docs/reference.md")


if __name__ == "__main__":
    unittest.main()
