import unittest
from datetime import date, datetime

from renewal_timing import JST, is_in_renewal_window, should_attempt_login_from_state


class RenewalWindowTests(unittest.TestCase):
    def setUp(self):
        self.expiry_date = date(2026, 7, 27)

    def at(self, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 7, day, hour, minute, tzinfo=JST)

    def test_is_outside_window_on_previous_day(self):
        self.assertFalse(is_in_renewal_window(self.expiry_date, self.at(26, 18, 34)))

    def test_is_outside_window_before_final_twelve_hours(self):
        self.assertFalse(is_in_renewal_window(self.expiry_date, self.at(27, 11, 59)))

    def test_is_in_window_at_start_of_final_twelve_hours(self):
        self.assertTrue(is_in_renewal_window(self.expiry_date, self.at(27, 12)))

    def test_is_in_window_during_final_twelve_hours(self):
        self.assertTrue(is_in_renewal_window(self.expiry_date, self.at(27, 23, 59)))

    def test_is_outside_window_after_expiry(self):
        self.assertFalse(is_in_renewal_window(self.expiry_date, self.at(28, 0)))

    def test_legacy_inferred_timestamps_are_ignored(self):
        state = {
            'next_expiry_date': '2026-07-27',
            'renewal_opens_at_jst': '2026-07-27T06:34:00+09:00',
            'estimated_expiry_at_jst': '2026-07-27T18:34:00+09:00',
        }

        self.assertFalse(is_in_renewal_window(self.expiry_date, self.at(27, 6, 34), state))
        self.assertFalse(is_in_renewal_window(self.expiry_date, self.at(27, 11, 59), state))
        self.assertTrue(is_in_renewal_window(self.expiry_date, self.at(27, 12, 0), state))


class CachedExpiryLoginDecisionTests(unittest.TestCase):
    def at(self, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=JST)

    def test_stale_previous_day_expiry_forces_login_and_refresh(self):
        state = {'next_expiry_date': '2026-09-14'}

        self.assertTrue(should_attempt_login_from_state(state, self.at(15, 12, 18)))

    def test_current_expiry_in_renewal_window_logs_in(self):
        state = {'next_expiry_date': '2026-09-15'}

        self.assertTrue(should_attempt_login_from_state(state, self.at(15, 12, 18)))

    def test_next_day_expiry_after_successful_renewal_skips_login(self):
        state = {'next_expiry_date': '2026-09-16'}

        self.assertFalse(should_attempt_login_from_state(state, self.at(15, 12, 18)))

    def test_current_expiry_before_renewal_window_skips_login(self):
        state = {'next_expiry_date': '2026-09-15'}

        self.assertFalse(should_attempt_login_from_state(state, self.at(15, 11, 59)))

    def test_missing_or_invalid_cache_forces_login(self):
        self.assertTrue(should_attempt_login_from_state({}, self.at(15, 12, 18)))
        self.assertTrue(
            should_attempt_login_from_state(
                {'next_expiry_date': 'not-a-date'},
                self.at(15, 12, 18),
            )
        )


if __name__ == '__main__':
    unittest.main()
