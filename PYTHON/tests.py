#!/usr/bin/env python3
# Stdlib-only tests (no pip deps, matching the rest of this project) covering
# the pieces of the dashboard that are pure logic -- no live Sonarr/Radarr/
# qBittorrent needed to run these. Run with: python3 tests.py

import importlib.util
import os
import tempfile
import unittest
import urllib.request
from datetime import datetime

from arr_common.state import load_json_state, save_json_state
from arr_dashboard import data


def _load_queue_cleaner():
    """arr-queue-cleaner.py is a hyphenated script rather than an importable
    module name, so load it by path to test its logic directly."""
    for var in ("SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY"):
        os.environ.setdefault(var, "test")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arr-queue-cleaner.py")
    spec = importlib.util.spec_from_file_location("queue_cleaner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


queue_cleaner = _load_queue_cleaner()


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


class RadarrRescueImportTests(unittest.TestCase):
    """Regression cover for the import loop found live: Radarr's manual-import
    scan returns a movie object whose hasFile is always null, so reading it
    directly made every completed download look like it still needed importing.
    The rescue then re-imported the same file every 30 minutes, deleting and
    re-copying an already-imported movie each time. hasFile must come from the
    real library record instead."""

    SCAN_MOVIE = {"id": 45, "title": "Some Movie", "hasFile": None}  # as Radarr really returns it

    def setUp(self):
        fd, self.ledger = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.ledger)
        self.submitted = []

        self._originals = {
            name: getattr(queue_cleaner, name)
            for name in (
                "INSTANCES", "REVIEW_LEDGER_PATH", "qbit_login", "qbit_get",
                "already_imported", "manualimport_scan", "radarr_movies",
                "submit_manual_import", "clear_stale_queue_entries",
            )
        }

        queue_cleaner.INSTANCES = [("Radarr", "http://radarr", "key")]
        queue_cleaner.REVIEW_LEDGER_PATH = self.ledger
        queue_cleaner.qbit_login = lambda: "cookie"
        queue_cleaner.qbit_get = lambda path, cookie: [{
            "hash": "abc123",
            "name": "Some.Movie.2018.HDRip",
            "progress": 1.0,
            "content_path": "/media/downloads/Some.Movie.2018.HDRip",
        }]
        # The rescue's own past imports are recorded by Radarr with a null
        # downloadId, so this guard can never recognise them -- exactly the
        # condition under which the loop ran.
        queue_cleaner.already_imported = lambda base, key, name, target_hash: False
        queue_cleaner.manualimport_scan = lambda base, key, folder: [{
            "path": "/media/downloads/Some.Movie.2018.HDRip/Some.Movie.2018.HDRip.avi",
            "relativePath": "Some.Movie.2018.HDRip.avi",
            "size": 1317934612,
            "rejections": [],
            "movie": dict(self.SCAN_MOVIE),
            "quality": {}, "languages": [],
        }]
        queue_cleaner.clear_stale_queue_entries = lambda base, key, target_hash: None
        queue_cleaner.submit_manual_import = self._record_import

        # The redundant-duplicate cleanup deletes straight through urllib, so
        # capture that rather than letting a test reach the network.
        self.deleted = []
        self._real_urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._record_delete

    def _record_import(self, base, key, files, import_mode="copy"):
        self.submitted.append(files)
        return True

    def _record_delete(self, request, timeout=None):
        self.deleted.append(request.full_url)
        return None  # the caller ignores the response

    def tearDown(self):
        urllib.request.urlopen = self._real_urlopen
        for name, value in self._originals.items():
            setattr(queue_cleaner, name, value)
        if os.path.exists(self.ledger):
            os.unlink(self.ledger)

    @staticmethod
    def _library(has_file=True, relative_path="Some.Movie.2018.HDRip.avi", size=1317934612):
        movie = {"id": 45, "title": "Some Movie", "hasFile": has_file}
        if has_file:
            movie["movieFile"] = {"relativePath": relative_path, "size": size}
        return [movie]

    def test_does_not_reimport_a_movie_the_library_already_has(self):
        queue_cleaner.radarr_movies = lambda base, key: self._library()
        result = queue_cleaner.rescue_stuck_imports()
        self.assertEqual(self.submitted, [])
        self.assertNotIn("imported", result)

    def test_removes_the_download_once_it_is_the_file_already_imported(self):
        queue_cleaner.radarr_movies = lambda base, key: self._library()
        result = queue_cleaner.rescue_stuck_imports()
        self.assertEqual(len(self.deleted), 1)
        self.assertIn("torrents/delete", self.deleted[0])
        self.assertIn("REDUNDANT", result)

    def test_leaves_a_differently_sized_download_for_a_human(self):
        """Same filename but a different size is a different release -- possibly
        a real upgrade that failed to import, so it must never be auto-deleted."""
        queue_cleaner.radarr_movies = lambda base, key: self._library(size=999)
        result = queue_cleaner.rescue_stuck_imports()
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.submitted, [])
        self.assertIn("NEEDS MANUAL REVIEW", result)

    def test_leaves_a_differently_named_download_for_a_human(self):
        queue_cleaner.radarr_movies = lambda base, key: self._library(relative_path="Some.Movie.2018.BluRay.mkv")
        result = queue_cleaner.rescue_stuck_imports()
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.submitted, [])
        self.assertIn("NEEDS MANUAL REVIEW", result)

    def test_still_imports_a_movie_that_is_genuinely_missing(self):
        queue_cleaner.radarr_movies = lambda base, key: self._library(has_file=False)
        result = queue_cleaner.rescue_stuck_imports()
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(self.submitted[0][0]["movieId"], 45)
        self.assertIn("imported 1 file(s)", result)

    def test_unknown_library_movie_goes_to_review_rather_than_importing(self):
        queue_cleaner.radarr_movies = lambda base, key: []
        result = queue_cleaner.rescue_stuck_imports()
        self.assertEqual(self.submitted, [])
        self.assertIn("NEEDS MANUAL REVIEW", result)


if __name__ == "__main__":
    unittest.main()
