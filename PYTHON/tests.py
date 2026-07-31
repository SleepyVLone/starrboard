#!/usr/bin/env python3
# Stdlib-only tests (no pip deps, matching the rest of this project) covering
# the pieces of the dashboard that are pure logic -- no live Sonarr/Radarr/
# qBittorrent needed to run these. Run with: python3 tests.py

import os
import tempfile
import unittest
from datetime import datetime

from arr_common.state import load_json_state, save_json_state
from arr_dashboard import data


class FormatBytesTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(data.format_bytes(500), "500.0 B")

    def test_kib(self):
        self.assertEqual(data.format_bytes(2048), "2.0 KiB")

    def test_gib(self):
        self.assertEqual(data.format_bytes(5 * 1024 ** 3), "5.0 GiB")

    def test_tib(self):
        self.assertEqual(data.format_bytes(3 * 1024 ** 4), "3.0 TiB")


class NextRunTimeTests(unittest.TestCase):
    def test_picks_the_soonest_future_candidate(self):
        now = datetime(2026, 7, 31, 10, 5, 0)
        result = data.next_run_time(now=now, minutes=[17, 47])
        self.assertEqual(result, datetime(2026, 7, 31, 10, 17, 0))

    def test_rolls_over_to_the_next_hour(self):
        now = datetime(2026, 7, 31, 10, 50, 0)
        result = data.next_run_time(now=now, minutes=[17, 47])
        self.assertEqual(result, datetime(2026, 7, 31, 11, 17, 0))


class ParseLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        self.tmp.write(
            "2026-07-30 10:17:00 === run ===\n"
            "Dead torrents: nothing to clean\n"
            "2026-07-30 10:47:00 === run ===\n"
            "DEAD: 'Some Show S01E01' -> removed 1 queue row(s)\n"
        )
        self.tmp.close()
        self.original_log_path = data.LOG_PATH
        data.LOG_PATH = self.tmp.name

    def tearDown(self):
        data.LOG_PATH = self.original_log_path
        os.unlink(self.tmp.name)

    def test_most_recent_run_first(self):
        runs = data.parse_log()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["timestamp"], "2026-07-30 10:47:00")
        self.assertEqual(runs[1]["timestamp"], "2026-07-30 10:17:00")

    def test_lines_grouped_under_the_right_run(self):
        runs = data.parse_log()
        self.assertIn("DEAD: 'Some Show S01E01' -> removed 1 queue row(s)", runs[0]["lines"])

    def test_missing_file_returns_empty_list(self):
        data.LOG_PATH = "/tmp/this-log-does-not-exist.log"
        self.assertEqual(data.parse_log(), [])


class JsonStateTests(unittest.TestCase):
    """Covers the design principle every state file in this project relies on:
    atomic write + corrupt-safe load, so a kill mid-write or a truncated file
    can never permanently wedge a script that's meant to run unattended."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)  # start from "file doesn't exist yet"

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_missing_file_loads_as_empty_dict(self):
        self.assertEqual(load_json_state(self.path), {})

    def test_round_trips_through_save_and_load(self):
        save_json_state(self.path, {"hash-abc": {"downloaded": 12345}})
        self.assertEqual(load_json_state(self.path), {"hash-abc": {"downloaded": 12345}})

    def test_corrupt_file_loads_as_empty_dict_not_a_crash(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        self.assertEqual(load_json_state(self.path), {})

    def test_empty_file_loads_as_empty_dict(self):
        with open(self.path, "w") as f:
            f.write("")
        self.assertEqual(load_json_state(self.path), {})


if __name__ == "__main__":
    unittest.main()
