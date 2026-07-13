import unittest
from unittest.mock import patch

from fastapi import HTTPException

import admin


class PromoUpdateTests(unittest.TestCase):
    def test_enabled_only_update_preserves_windows(self):
        schedule = {'enabled': True, 'windows': [{'start': '06:00', 'end': '10:00'}]}
        admin._apply_promo_update(schedule, False, None)
        self.assertFalse(schedule['enabled'])
        self.assertEqual(schedule['windows'], [{'start': '06:00', 'end': '10:00'}])

    def test_explicit_empty_windows_clears_windows(self):
        schedule = {'enabled': True, 'windows': [{'start': '06:00', 'end': '10:00'}]}
        admin._apply_promo_update(schedule, None, [])
        self.assertEqual(schedule['windows'], [])


class BusinessHealthTests(unittest.TestCase):
    def test_no_permission_overrides_fresh_active_process(self):
        health = admin._classify_business_health({'timestamp': 1000, 'url': 'https://x/#/noPermission'}, 1050)
        self.assertEqual(health['status'], 'no_permission')
        self.assertFalse(health['ok'])

    def test_stale_session_is_not_healthy(self):
        health = admin._classify_business_health({'timestamp': 1000, 'url': 'https://x/imworkbench/home'}, 1121)
        self.assertEqual(health['status'], 'stale')
        self.assertFalse(health['ok'])

    def test_fresh_workbench_session_is_healthy(self):
        health = admin._classify_business_health({'timestamp': 1000, 'url': 'https://x/imworkbench/home'}, 1050)
        self.assertEqual(health['status'], 'healthy')
        self.assertTrue(health['ok'])


class CaptureOwnershipTests(unittest.TestCase):
    def test_start_rejects_when_scheduler_owns_global_lock(self):
        with patch.object(admin, '_check_token'), patch.object(admin, '_manual_capture_owned', return_value=False), patch.object(admin, '_try_acquire_capture_owner', return_value=False):
            with self.assertRaises(HTTPException) as raised:
                admin.api_capture('start', token='session')
        self.assertEqual(raised.exception.status_code, 409)

    def test_stop_rejects_capture_not_owned_by_manual_session(self):
        with patch.object(admin, '_check_token'), patch.object(admin, '_manual_capture_owned', return_value=False):
            with self.assertRaises(HTTPException) as raised:
                admin.api_capture('stop', token='session')
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
