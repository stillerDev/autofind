"""RAM soft limit: eviction fires only when crossed and out of cooldown."""

from __future__ import annotations

import unittest

from core import api
from core.memguard import SoftLimit, process_rss_mb


class RssTests(unittest.TestCase):
    def test_rss_is_plausible(self):
        rss = process_rss_mb()
        if rss is None:
            self.skipTest("memory info unavailable on this platform")
        # a pytest run lives somewhere between a few MB and a few GB
        self.assertGreater(rss, 1.0)
        self.assertLess(rss, 16 * 1024)


class SoftLimitTests(unittest.TestCase):
    def test_unset_limit_never_exceeded(self):
        limit = SoftLimit(limit_mb=0)
        self.assertFalse(limit.exceeded())

    def test_fires_when_crossed(self):
        limit = SoftLimit(limit_mb=100, rss_fn=lambda: 150.0)
        self.assertTrue(limit.exceeded())

    def test_quiet_when_below(self):
        limit = SoftLimit(limit_mb=100, rss_fn=lambda: 90.0)
        self.assertFalse(limit.exceeded())

    def test_unknown_rss_is_not_a_fire(self):
        limit = SoftLimit(limit_mb=100, rss_fn=lambda: None)
        self.assertFalse(limit.exceeded())

    def test_cooldown_suppresses_repeat_fires(self):
        # after an eviction the allocator holds freed pages for a while, so a
        # second fire right after the first would thrash the fresh caches
        limit = SoftLimit(limit_mb=100, rss_fn=lambda: 150.0)
        limit.mark_evicted("test")
        self.assertFalse(limit.exceeded())

    def test_env_variable_sets_the_limit(self, ):
        import os

        old = os.environ.get("AUTOFIND_RAM_LIMIT_MB")
        try:
            os.environ["AUTOFIND_RAM_LIMIT_MB"] = "512"
            limit = SoftLimit()
            self.assertEqual(limit.limit_mb, 512.0)
            os.environ["AUTOFIND_RAM_LIMIT_MB"] = "0"
            self.assertIsNone(SoftLimit().limit_mb)
        finally:
            if old is None:
                os.environ.pop("AUTOFIND_RAM_LIMIT_MB", None)
            else:
                os.environ["AUTOFIND_RAM_LIMIT_MB"] = old


class EvictionTests(unittest.TestCase):
    def test_evict_caches_clears_everything(self):
        api.DUP_CACHE[("k",)] = [{"a": 1}]
        api.evict_caches("test")
        self.assertEqual(api.DUP_CACHE, {})

    def test_maybe_evict_runs_the_eviction_when_crossed(self):
        crossed = SoftLimit(limit_mb=10, rss_fn=lambda: 999.0)
        original = api.SOFT_LIMIT
        api.SOFT_LIMIT = crossed
        try:
            api.DUP_CACHE[("k",)] = [{"a": 1}]
            api._maybe_evict()
            self.assertEqual(api.DUP_CACHE, {})
            self.assertEqual(crossed.last_reason, "rss over 10 MB")
        finally:
            api.SOFT_LIMIT = original

    def test_maybe_evict_skipped_while_job_running(self):
        # an index job holds the write lock and warm Stores: dropping caches
        # under it would slow the job, not save memory
        crossed = SoftLimit(limit_mb=10, rss_fn=lambda: 999.0)
        original_limit, original_running = api.SOFT_LIMIT, api.JOB.running
        api.SOFT_LIMIT = crossed
        api.JOB.running = True
        try:
            api.DUP_CACHE[("k",)] = [{"a": 1}]
            api._maybe_evict()
            self.assertEqual(api.DUP_CACHE, {("k",): [{"a": 1}]})
        finally:
            api.SOFT_LIMIT = original_limit
            api.JOB.running = original_running


if __name__ == "__main__":
    unittest.main()
