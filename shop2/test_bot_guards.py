"""单元测试：倒计时解析、URL 守卫、连续相同消息去重 key。

不启动浏览器，纯逻辑测试。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import (  # noqa: E402
    MeituanBot,
    conversation_card_is_candidate,
    inbound_message_fingerprint,
    parse_countdown,
    should_rebuild_browser,
)
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
        raise


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


def test_idle_browser_rebuild_thresholds():
    assert should_rebuild_browser(100, 0, 10800, 180, 100)
    assert not should_rebuild_browser(99, 0, 10800, 180, 100)
    assert not should_rebuild_browser(100, 0, 10799, 180, 100)


def test_state_distinguishes_same_text():
    """连续两条相同文本应该被区分（不同 index/total）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        st = ReplyState(d)
        k1 = st.make_key("alice", "你好", 5, 6)
        k2 = st.make_key("alice", "你好", 6, 7)
        assert k1 != k2
        st.mark_replied(k1)
        assert st.already_replied(k1)
        assert not st.already_replied(k2), "第二条相同文本应被视为新消息"


def test_inbound_fingerprint_tracks_message_instance():
    first = inbound_message_fingerprint("v**\n你好", 5, 4)
    same_scan = inbound_message_fingerprint("v**\n你好", 5, 4)
    repeated_text_new_bubble = inbound_message_fingerprint("v**\n你好", 6, 5)
    assert first == same_scan
    assert first != repeated_text_new_bubble


def test_order_card_with_timeout_is_candidate():
    card_text = "\u4eca\u65e5#16\u5355\n\u54ce**\n17:05\n\u66f4\u6539\u5730\u5740\n\u8d85\u65f6\u672a\u56de\u590d"
    assert conversation_card_is_candidate(card_text)


def test_today_order_card_extracts_masked_customer():
    card_text = "\u5f85\u56de\u590d 1\n\u4eca\u65e5#16\u5355 \u54ce**\n17:05\n\u66f4\u6539\u5730\u5740\n\u8d85\u65f6\u672a\u56de\u590d"
    assert MeituanBot._extract_name_from_text(card_text) == "\u54ce**"


if __name__ == "__main__":
    t("countdown_s", test_countdown_s)
    t("countdown_zh", test_countdown_zh)
    t("clock_not_countdown", test_clock_not_countdown)
    t("garbage", test_garbage)
    t("idle_browser_rebuild_thresholds", test_idle_browser_rebuild_thresholds)
    t("state_distinguishes_same_text", test_state_distinguishes_same_text)
    t("inbound_fingerprint_tracks_message_instance", test_inbound_fingerprint_tracks_message_instance)
    t("order_card_with_timeout_is_candidate", test_order_card_with_timeout_is_candidate)
    t("today_order_card_extracts_masked_customer", test_today_order_card_extracts_masked_customer)
