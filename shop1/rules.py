"""关键词匹配和 AI 提示自动追加。

机器人会自动给没有 AI 提示的回复追加：
（本消息由店铺AI自动回复，如有急事请直接拨打门店电话联系。）

如果回复里已经包含这些词，就不重复追加：
AI / 自动回复 / 机器人 / 智能客服 / 店铺AI / AI会话 / 本消息由
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

AI_SUFFIX = "（本消息由店铺AI自动回复，如有急事请直接拨打门店电话联系。）"
AI_HINT_TOKENS = ["AI", "自动回复", "机器人", "智能客服", "店铺AI", "AI会话", "本消息由"]


@dataclass
class ReplyDecision:
    rule: str
    reply: str
    auto_appended_ai: bool


def _has_ai_hint(text: str) -> bool:
    return any(tok in text for tok in AI_HINT_TOKENS)


def append_ai_hint(text: str) -> tuple[str, bool]:
    if _has_ai_hint(text):
        return text, False
    if not text.endswith(("。", "！", "？", ".", "!", "?")):
        return text + AI_SUFFIX, True
    return text + AI_SUFFIX, True


def decide_reply(message: str, cfg: Dict[str, Any], is_first_message: bool) -> ReplyDecision:
    """根据消息和配置决定回复。

    - 关键词规则优先（按声明顺序匹配）
    - 没有命中关键词，且是首条：first_message
    - 没有命中关键词，且非首条：fallback
    - 自动给没有 AI 提示的回复追加 AI 提示
    """
    replies_cfg = (cfg.get("replies", {}) or {})
    text = (message or "").strip()

    for rule in (replies_cfg.get("rules", []) or []):
        kws: List[str] = rule.get("keywords", []) or []
        if not kws:
            continue
        if any(_match_keyword(text, kw) for kw in kws):
            reply = rule.get("reply", "")
            reply, appended = append_ai_hint(reply)
            return ReplyDecision(rule=rule.get("name", "unnamed"), reply=reply, auto_appended_ai=appended)

    if is_first_message:
        base = replies_cfg.get("first_message", "您好，请问有什么可以帮到您？")
        reply, appended = append_ai_hint(base)
        return ReplyDecision(rule="first_message", reply=reply, auto_appended_ai=appended)

    base = replies_cfg.get("fallback", "您好，您的消息已收到，店铺会尽快为您处理。")
    reply, appended = append_ai_hint(base)
    return ReplyDecision(rule="fallback", reply=reply, auto_appended_ai=appended)


def _match_keyword(text: str, kw: str) -> bool:
    """大小写不敏感、忽略空白地匹配关键词。"""
    if not kw:
        return False
    t = re.sub(r"\s+", "", (text or "")).lower()
    k = re.sub(r"\s+", "", kw).lower()
    return k in t
