import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from promo_scheduler import CaptureLease, capture_available_for_scheduler, in_any_window, reconcile_interval_sec, safe_url_for_log, should_reconcile


def test_regular_window_opens_at_start_and_closes_at_end():
    windows = [{"start": "06:00", "end": "10:00"}]
    assert in_any_window(6 * 60, windows)
    assert in_any_window(9 * 60 + 59, windows)
    assert not in_any_window(10 * 60, windows)


def test_overnight_window_stays_open_across_midnight():
    windows = [{"start": "17:00", "end": "02:30"}]
    assert in_any_window(17 * 60, windows)
    assert in_any_window(60, windows)
    assert not in_any_window(2 * 60 + 30, windows)


def test_reconciles_periodically_when_desired_state_is_unchanged():
    assert not should_reconcile('on', 'on', 100.0, 699.0, 600)
    assert should_reconcile('on', 'on', 100.0, 700.0, 600)
    assert should_reconcile('off', 'on', 699.0, 700.0, 600)


def test_reconcile_interval_defaults_to_hourly_and_is_bounded():
    assert reconcile_interval_sec({}) == 3600
    assert reconcile_interval_sec({'reconcile_interval_sec': 60}) == 900
    assert reconcile_interval_sec({'reconcile_interval_sec': 99999}) == 21600


def test_safe_url_for_log_removes_credentials_and_keeps_route():
    value = 'https://example.test/ad/rpc?token=secret&acctId=123#/subapp/onestop/index?bsid=hidden'
    sanitized = safe_url_for_log(value)
    assert sanitized == 'https://example.test/ad/rpc#/subapp/onestop/index'
    assert 'secret' not in sanitized
    assert '123' not in sanitized
    assert 'hidden' not in sanitized


def test_scheduler_does_not_take_over_external_capture():
    active = type('Result', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()
    inactive = type('Result', (), {'returncode': 3, 'stderr': '', 'stdout': ''})()
    with patch('promo_scheduler._run_systemctl', side_effect=[inactive, active]):
        assert not capture_available_for_scheduler('meituan-capture-meituan-reply-bot.service')


def test_scheduler_retries_immediately_when_manual_owner_holds_lock():
    handle = MagicMock()
    handle.fileno.return_value = 9
    fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4)
    fake_fcntl.flock = MagicMock(side_effect=[BlockingIOError(), None])
    lease = CaptureLease('capture.service', 'shop1', MagicMock())
    with patch.dict(sys.modules, {'fcntl': fake_fcntl}), patch('builtins.open', return_value=handle), patch('promo_scheduler._run_systemctl') as systemctl:
        try:
            lease.__enter__()
            assert False, 'expected capture lease conflict'
        except RuntimeError as exc:
            assert 'capture lease busy' in str(exc)
    systemctl.assert_not_called()
    handle.close.assert_called_once()


if __name__ == "__main__":
    test_regular_window_opens_at_start_and_closes_at_end()
    test_overnight_window_stays_open_across_midnight()
    test_reconciles_periodically_when_desired_state_is_unchanged()
    print("PASS  promo_scheduler")
