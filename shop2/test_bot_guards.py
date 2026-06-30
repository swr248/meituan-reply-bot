"""单元测试：倒计时解析、URL 守卫、连续相同消息去重 key。

不启动浏览器，纯逻辑测试。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import parse_countdown  # noqa: E402
from state import ReplyState  # noqa: E402

CFG_URL = {
    "monitor": {"url_guard_pattern": "im/page/workbench/reception"},
    "meituan": {"bad_url_pattern": "page/customer"},
}


def t(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as e:
        print(f"FAIL  {name}: {e}")


def test_countdown_s():
    assert parse_countdown("59s") == 59
    assert parse_countdown("18s") == 18
    assert parse_countdown("1s") == 1


def test_countdown_zh():
    assert parse_countdown("59秒") == 59
    assert parse_countdown("18秒") == 18


def test_clock_not_countdown():
    assert parse_countdown("18:47") is None
    assert parse_countdown("17:05") is None


def test_garbage():
    assert parse_countdown("") is None
    assert parse_countdown("hello world") is None


def test_state_distinguishes_same_text():
    """连续两条相同文本应该被区分（不同 index/total）。"""
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        st = ReplyState(d)
        k1 = st.make_key("alice", "你好", 5, 6)
        k2 = st.make_key("alice", "你好", 6, 7)
        assert k1 != k2
        st.mark_replied(k1)
        assert st.already_replied(k1)
        assert not st.already_replied(k2), "第二条相同文本应被视为新消息"


if __name__ == "__main__":
    t("countdown_s", test_countdown_s)
    t("countdown_zh", test_countdown_zh)
    t("clock_not_countdown", test_clock_not_countdown)
    t("garbage", test_garbage)
    t("state_distinguishes_same_text", test_state_distinguishes_same_text)
