from unittest.mock import patch

from bot import MeituanBot


class FakePage:
    def __init__(self, url='https://example.invalid/'):
        self.url = url


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def add_cookies(self, _cookies):
        return None

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context

    def new_context(self, **_kwargs):
        return self.context


def make_bot(old, candidate):
    bot = MeituanBot.__new__(MeituanBot)
    bot.cfg = {}
    bot._browser = FakeBrowser(candidate)
    bot._context = old
    bot._page = old.page
    bot._viewport = {}
    bot._current_cookie_mtime = 1
    bot._bootstrap = lambda _page: None
    return bot


def test_invalid_candidate_keeps_old_context():
    old = FakeContext(FakePage())
    candidate = FakeContext(FakePage())
    bot = make_bot(old, candidate)
    with patch('bot.load_cookies', return_value=[{'name': 'x', 'value': '1'}]), patch('bot.url_is_correct', return_value=False):
        try:
            bot._reload_cookie_context(2)
            assert False, 'invalid candidate must fail'
        except RuntimeError:
            pass
    assert bot._context is old
    assert not old.closed
    assert candidate.closed


def test_valid_candidate_commits_before_old_context_closes():
    old = FakeContext(FakePage())
    candidate = FakeContext(FakePage())
    bot = make_bot(old, candidate)
    with patch('bot.load_cookies', return_value=[{'name': 'x', 'value': '1'}]), patch('bot.url_is_correct', return_value=True):
        page = bot._reload_cookie_context(2)
    assert page is candidate.page
    assert bot._context is candidate
    assert old.closed
    assert bot._current_cookie_mtime == 2


if __name__ == '__main__':
    test_invalid_candidate_keeps_old_context()
    test_valid_candidate_commits_before_old_context_closes()
    print('PASS cookie_reload')
