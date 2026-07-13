import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from cookie_sync import write_cookie_state


def _state(export_time: float, value: str) -> dict:
    return {
        'export_time': export_time,
        'export_time_str': str(export_time),
        'cookie_count': 1,
        'cookies': [{'name': 'token', 'value': value, 'domain': '.meituan.com', 'path': '/'}],
    }


def test_atomic_writer_rejects_older_cookie_state():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'cookies.json'
        assert write_cookie_state(path, _state(200, 'new'))
        assert not write_cookie_state(path, _state(100, 'old'))
        assert json.loads(path.read_text(encoding='utf-8'))['cookies'][0]['value'] == 'new'


def test_atomic_writer_survives_concurrent_writers_and_sets_private_mode():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'cookies.json'
        threads = [threading.Thread(target=write_cookie_state, args=(path, _state(i, str(i)))) for i in range(1, 41)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        data = json.loads(path.read_text(encoding='utf-8'))
        assert data['export_time'] == 40
        assert data['cookies'][0]['value'] == '40'
        if os.name != 'nt':
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
