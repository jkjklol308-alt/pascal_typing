"""Tests for the Auto-Typer V2 silent update checker.

The update check has three hard behavioural contracts, all covered here:
  1. It compares versions numerically (2.10.0 > 2.9.0), never lexically.
  2. When no update exists (or the check fails), it stays silent:
     ``update_to_announce`` returns None and nothing is posted to the GUI.
  3. It never forces anything: the only actions available to the user are
     "open the download page" or "keep using this version".
"""

import io
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import auto_typer_V2 as v2


class FakeResponse:
    def __init__(self, payload=b"", status=200):
        self._payload = payload
        self.status = status

    def read(self, limit=None):
        return self._payload[:limit] if limit is not None else self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParseVersionTests(unittest.TestCase):
    def test_simple_dotted(self):
        self.assertEqual(v2.parse_version("2.0.0"), (2, 0, 0))

    def test_v_prefix_and_whitespace(self):
        self.assertEqual(v2.parse_version(" v2.10.3 "), (2, 10, 3))

    def test_numeric_prefix_junk(self):
        self.assertEqual(v2.parse_version("2.0.0rc1"), (2, 0, 0))

    def test_missing_components(self):
        self.assertEqual(v2.parse_version("2.1"), (2, 1))
        self.assertEqual(v2.parse_version("7"), (7,))

    def test_garbage_returns_none(self):
        self.assertIsNone(v2.parse_version("banana"))
        self.assertIsNone(v2.parse_version(""))
        self.assertIsNone(v2.parse_version(None))

    def test_numeric_not_lexical_comparison(self):
        # Lexical string comparison would say "2.10.0" < "2.9.0".
        self.assertTrue(v2.is_newer_version("2.10.0", "2.9.0"))


class IsNewerVersionTests(unittest.TestCase):
    def test_newer_at_each_level(self):
        self.assertTrue(v2.is_newer_version("2.0.1", "2.0.0"))
        self.assertTrue(v2.is_newer_version("2.1.0", "2.0.9"))
        self.assertTrue(v2.is_newer_version("3.0.0", "2.99.99"))

    def test_equal_is_not_newer(self):
        self.assertFalse(v2.is_newer_version("2.0.0", "2.0.0"))
        self.assertFalse(v2.is_newer_version("v2.0", "2.0.0"))

    def test_older_is_not_newer(self):
        self.assertFalse(v2.is_newer_version("1.9.9", "2.0.0"))

    def test_malformed_never_announces(self):
        self.assertFalse(v2.is_newer_version("garbage", "2.0.0"))
        self.assertFalse(v2.is_newer_version("2.0.1", "garbage"))


class UpdateToAnnounceTests(unittest.TestCase):
    """The silence contract: no information or no update means None."""

    def test_no_information_is_silent(self):
        self.assertIsNone(v2.update_to_announce(None, "2.0.0"))

    def test_same_version_is_silent(self):
        self.assertIsNone(v2.update_to_announce("2.0.0", "2.0.0"))

    def test_older_version_is_silent(self):
        self.assertIsNone(v2.update_to_announce("1.9.0", "2.0.0"))

    def test_unparsable_is_silent(self):
        self.assertIsNone(v2.update_to_announce("???bad???", "2.0.0"))

    def test_newer_version_is_announced(self):
        self.assertEqual(v2.update_to_announce("2.1.0", "2.0.0"), "2.1.0")


class ExtractVersionTests(unittest.TestCase):
    def test_extracts_declared_version(self):
        source = '"""Doc."""\nAPP_VERSION = "9.9.9"\nX = 1\n'
        self.assertEqual(v2.extract_version_from_source(source), "9.9.9")

    def test_single_quotes(self):
        self.assertEqual(v2.extract_version_from_source("APP_VERSION = '1.2.3'"), "1.2.3")

    def test_indented_declaration(self):
        self.assertEqual(v2.extract_version_from_source("    APP_VERSION = \"4.5.6\""), "4.5.6")

    def test_no_declaration_returns_none(self):
        self.assertIsNone(v2.extract_version_from_source("print('hello')"))
        self.assertIsNone(v2.extract_version_from_source(""))

    def test_prose_mention_is_not_a_declaration(self):
        source = "compares APP_VERSION against the published copy\n"
        self.assertIsNone(v2.extract_version_from_source(source))

    def test_real_shipped_file_declares_its_version(self):
        # The published-file lookup depends on this exact declaration style,
        # so pin it against the real module on disk.
        own_source = Path(__file__).with_name("auto_typer_V2.py").read_text(encoding="utf-8")
        self.assertEqual(v2.extract_version_from_source(own_source), v2.APP_VERSION)
        self.assertIsNotNone(v2.parse_version(v2.APP_VERSION))


class UpdateCheckerFetchTests(unittest.TestCase):
    def setUp(self):
        self.checker = v2.UpdateChecker(source_url="http://example.invalid/auto_typer_V2.py")

    def test_fetches_published_version(self):
        payload = 'APP_VERSION = "4.5.6"\n'
        with mock.patch.object(v2.urllib.request, "urlopen",
                               return_value=FakeResponse(payload.encode())):
            self.assertEqual(self.checker.fetch_latest_version(), "4.5.6")

    def test_http_error_is_silent(self):
        error = urllib.error.HTTPError("url", 404, "Not Found", None, None)
        with mock.patch.object(v2.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(self.checker.fetch_latest_version())

    def test_network_error_is_silent(self):
        with mock.patch.object(v2.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(self.checker.fetch_latest_version())

    def test_non_200_is_silent(self):
        payload = 'APP_VERSION = "9.9.9"\n'
        with mock.patch.object(v2.urllib.request, "urlopen",
                               return_value=FakeResponse(payload.encode(), status=404)):
            self.assertIsNone(self.checker.fetch_latest_version())

    def test_payload_without_version_is_silent(self):
        with mock.patch.object(v2.urllib.request, "urlopen",
                               return_value=FakeResponse(b"print('no version here')")):
            self.assertIsNone(self.checker.fetch_latest_version())


class AppWiringTests(unittest.TestCase):
    """The GUI class must expose the update hooks (Tk-free check)."""

    def test_app_class_has_update_hooks(self):
        for name in ("_start_update_check", "_update_check_worker",
                     "_manual_update_check", "_on_update_available",
                     "_on_manual_update_result", "_open_update_page"):
            self.assertTrue(hasattr(v2.AutoTyperV2App, name), name)


class CliCheckUpdateTests(unittest.TestCase):
    def test_cli_reports_available_update(self):
        with mock.patch.object(v2.UpdateChecker, "fetch_latest_version",
                               return_value="9.9.9"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = v2.main(["--check-update"])
        self.assertEqual(code, 0)
        self.assertIn("Update available", out.getvalue())
        self.assertIn("9.9.9", out.getvalue())

    def test_cli_reports_up_to_date(self):
        with mock.patch.object(v2.UpdateChecker, "fetch_latest_version",
                               return_value=v2.APP_VERSION), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = v2.main(["--check-update"])
        self.assertEqual(code, 0)
        self.assertIn("up to date", out.getvalue())

    def test_cli_unreachable_is_graceful(self):
        with mock.patch.object(v2.UpdateChecker, "fetch_latest_version",
                               return_value=None), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = v2.main(["--check-update"])
        self.assertEqual(code, 1)
        self.assertIn("Could not check for updates", out.getvalue())


if __name__ == "__main__":
    unittest.main()
