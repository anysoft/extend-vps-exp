import unittest
from datetime import datetime

from renewal_timing import JST, renewal_window_after_success, should_attempt_login_from_state


class ShouldAttemptLoginFromStateTests(unittest.TestCase):
    def setUp(self):
        self.state = {'next_expiry_date': '2026-07-27'}

    def at(self, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 7, day, hour, minute, tzinfo=JST)

    def test_does_not_login_on_previous_day_after_a_successful_renewal(self):
        self.assertFalse(should_attempt_login_from_state(self.state, self.at(26, 18, 34)))

    def test_does_not_login_before_twelve_hour_window(self):
        self.assertFalse(should_attempt_login_from_state(self.state, self.at(27, 11, 59)))

    def test_logs_in_at_start_of_twelve_hour_window(self):
        self.assertTrue(should_attempt_login_from_state(self.state, self.at(27, 12)))

    def test_logs_in_during_twelve_hour_window(self):
        self.assertTrue(should_attempt_login_from_state(self.state, self.at(27, 23, 59)))

    def test_does_not_login_after_expiry(self):
        self.assertFalse(should_attempt_login_from_state(self.state, self.at(28, 0)))

    def test_missing_or_invalid_state_still_logs_in(self):
        self.assertTrue(should_attempt_login_from_state({}, self.at(26, 18)))
        self.assertTrue(
            should_attempt_login_from_state({'next_expiry_date': 'invalid'}, self.at(26, 18))
        )

    def test_precise_window_recorded_after_renewal_takes_priority(self):
        state = {
            'next_expiry_date': '2026-07-27',
            'renewal_opens_at_jst': '2026-07-27T06:34:00+09:00',
            'estimated_expiry_at_jst': '2026-07-27T18:34:00+09:00',
        }

        self.assertFalse(should_attempt_login_from_state(state, self.at(27, 6, 33)))
        self.assertTrue(should_attempt_login_from_state(state, self.at(27, 6, 34)))
        self.assertTrue(should_attempt_login_from_state(state, self.at(27, 18, 33)))
        self.assertFalse(should_attempt_login_from_state(state, self.at(27, 18, 34)))

    def test_successful_renewal_creates_twelve_to_twenty_four_hour_window(self):
        opens_at, expires_at = renewal_window_after_success(self.at(26, 18, 34))

        self.assertEqual(opens_at, self.at(27, 6, 34))
        self.assertEqual(expires_at, self.at(27, 18, 34))


if __name__ == '__main__':
    unittest.main()
