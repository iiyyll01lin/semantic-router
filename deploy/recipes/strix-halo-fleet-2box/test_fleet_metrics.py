#!/usr/bin/env python3
"""Regression tests for fleet_metrics (stdlib `unittest`, no dependencies).

These lock in two subtle fixes that shipped broken once:

  * Convergence truth -- the audit log reuses version numbers across runs, so a
    ``v9`` applied in an earlier run must NOT count as "converged today" and must
    NOT inflate the cross-box span with a stale (cross-run) timestamp. The FINAL
    ``fleet-status.txt`` snapshot is authoritative.
  * summarize() poll formatting -- a missing poll interval renders as ``poll=n/a``
    (never a bare ``None``).

Run:  python3 test_fleet_metrics.py     # exits nonzero on any failure
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_metrics  # noqa: E402  (path is fixed up above so this resolves)


def _find_version(per_version, version):
    """Return the per-version dict for ``version`` (or None)."""
    for entry in per_version:
        if entry["version"] == version:
            return entry
    return None


class ConvergenceMetricsTest(unittest.TestCase):
    def test_stale_cross_run_is_not_converged(self):
        # v9 was applied by halo-b in an OLD run and by halo-a in a NEW run
        # (~113813s apart), but the FINAL status has halo-b back on v3. The fleet
        # is therefore NOT converged on v9, and the stale halo-b row must not be
        # counted -- so no giant cross-box span materializes.
        audit_rows = [
            {
                "ts": "2026-07-14T00:00:00Z",
                "box": "halo-b",
                "version": "v9",
                "result": "in_sync",
            },
            {
                "ts": "2026-07-15T07:36:53Z",
                "box": "halo-a",
                "version": "v9",
                "result": "in_sync",
            },
            {
                "ts": "2026-07-15T07:37:00Z",
                "box": "halo-b",
                "version": "v3",
                "result": "in_sync",
            },
        ]
        boxes = {
            "halo-a": {"version": "v9", "result": "in_sync", "hash": "aaaa1111"},
            "halo-b": {"version": "v3", "result": "in_sync", "hash": "bbbb2222"},
        }
        m = fleet_metrics.convergence_metrics(audit_rows, boxes, desired_version="v9")

        self.assertIs(m["converged_all_boxes"], False)
        self.assertEqual(m["converged_versions"], 0)
        self.assertIsNone(m["max_cross_box_span_seconds"])

        v9 = _find_version(m["per_version"], "v9")
        self.assertIsNotNone(v9)
        self.assertIs(v9["applied_by_all"], False)
        # Only halo-a actually ended at v9, so the span collapses to 0.0 rather
        # than the ~113813s cross-run artifact the old code reported.
        self.assertEqual(v9["span_seconds"], 0.0)

    def test_healthy_fleet_is_converged(self):
        # Both boxes end at v9 with equal hashes; the v9 audit rows are seconds
        # apart -> converged, with a small finite cross-box span.
        audit_rows = [
            {
                "ts": "2026-07-15T02:28:14Z",
                "box": "halo-a",
                "version": "v9",
                "result": "in_sync",
            },
            {
                "ts": "2026-07-15T02:28:17Z",
                "box": "halo-b",
                "version": "v9",
                "result": "in_sync",
            },
        ]
        boxes = {
            "halo-a": {"version": "v9", "result": "in_sync", "hash": "7831beef"},
            "halo-b": {"version": "v9", "result": "in_sync", "hash": "7831beef"},
        }
        m = fleet_metrics.convergence_metrics(audit_rows, boxes, desired_version="v9")

        self.assertIs(m["converged_all_boxes"], True)
        self.assertEqual(m["converged_versions"], 1)
        self.assertIsNotNone(m["max_cross_box_span_seconds"])
        self.assertEqual(m["max_cross_box_span_seconds"], 3.0)

        v9 = _find_version(m["per_version"], "v9")
        self.assertIsNotNone(v9)
        self.assertIs(v9["applied_by_all"], True)
        self.assertEqual(v9["span_seconds"], 3.0)


class SummarizePollTest(unittest.TestCase):
    @staticmethod
    def _metrics(poll):
        return {
            "bundle": "run-test",
            "fleet_mode": "mock",
            "boxes": ["halo-a", "halo-b"],
            "desired_version": "v9",
            "audit_count": 2,
            "hash_agreement": True,
            "convergence": {
                "per_version": [],
                "converged_versions": 1,
                "converged_all_boxes": True,
                "max_cross_box_span_seconds": None,
                "mean_cross_box_span_seconds": None,
                "poll_interval_seconds": poll,
            },
            "router_readiness_seconds": {},
        }

    def test_poll_none_renders_na(self):
        out = fleet_metrics.summarize(self._metrics(None))
        self.assertIn("poll=n/a", out)
        # A missing poll (or span) must never leak a bare "None" into the summary.
        self.assertNotIn("None", out)

    def test_poll_three_renders_seconds(self):
        out = fleet_metrics.summarize(self._metrics(3))
        self.assertIn("poll=3.0s", out)
        self.assertNotIn("None", out)


class BuildMetricsBundleTest(unittest.TestCase):
    def test_build_metrics_from_bundle(self):
        # Keep the analyzer fully offline: no live-CCP enrichment or JSON audit.
        for var in ("CCP_URL", "FLEET_TOKEN", "CCP_AUDIT_LOG"):
            os.environ.pop(var, None)

        with tempfile.TemporaryDirectory() as bundle:
            with open(
                os.path.join(bundle, "fleet-status.txt"), "w", encoding="utf-8"
            ) as fh:
                fh.write(
                    "desired_version=v9 audit_count=2\n"
                    "halo-a version=v9 result=in_sync hash=7831beefcafe\n"
                    "halo-b version=v9 result=in_sync hash=7831beefcafe\n"
                )
            with open(
                os.path.join(bundle, "fleet-audit.txt"), "w", encoding="utf-8"
            ) as fh:
                fh.write(
                    "2026-07-15T02:28:14Z halo-a v9 in_sync\n"
                    "2026-07-15T02:28:17Z halo-b v9 in_sync\n"
                )
            with open(os.path.join(bundle, "fleet.env"), "w", encoding="utf-8") as fh:
                fh.write("export FLEET_MODE=mock\nexport POLL_INTERVAL=3\n")

            m = fleet_metrics.build_metrics(bundle)

        self.assertEqual(m["desired_version"], "v9")
        self.assertEqual(m["fleet_mode"], "mock")
        self.assertEqual(m["boxes"], ["halo-a", "halo-b"])
        self.assertEqual(m["audit_count"], 2)
        # POLL_INTERVAL regex + hash_agreement scoping (both boxes on v9, equal
        # hashes) + convergence truth are all exercised here.
        self.assertEqual(m["convergence"]["poll_interval_seconds"], 3)
        self.assertIs(m["hash_agreement"], True)
        self.assertIs(m["convergence"]["converged_all_boxes"], True)
        # The whole record must be JSON-serializable (it is written to disk).
        json.dumps(m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
