"""单元测试：关键词匹配 + AI 提示自动追加。

不需要浏览器，跑得快。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rules import append_ai_hint, decide_reply  # noqa: E402

CFG = {
    "replies": {
        "first_message": "您好，请问有什么可以帮到您？",
        "fallback": "您好，您的消息已收到。",
        "rules": [
            {
                "name": "cigarettes",
                "keywords": ["烟", "香烟", "yan"],
                "reply": "本店不售卖香烟。",
            },
            {
                "name": "stock",
                "keywords": ["还有没有", "有货"],
                "reply": "库存以页面为准。",
            },
        ],
    }
}


def t(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as e:
        print(f"FAIL  {name}: {e}")


def test_append_ai():
    text, appended = append_ai_hint("你好")
    assert appended is True
    assert "AI" in text or "自动回复" in text

    text2, appended2 = append_ai_hint("本店AI自动回复：稍等")
    assert appended2 is False
    assert text2 == "本店AI自动回复：稍等"

    text3, _ = append_ai_hint("请问你们几点关门？")
    assert "AI" in text3 or "自动回复" in text3 or "本消息由" in text3


def test_cigarettes():
    d = decide_reply("有香烟吗", CFG, False)
    assert d.rule == "cigarettes", d.rule
    assert "AI" in d.reply, d.reply


def test_stock():
    d = decide_reply("还有没有可乐", CFG, False)
    assert d.rule == "stock", d.rule


def test_fallback():
    d = decide_reply("完全没听过的关键词xyz", CFG, False)
    assert d.rule == "fallback", d.rule


def test_first():
    d = decide_reply("你好", CFG, True)
    assert d.rule == "first_message", d.rule


def test_case_insensitive():
    d = decide_reply("YAN 香烟", CFG, False)
    assert d.rule == "cigarettes"


if __name__ == "__main__":
    t("append_ai_hint", test_append_ai)
    t("cigarettes", test_cigarettes)
    t("stock", test_stock)
    t("fallback", test_fallback)
    t("first_message", test_first)
    t("case_insensitive", test_case_insensitive)
