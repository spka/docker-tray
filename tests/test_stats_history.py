import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

import docker_tray


class StatsHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.stats_file = Path(self.temp_dir.name) / "stats.jsonl"
        self.stats_file_patch = mock.patch.object(docker_tray, "STATS_FILE", self.stats_file)
        self.stats_file_patch.start()
        self.reset_cache()

    def tearDown(self):
        self.stats_file_patch.stop()
        self.reset_cache()
        self.temp_dir.cleanup()

    def reset_cache(self):
        docker_tray.stats_history_cache.update({
            "initialized": False,
            "peaks": {},
            "recent": deque(),
        })

    def write_entries(self, entries):
        with self.stats_file.open("w") as history_file:
            for entry in entries:
                history_file.write(json.dumps(entry) + "\n")

    def test_cache_keeps_peaks_but_only_recent_samples(self):
        now = time.time()
        old_entry = {"t": now - 3600, "name": "web", "cpu": 90.0, "mem": 200, "mem_str": "200B / 1GiB"}
        recent_entry = {"t": now, "name": "web", "cpu": 20.0, "mem": 100, "mem_str": "100B / 1GiB"}
        self.write_entries([old_entry, recent_entry])

        peaks, recent = docker_tray.get_stats_history_snapshot()

        self.assertEqual(90.0, peaks["web"]["cpu"])
        self.assertEqual(200, peaks["web"]["mem"])
        self.assertEqual([recent_entry], recent)

    def test_append_updates_initialized_cache_without_reloading_file(self):
        self.write_entries([])
        docker_tray.ensure_stats_history_cache()
        sample = {"t": time.time(), "name": "api", "cpu": 10.0, "mem": 50, "mem_str": "50B / 1GiB"}

        docker_tray.append_stats_to_file([sample])
        self.stats_file.unlink()
        peaks, recent = docker_tray.get_stats_history_snapshot()

        self.assertEqual(10.0, peaks["api"]["cpu"])
        self.assertEqual([sample], recent)

    def test_automatic_trim_retains_only_configured_history(self):
        now = time.time()
        old_entry = {"t": now - 40 * 86400, "name": "old", "cpu": 1.0, "mem": 1, "mem_str": "1B / 1GiB"}
        recent_entry = {"t": now, "name": "new", "cpu": 2.0, "mem": 2, "mem_str": "2B / 1GiB"}
        self.write_entries([old_entry, recent_entry])

        with mock.patch.object(docker_tray, "STATS_MAX_SIZE_MB", 0):
            docker_tray.trim_stats_file_if_needed()

        entries = [json.loads(line) for line in self.stats_file.read_text().splitlines()]
        self.assertEqual([recent_entry], entries)
        self.assertFalse(docker_tray.stats_history_cache["initialized"])

    def test_current_low_sample_breaks_an_earlier_high_cpu_streak(self):
        history = [
            {"t": 1, "name": "web", "cpu": 90.0},
            {"t": 2, "name": "web", "cpu": 95.0},
        ]
        current = [{"t": 3, "name": "web", "cpu": 10.0}]

        with (
            mock.patch.object(docker_tray, "STATS_CPU_WARNING_PCT", 50.0),
            mock.patch.object(docker_tray, "STATS_CPU_CRITICAL_PCT", 80.0),
        ):
            warning, critical = docker_tray.get_recent_cpu_streak_counts(history, current)

        self.assertEqual(0, warning["web"])
        self.assertEqual(0, critical["web"])

    def test_current_high_sample_continues_a_cpu_streak(self):
        history = [{"t": 1, "name": "web", "cpu": 90.0}]
        current = [{"t": 2, "name": "web", "cpu": 85.0}]

        with (
            mock.patch.object(docker_tray, "STATS_CPU_WARNING_PCT", 50.0),
            mock.patch.object(docker_tray, "STATS_CPU_CRITICAL_PCT", 80.0),
        ):
            warning, critical = docker_tray.get_recent_cpu_streak_counts(history, current)

        self.assertEqual(2, warning["web"])
        self.assertEqual(2, critical["web"])

    def test_current_sample_is_not_counted_twice(self):
        current = {"t": 2, "name": "web", "cpu": 85.0}

        with (
            mock.patch.object(docker_tray, "STATS_CPU_WARNING_PCT", 50.0),
            mock.patch.object(docker_tray, "STATS_CPU_CRITICAL_PCT", 80.0),
        ):
            warning, critical = docker_tray.get_recent_cpu_streak_counts([current], [current])

        self.assertEqual(1, warning["web"])
        self.assertEqual(1, critical["web"])


if __name__ == "__main__":
    unittest.main()
