import os
import unittest
from unittest.mock import patch

from pricebot.config import Settings


class ConfigTests(unittest.TestCase):
    def settings(self, values=None, **kwargs):
        with patch.dict(os.environ, values or {}, clear=True):
            return Settings.from_env(**kwargs)

    def test_token_missing_for_bot_but_not_diagnostics(self):
        with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
            self.settings()
        self.assertEqual(self.settings(require_token=False).bot_token, "")

    def test_secret_is_not_in_dataclass_repr(self):
        settings = self.settings({"BOT_TOKEN": "12345:private-test-token"})
        self.assertNotIn("private-test-token", repr(settings))
        self.assertEqual(settings.bot_token, "12345:private-test-token")

    def test_defaults_are_lipetsk_with_unconfirmed_wb_destination(self):
        settings = self.settings(require_token=False)
        self.assertEqual(settings.city, "Липецк")
        self.assertEqual(settings.avito_city, "lipetsk")
        self.assertEqual(settings.wb_latitude, 52.6088)
        self.assertEqual(settings.wb_longitude, 39.5992)
        self.assertIsNone(settings.wb_dest)

    def test_allowed_users_parsed_and_invalid_names_rejected(self):
        settings = self.settings({"ALLOWED_USER_IDS": " 123,456,123, "}, require_token=False)
        self.assertEqual(settings.allowed_user_ids, frozenset({123, 456}))
        with self.assertRaisesRegex(ValueError, "ALLOWED_USER_IDS"):
            self.settings({"ALLOWED_USER_IDS": "123,user"}, require_token=False)

    def test_timeout_cache_and_cooldown_require_finite_positive_numbers(self):
        for name, maximum in (("SOURCE_TIMEOUT", 120), ("CACHE_TTL", 3600), ("USER_COOLDOWN", 3600)):
            for value in ("0", "-1", "NaN", "Infinity", "abc", str(maximum + 1)):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.settings({name: value}, require_token=False)
            self.assertEqual(getattr(self.settings({name: "0.5"}, require_token=False), name.lower()), 0.5)

    def test_count_settings_reject_fractional_and_invalid_values(self):
        for name, maximum in (("MAX_CONCURRENT_SEARCHES", 10), ("MAX_CACHE_ENTRIES", 10000)):
            for value in ("0", "-1", "0.5", "2.9", "NaN", "Infinity", "abc", str(maximum + 1)):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.settings({name: value}, require_token=False)
            self.assertEqual(getattr(self.settings({name: "1"}, require_token=False), name.lower()), 1)

    def test_coordinates_reject_invalid_or_nonfinite_input(self):
        for name, values in (("WB_LATITUDE", ["91", "-91", "NaN", "Infinity", "bad"]), ("WB_LONGITUDE", ["181", "-181", "NaN", "Infinity", "bad"])):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.settings({name: value}, require_token=False)

    def test_custom_location_and_browser_override(self):
        settings = self.settings({"CITY": "Тула", "AVITO_CITY": "tula", "BROWSER_EXECUTABLE": "/custom/chromium", "WB_DEST": "-12345"}, require_token=False)
        self.assertEqual(settings.city, "Тула")
        self.assertEqual(settings.avito_city, "tula")
        self.assertEqual(settings.browser_executable, "/custom/chromium")
        self.assertEqual(settings.wb_dest, "-12345")
