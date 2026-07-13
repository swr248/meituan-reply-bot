import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from capture import cookie_capture


class FakeWebSocket:
    def __init__(self, token):
        self.query_params = {'token': token} if token is not None else {}
        self.headers = {}
        self.accept = AsyncMock()
        self.close = AsyncMock()


class VncAuthTests(unittest.TestCase):
    def setUp(self):
        self.original_config = cookie_capture._cfg
        cookie_capture._cfg = {'server': {'auth_token': 'shop1-token', 'legacy_auth_tokens': ['old-shop1-token']}}

    def tearDown(self):
        cookie_capture._cfg = self.original_config

    def test_token_validation_accepts_current_and_legacy_tokens(self):
        self.assertTrue(cookie_capture._token_is_valid('shop1-token'))
        self.assertTrue(cookie_capture._token_is_valid('old-shop1-token'))
        self.assertFalse(cookie_capture._token_is_valid('wrong-token'))

    def test_vnc_websocket_rejects_bad_token_before_rfb_connection(self):
        websocket = FakeWebSocket('wrong-token')
        with patch.object(cookie_capture.asyncio, 'open_connection', new=AsyncMock()) as connect:
            asyncio.run(cookie_capture.vnc_ws(websocket))
        websocket.accept.assert_not_awaited()
        websocket.close.assert_awaited_once_with(code=1008, reason='invalid token')
        connect.assert_not_awaited()

    def test_worker_starts_lazily_and_replaces_previous_role(self):
        original_workers = cookie_capture._workers
        cookie_capture._workers = {}
        try:
            with patch.object(cookie_capture, 'RoleWorker') as worker_type:
                im_worker = worker_type.return_value
                self.assertIs(cookie_capture._get_worker('im'), im_worker)
                promo_worker = object()
                worker_type.return_value = promo_worker
                self.assertIs(cookie_capture._get_worker('promo'), promo_worker)
                im_worker.close.assert_called_once_with()
                self.assertEqual(cookie_capture._workers, {'promo': promo_worker})
        finally:
            cookie_capture._workers = original_workers

    def test_main_config_uses_explicit_path(self):
        original_path = cookie_capture._main_config_path
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.yaml'
                path.write_text('server:\n  auth_token: explicit-token\n', encoding='utf-8')
                cookie_capture._main_config_path = path
                self.assertEqual(cookie_capture._main_config()['server']['auth_token'], 'explicit-token')
        finally:
            cookie_capture._main_config_path = original_path



if __name__ == '__main__':
    unittest.main()
