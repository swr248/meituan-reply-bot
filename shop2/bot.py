"""美团 IM 自动回复机器人。

关键防护（针对"崩溃后无法重启到正确监控画面"问题）：
- 启动守卫：bot 启动后强制 goto(chat_url)
- URL 白名单守卫：每次扫描前检查，不在白名单就强制导航
- 启动前清理：杀掉残留 chromium 进程，避免 profile 锁
- 自检：定期检查 URL 健康
"""
from __future__ import annotations

import argparse
import json
import os
import logging
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from browser_common import (
    build_launch_options,
    ensure_profile_dir,
    load_config,
    log,
    navigate_chat_url,
    pre_start_cleanup,
    url_is_bad,
    url_is_correct,
)
from rules import decide_reply
from state import ReplyState
from cookie_sync import load_cookies, cookie_file_exists, cookie_file_path

# 把这个 logger 调到 DEBUG，selector 匹配过程可见
log.setLevel(logging.INFO)


# 倒计时正则：1s/59s/1秒/59秒
COUNTDOWN_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:s|sec|secs|second|seconds|秒)(?!\d)", re.IGNORECASE)
# 普通时间不算倒计时：18:47 / 17:05
CLOCK_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*$")
MASKED_NAME_RE = re.compile(r"[A-Za-z][*??]{1,4}")


def parse_countdown(text: str) -> Optional[int]:
    """从文本里解析出"剩余秒数"。返回 None 表示不是倒计时。"""
    s = (text or "").replace("\xa0", " ").strip()
    if CLOCK_RE.match(s):
        return None
    m = COUNTDOWN_RE.search(s)
    if not m:
        return None
    val = int(m.group(1))
    if val is None:
        return None
    if not (0 <= val <= 59):
        return None
    return val



SELF_AI_PHRASES = (
    "\u60a8\u597d\uff0c\u8bf7\u95ee\u6709\u4ec0\u4e48\u53ef\u4ee5\u5e2e\u5230\u60a8",  # \u60a8\u597d\uff0c\u8bf7\u95ee\u6709\u4ec0\u4e48\u53ef\u4ee5\u5e2e\u5230\u60a8
    "\u4ee5\u4e0a\u4fe1\u606f\u662f\u5e97\u94faAI\u4f1a\u8bdd",
    "\u672c\u6d88\u606f\u7531\u5e97\u94fa",
    "\u5e97\u94faAI\u81ea\u52a8\u56de\u590d",
    "\u5df2\u8bfb",
    "\u672a\u8bfb",
    "\u5df2\u56de\u590d",
    "\u5df2\u7ecf\u5230\u5e95\u5566",
    "\u5168\u5bb6\u4fbf\u5229\u5e97",
)

def is_self_ai_reply(text: str) -> bool:
    """\u8bc6\u522b\u5e97\u94fa\u81ea\u5bb6AI\u521a\u53d1\u7684\u9884\u8bbe\u8bdd\u672f\uff0c\u4e0d\u89c6\u4e3a\u5ba2\u6237\u6d88\u606f\u3002"""
    if not text:
        return False
    s = text.replace("\xa0", " ")
    return any(p in s for p in SELF_AI_PHRASES)

def card_has_pending_signal(text: str) -> bool:
    """Return True when the left session card means a customer message needs reply."""
    s = (text or "").replace("\xa0", " ").strip()
    if not s:
        return False
    # "已回复" prefix means bot already replied to this card - skip it.
    if s.startswith("\u5df2\u56de\u590d"):
        return False
    # "待回复" prefix or countdown/timeout means genuinely pending.
    if parse_countdown(s) is not None or "\u8d85\u65f6\u672a\u56de\u590d" in s:
        return True
    if not any(tag in s for tag in (
        "[\u673a\u5668\u4eba\u5df2\u6682\u505c]",
        "[\u673a\u5668\u4eba\u63a5\u5f85\u4e2d]",
        "[\u673a\u5668\u4eba\u6b63\u5728\u63a5\u5f85]",
        "[\u673a\u5668\u4eba\u5df2\u8f6c\u4eba\u5de5]",
    )):
        return False
    match = re.search(r"\[\u673a\u5668\u4eba(?:\u5df2\u6682\u505c|\u63a5\u5f85\u4e2d|\u6b63\u5728\u63a5\u5f85|\u5df2\u8f6c\u4eba\u5de5)\]\s*(.+)$", s)
    if not match:
        # Also match when followed by just a number (e.g. [机器人接待中]3)
        match = re.search(r"\[\u673a\u5668\u4eba(?:\u5df2\u6682\u505c|\u63a5\u5f85\u4e2d|\u6b63\u5728\u63a5\u5f85|\u5df2\u8f6c\u4eba\u5de5)\](\d+)\s*$", s)
        if match:
            # Number-only preview is still a pending signal
            return True
    if not match:
        return False
    preview = (match.group(1) or "").strip()
    if not preview:
        return False
    if any(x in preview for x in (
        "\u5df2\u8bfb",
        "\u672a\u8bfb",
        "\u5df2\u56de\u590d",
        "\u5df2\u7ecf\u5230\u5e95\u5566",
        "\u5168\u5bb6\u4fbf\u5229\u5e97",
    )):
        return False
    return True

# 选择器集合（实际部署时需要根据美团 IM 真实 DOM 调整）
CONVERSATION_LIST_SEL = "[class*='conversation'], [class*='session-list']"
CONVERSATION_ITEM_SEL = "[class*='conversation-item'], [class*='session-item']"
MESSAGE_BUBBLE_SEL = "[class*='message'], [class*='bubble']"
INPUT_BOX_SEL = "textarea, [contenteditable='true']"
SEND_BTN_SEL = "button[class*='send'], [class*='send-btn']"


JS_BTN_EVAL = '(el) => {\n  let p = el;\n  for (let i = 0; i < 12 && p; i++) {\n    if (p.parentElement) p = p.parentElement; else break;\n    const cands = p.querySelectorAll("button, [role=\'button\'], [class*=\'send\']");\n    for (const c of cands) {\n      const t = (c.innerText || "").trim();\n      const aria = (c.getAttribute("aria-label") || "").toLowerCase();\n      const cls = ((c.className || "") + "").toLowerCase();\n      const looksLikeSend = t === "\\u53d1\\u9001" || t.toLowerCase() === "send" || /send|\\u53d1\\u9001/i.test(aria) || /send/.test(cls);\n      if (!looksLikeSend) continue;\n      const block = c.closest("[class*=\'order\'], [class*=\'Order\']");\n      if (block) continue;\n      const txt = t.toLowerCase();\n      if (txt.includes("\\u9000\\u6b3e") || txt.includes("\\u9000\\u8d27")) continue;\n      return c;\n    }\n  }\n  return null;\n}'
JS_BUBBLE_TOTAL_EVAL = '() => {\n  const sels = ["[class*=\'message-item\']", "[class*=\'bubble\']"];\n  for (const s of sels) {\n    const l = document.querySelectorAll(s);\n    if (l.length) return l.length;\n  }\n  return 0;\n}'
JS_HITS_EVAL = '(payload) => {\n  const t = payload.text;\n  const sels = ["[class*=\'message-item\']", "[class*=\'bubble\']"];\n  let nodes = [];\n  for (const s of sels) {\n    const l = document.querySelectorAll(s);\n    if (l.length) { nodes = Array.from(l); break; }\n  }\n  let n = 0;\n  for (const el of nodes) {\n    const x = (el.innerText || "").trim();\n    if (x === t || x.indexOf(t) >= 0) n++;\n  }\n  return {count: n, total: nodes.length};\n}'
JS_TAG_EVAL = '(el) => {\n  return {\n    tag: el.tagName,\n    ce: el.contentEditable,\n    cls: (el.className || "").toString().slice(0,80)\n  };\n}'

class MeituanBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.monitor = cfg.get("monitor", {}) or {}
        self.state = ReplyState(Path(cfg["browser"]["profile_dir"]).parent / "state")
        self.last_self_check = 0.0
        self._stop = False
        self.last_cool = 0.0
        self._diag_last_ts: Dict[str, float] = {}
        self._last_page_reload_ts: float = 0.0
        self._idle_rounds: int = 0
        self._last_session_file = Path(cfg["browser"]["profile_dir"]).parent / "state" / "last_session.json"
        self._browser = None
        self._context = None
        self._page = None
        self._viewport = {"width": 1440, "height": 900}
        self._launch_args: List[str] = []
        self._headless = True
        self._current_cookie_mtime = 0.0
        self._restart_cooldown_until = 0.0

    def request_stop(self, *_) -> None:
        log.info("stop signal received")
        self._stop = True

    def _save_session(self, **fields: Any) -> None:
        try:
            self._last_session_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"timestamp": time.time(), **fields}
            with self._last_session_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.debug("save session failed: %s", e)

    # ---------- 浏览器生命周期 ----------
    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        # Bot uses cookie injection, not persistent context - no profile lock needed
        if not cookie_file_exists(self.cfg):
            log.warning("no cookie file found - please login via browser-control first")
        # Clear stale watermarks on startup so bot re-detects pending cards
        try:
            self.state.clear_watermarks()
        except Exception:
            pass
        with sync_playwright() as p:
            self._loop(p)

    def _is_page_alive(self) -> bool:
        if self._page is None or self._context is None or self._browser is None:
            return False
        try:
            if self._page.is_closed():
                return False
        except Exception:
            return False
        return True

    def _is_x11_fatal_error(self, err: Exception) -> bool:
        s = str(err).lower()
        if not s:
            return False
        needles = ("missing x server", "no display", "$display", "x11", "ozone_platform_x11",
                   "platform failed to initialize", "connection refused", "broken pipe", "fatal")
        if "display" in s and ("missing" in s or "not set" in s or "no display" in s):
            return True
        return any(n in s for n in needles)

    def _rebuild_browser(self, p: Playwright, reason: str = "") -> Optional["Page"]:
        """重建 browser + context + page，重新注入 cookie。返回新 page，失败返回 None。
        如果检测到 X11 / DISPLAY 错误，主动退出进程让 run.sh 重启。"""
        if time.time() < self._restart_cooldown_until:
            log.warning("rebuild in cooldown, skip: reason=%s", reason)
            return None
        self._restart_cooldown_until = time.time() + 30
        log.warning("rebuilding bot browser stack: reason=%s", reason)
        for close_fn, name in ((lambda: self._context.close() if self._context else None, "context"),
                                (lambda: self._browser.close() if self._browser else None, "browser")):
            try:
                close_fn()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._page = None
        x11_fail_streak = getattr(self, "_x11_fail_streak", 0)
        try:
            self._browser = p.chromium.launch(headless=self._headless, args=self._launch_args)
            self._x11_fail_streak = 0
        except Exception as e:
            if self._is_x11_fatal_error(e):
                self._x11_fail_streak = x11_fail_streak + 1
                log.error("X11/DISPLAY fatal on chromium launch (%d/3): %s", self._x11_fail_streak, e)
                if self._x11_fail_streak >= 3:
                    log.error("X11 keep dying, exiting so run.sh can restart with fresh Xvfb")
                    os._exit(2)
                time.sleep(10)
                return None
            log.error("rebuild launch failed: %s", e)
            time.sleep(5)
            return None
        try:
            self._context = self._browser.new_context(viewport=self._viewport)
            cookies = load_cookies(self.cfg)
            if cookies:
                self._context.add_cookies(cookies)
                log.info("re-injected %d cookies after rebuild", len(cookies))
            else:
                log.warning("no cookies to inject on rebuild - bot may not be logged in")
            self._page = self._context.new_page()
            self._bootstrap(self._page)
            self._dump_dom_diagnostics(self._page, tag="post-rebuild")
            self._diag_last_ts.clear()
            self._idle_rounds = 0
            try:
                self._current_cookie_mtime = cookie_file_path(self.cfg).stat().st_mtime
            except Exception:
                pass
            return self._page
        except Exception as e:
            if self._is_x11_fatal_error(e):
                log.error("X11/DISPLAY fatal on context/page create: %s", e)
                os._exit(2)
            log.error("rebuild failed: %s", e)
            return None

    def _loop(self, p: Playwright) -> None:
        # Use launch() + new_context() + add_cookies() instead of persistent context
        # This avoids profile lock conflict with browser-control
        launch_opts = build_launch_options(self.cfg, headless_override=False)
        self._headless = launch_opts.pop("headless", True)
        self._launch_args = launch_opts.pop("args", [])
        self._viewport = launch_opts.pop("viewport", {"width": 1440, "height": 900})
        self._browser = p.chromium.launch(headless=self._headless, args=self._launch_args)
        self._context = self._browser.new_context(viewport=self._viewport)
        cookies = load_cookies(self.cfg)
        if cookies:
            self._context.add_cookies(cookies)
            log.info("injected %d cookies into new context", len(cookies))
            try:
                self._current_cookie_mtime = cookie_file_path(self.cfg).stat().st_mtime
            except Exception:
                self._current_cookie_mtime = 0.0
        else:
            log.warning("no cookies to inject - bot may not be logged in!")
        try:
            self._page = self._context.new_page()
            self._bootstrap(self._page)
            self._dump_dom_diagnostics(self._page, tag="post-bootstrap")
            log.warning("[loop] entering main loop")
            page = self._page  # local alias for backward compat
            poll = max(1, int(self.monitor.get("poll_seconds", 2)))
            self_check_interval = max(60, int(self.monitor.get("self_check_interval_seconds", 300)))
            cookie_reload_interval = max(60, int(self.monitor.get("cookie_reload_check_seconds", 300)))
            last_cookie_reload_check = time.time()
            self_check_counter = 0
            scan_counter = 0
            while not self._stop:
                try:
                    if not self._is_page_alive():
                        new_page = self._rebuild_browser(p, reason="page-dead-precheck")
                        if new_page is None:
                            time.sleep(5)
                            continue
                        page = new_page
                    scan_counter += 1
                    try:
                        log.info("[loop] iter=%d url=%s", scan_counter, page.url[:80])
                    except Exception as e:
                        log.warning("page.url read failed: %s", e)
                        new_page = self._rebuild_browser(p, reason="url-read-failed")
                        if new_page is None:
                            time.sleep(5)
                            continue
                        page = new_page
                        continue
                    now = time.time()
                    if now - last_cookie_reload_check > cookie_reload_interval:
                        last_cookie_reload_check = now
                        try:
                            cookie_path = cookie_file_path(self.cfg)
                            new_mtime = cookie_path.stat().st_mtime if cookie_path.exists() else 0.0
                            if new_mtime and new_mtime > self._current_cookie_mtime:
                                log.info("cookie file updated; rebuilding bot context only")
                                try:
                                    self._context.close()
                                except Exception:
                                    pass
                                self._context = self._browser.new_context(viewport=self._viewport)
                                cookies = load_cookies(self.cfg)
                                if cookies:
                                    self._context.add_cookies(cookies)
                                    self._current_cookie_mtime = new_mtime
                                    log.info("re-injected %d updated cookies", len(cookies))
                                self._page = self._context.new_page()
                                self._bootstrap(self._page)
                                self._dump_dom_diagnostics(self._page, tag="post-cookie-reload")
                                page = self._page
                                continue
                        except Exception as e:
                            log.warning("cookie reload check failed: %s", e)
                            if "has been closed" in str(e) or "Target" in str(e):
                                new_page = self._rebuild_browser(p, reason="cookie-reload-failed")
                                if new_page is None:
                                    time.sleep(5)
                                    continue
                                page = new_page
                    if not url_is_correct(page, self.cfg):
                        log.warning("page redirected, navigating back url=%s", page.url)
                        try:
                            navigate_chat_url(page, self.cfg, log_prefix="[url-guard] ")
                            time.sleep(self.monitor.get("warmup_seconds", 5))
                            self._dump_dom_diagnostics(page, tag="post-redirect")
                        except Exception as e:
                            log.warning("url-guard navigate failed: %s", e)
                            new_page = self._rebuild_browser(p, reason="url-guard-failed")
                            if new_page is None:
                                time.sleep(5)
                                continue
                            page = new_page
                        continue
                    if time.time() - self.last_self_check > self_check_interval:
                        try:
                            self._self_check(page)
                        except Exception as e:
                            log.warning("self_check failed: %s", e)
                            if "has been closed" in str(e) or "Target" in str(e):
                                new_page = self._rebuild_browser(p, reason="self-check-failed")
                                if new_page is None:
                                    time.sleep(5)
                                    continue
                                page = new_page
                                continue
                        self.last_self_check = time.time()
                    did_work = self._scan_once(page)
                    # 空闲检测：连续多轮无候选 + 距上次刷新超过配置阈值，自动 reload 释放页面内存
                    if did_work:
                        self._idle_rounds = 0
                    else:
                        self._idle_rounds += 1
                    idle_min = int(self.monitor.get("page_reload_idle_minutes", 180))
                    idle_min_rounds = int(self.monitor.get("page_reload_idle_rounds", 100))
                    if self._idle_rounds >= idle_min_rounds and (time.time() - self._last_page_reload_ts) > idle_min * 60:
                        log.info("page idle reload triggered: idle_rounds=%d minutes_idle=%.0f", self._idle_rounds, (time.time() - self._last_page_reload_ts) / 60)
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            self._last_page_reload_ts = time.time()
                            self._idle_rounds = 0
                            self._diag_last_ts.clear()
                            time.sleep(self.monitor.get("warmup_seconds", 5))
                        except Exception as e:
                            log.warning("page idle reload failed: %s; rebuilding browser stack", e)
                            new_page = self._rebuild_browser(p, reason="page-reload-failed")
                            if new_page is None:
                                time.sleep(5)
                                continue
                            page = new_page
                except Exception as e:
                    log.exception("scan error: %s", e)
                    self_check_counter += 1
                    err_s = str(e)
                    if "has been closed" in err_s or "Target" in err_s or "browser has been closed" in err_s:
                        new_page = self._rebuild_browser(p, reason="scan-error-closed")
                        if new_page is None:
                            time.sleep(5)
                            continue
                        page = new_page
                    elif self_check_counter % 15 == 0:
                        try:
                            self._dump_dom_diagnostics(page, tag="on-error")
                        except Exception:
                            pass
                time.sleep(poll)
        finally:
            for close_fn, name in ((lambda: self._context.close() if self._context else None, "context"),
                                    (lambda: self._browser.close() if self._browser else None, "browser")):
                try:
                    close_fn()
                except Exception:
                    pass

    def _dump_dom_diagnostics(self, page: Page, tag: str = "debug", throttle_seconds: int = 300, force: bool = False) -> None:
        """把当前页面关键 DOM 信息写到日志。节流：同 tag 默认 5 分钟最多一次。"""
        if not force:
            now = time.time()
            last = self._diag_last_ts.get(tag, 0.0)
            if now - last < throttle_seconds:
                return
            self._diag_last_ts[tag] = now
        try:
            diag = page.evaluate("""() => {
                const sel = s => document.querySelectorAll(s).length;
                return {
                    url: location.href,
                    title: document.title,
                    body_text_head: (document.body.innerText || '').slice(0, 200).replace(/\\s+/g, ' '),
                    counts: {
                        iframe: sel('iframe'),
                        textarea: sel('textarea'),
                        contenteditable: sel('[contenteditable="true"]'),
                        list_class: sel('[class*="list"]'),
                        item_class: sel('[class*="item"]'),
                        row_class: sel('[class*="row"]'),
                        msg_class: sel('[class*="msg"]'),
                        message_class: sel('[class*="message"]'),
                        bubble_class: sel('[class*="bubble"]'),
                        send_class: sel('[class*="send"]'),
                        chat_class: sel('[class*="chat"]'),
                        session_class: sel('[class*="session"]'),
                        conversation_class: sel('[class*="conversation"]'),
                        workbench_class: sel('[class*="workbench"]'),
                        reception_class: sel('[class*="reception"]'),
                        button: sel('button'),
                    },
                };
            }""")
            log.warning("[diag-%s] %s", tag, json.dumps(diag, ensure_ascii=False))
        except Exception as e:
            log.warning("[diag-%s] dump failed: %s", tag, e)


    def _dispose_all(self, handles) -> None:
        """安全释放 ElementHandle 列表。失败不影响主流程。"""
        for h in handles or []:
            try:
                if h is not None and hasattr(h, "dispose"):
                    h.dispose()
            except Exception:
                pass

    def _bootstrap(self, page: Page) -> None:
        if self.monitor.get("startup_navigate", True):
            navigate_chat_url(page, self.cfg, log_prefix="[startup] ")
            warmup = self.monitor.get("warmup_seconds", 5)
            time.sleep(warmup)
        self._self_check(page)
        self.last_self_check = time.time()

    def _self_check(self, page: Page) -> None:
        try:
            cur = page.url or ""
            self._save_session(url=cur, last_status="alive")
            if url_is_bad(page, self.cfg):
                log.warning("self_check: on bad page url=%s", cur)
                navigate_chat_url(page, self.cfg, log_prefix="[self-check] ")
        except Exception as e:
            log.warning("self_check error: %s", e)

    # ---------- 扫描 ----------
    def _scan_once(self, page: Page) -> bool:
        # 限流
        cooldown = int(self.monitor.get("response_cooldown_seconds", 3))
        if time.time() - self.last_cool < cooldown:
            return False

        # 先看是不是在白名单
        if not url_is_correct(page, self.cfg):
            log.debug("scan_once: not on whitelist url=%s", page.url)
            return False

        # 找对话项：直接查元素（不依赖外层 list 容器，SPA 渲染时容器类名可能完全没有 list/panel 字样）
        # 用多个 selector 退化匹配，拿到所有候选再按"会话卡片特征"过滤
        raw_items: List[Any] = []
        for sel in (
            CONVERSATION_ITEM_SEL,
            "[class*='session']",
            "[class*='item']",
            "[class*='row']",
            "[class*='conversation']",
            "li",
            "[role='listitem']",
        ):
            try:
                cand = page.query_selector_all(sel)
                if cand and len(cand) >= 1:
                    raw_items = cand
                    log.debug("raw items matched selector=%r count=%d", sel, len(cand))
                    break
            except Exception:
                continue
        if not raw_items:
            log.debug("no conversation items found (all selectors failed)")
            self._dump_dom_diagnostics(page, tag="scan-no-items")
            return False

        # 过滤：识别真正的"会话卡片"
        # 会话卡片特征：
        #  - 多行文本（3-6 行：姓名+订单数+时间+状态）
        #  - 含"已下N单"+时间 或 状态标签
        # 排除：整个侧边栏容器（行数 > 6）、tab、按钮、列表底部
        candidates: List[Any] = []
        seen_texts = set()
        # 这些是 tab / 列表标识，出现说明是容器
        EXCLUDE_TOKENS = ("已经到底了", "暂无消息", "加载中")
        for it in raw_items:
            try:
                t = (it.inner_text() or "").strip()
            except Exception:
                continue
            if not t or len(t) > 180:  # 真实卡片 < 120 字符
                continue
            if t in seen_texts:
                continue
            # 排除包含容器标识的
            if any(tok in t for tok in EXCLUDE_TOKENS):
                continue
            lines = [l.strip() for l in t.split("\n") if l.strip()]
            # 真实会话卡片：3-6 行（姓名/订单数/时间/状态）
            if not (3 <= len(lines) <= 6):
                continue
            # 会话卡片特征判定
            has_order = "已下" in t  # "已下11单"
            has_time = bool(re.search(r"\d{1,2}:\d{2}", t))
            has_status = any(kw in t for kw in ("机器人", "客服", "推荐商品", "接待中"))
            has_pending = "超时未回复" in t or "待回复" in t
            # Must match: order info, or status+time, or pending signal.
            if not (has_order or has_pending or (has_status and has_time)):
                continue
            candidates.append((it, t))
            seen_texts.add(t)

        # 每个会话在本轮最多处理一次：不同 card 文本背后可能是同一个会话（带状态/带计数/带底部），
        # 以“订单号/门店新客 + v**”作为 session key 去重，不是个别文本交叉点击。
        deduped: List[Any] = []
        seen_sessions = set()
        for item, txt in candidates:
            cust = self._extract_name_from_text(txt)
            order_m = re.search(r"\d{1,3}\.\d{1,2}#\d+单", txt) or re.search(r"门店新客", txt)
            order = order_m.group(0) if order_m else "_"
            key = (order, cust if cust != "unknown" else txt[:40])
            if key in seen_sessions:
                log.info("[scan] dedupe skip duplicate candidate key=%s cust=%s", key, cust)
                continue
            seen_sessions.add(key)
            deduped.append((item, txt, cust))

        log.info("[scan] raw=%d candidates=%d unique=%d", len(raw_items), len(candidates), len(deduped))
        if not deduped:
            self._dump_dom_diagnostics(page, tag="scan-no-candidates")
            return False

        max_n = int(self.monitor.get("max_conversations_per_scan", 8))
        try:
            for item, txt, cust in deduped[:max_n]:
                if self._stop:
                    return False
                log.info("[scan] candidate cust=%s text=%r", cust, txt[:160].replace("\n", " | "))
                if not card_has_pending_signal(txt):
                    # Robot-tag cards without countdown/timeout: only process
                    # if card does NOT start with "已回复" (already replied).
                    if not txt.replace("\xa0", " ").strip().startswith("\u5df2\u56de\u590d"):
                        if any(tag in txt for tag in (
                            "[\u673a\u5668\u4eba\u5df2\u6682\u505c]",
                            "[\u673a\u5668\u4eba\u63a5\u5f85\u4e2d]",
                            "[\u673a\u5668\u4eba\u6b63\u5728\u63a5\u5f85]",
                            "[\u673a\u5668\u4eba\u5df2\u8f6c\u4eba\u5de5]",
                        )):
                            log.info("[scan] robot-tag pending cust=%s text=%r", cust, txt[:120])
                        else:
                            if is_self_ai_reply(txt):
                                log.info("[scan] skip self-ai preview cust=%s text=%r", cust, txt[:120])
                                continue
                    else:
                        log.info("[scan] skip already-replied card cust=%s text=%r", cust, txt[:80])
                        continue
                self._open_and_reply(page, item, expected_customer=cust, expected_card_text=txt)
        finally:
            # 一轮处理完，释放所有 ElementHandle
            self._dispose_all(raw_items)
            self._dispose_all([it for it, _ in candidates])
        return True

    def _open_and_reply(self, page: Page, item: Any, expected_customer: str = "", expected_card_text: str = "") -> None:

        # 直接点击过滤后的对话项（已通过"会话卡片"特征过滤，是真实的用户会话）
        try:
            item.click()
        except Exception as e:
            # 元素已经从 DOM 脱离（页面刚刷新/会话顺序变了）
            # 直接放弃这一轮，等下一次 scan 重新拿元素，避免落到错误的会话上。
            log.warning("open conversation click failed: %s (skip this round)", e)
            return
        finally:
            # 用完即释放，避免长跑累积 ElementHandle
            try:
                if item is not None and hasattr(item, "dispose"):
                    item.dispose()
            except Exception:
                pass
        log.info("opening conversation")
        time.sleep(2.0)

        # 找消息气泡：先在主页面找，失败则在所有 iframe 内找
        bubbles = self._find_bubbles(page)
        if not bubbles:
            log.warning("no message bubble after open (all selectors+frames failed)")
            self._dump_dom_diagnostics(page, tag="scan-no-bubble")
            return

        # 用 JS 在页面里按位置挑"最后一条对方消息"：
        #  - 商家自己发的消息靠右 (rect.left > viewport/2)
        #  - 顾客消息靠左 (rect.left <= viewport/2)
        # 兼容性：class 里也允许 self/own/me/right -> 视为我方
        last = None
        last_text = ""
        try:
            picked = page.evaluate("""() => {
                const sels = ["[class*='message-item']", "[class*='msg-bubble']", "[class*='message-bubble']", "[class*='chat-bubble']", "[class*='bubble']"];
                let nodes = [];
                for (const s of sels) {
                    const list = Array.from(document.querySelectorAll(s));
                    if (list.length) { nodes = list; break; }
                }
                if (!nodes.length) return null;
                // Find a chat container that holds most bubbles.
                let container = nodes[0].parentElement;
                for (let depth = 0; depth < 8 && container; depth++) {
                    const inside = nodes.filter(n => container.contains(n)).length;
                    if (inside >= Math.max(2, Math.floor(nodes.length * 0.6))) break;
                    container = container.parentElement;
                }
                const cRect = container ? container.getBoundingClientRect() : { left: 0, right: window.innerWidth || 1440, width: window.innerWidth || 1440 };
                const midX = cRect.left + cRect.width / 2;
                const cRight = cRect.right || (cRect.left + cRect.width);
                const isJunk = (t) => {
                    if (!t) return true;
                    if (t.length > 400) return true;
                    if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(t)) return true;
                    if (/^\d{4}[-\/]\d{1,2}[-\/]\d{1,2}.*$/.test(t)) return true;
                    if (/^[\s\-\d:\/]+$/.test(t)) return true;
                    if (t === '对方发送的这条消息可能涉及敏感或违规信息，请您加强防范意识~') return true;
                    // System banners / AI handoff notices
                    if (t.indexOf('人工接待后，本会话') >= 0) return true;
                    if (t.indexOf('AI客服已暂停') >= 0) return true;
                    if (t.indexOf('智能回复自动开启') >= 0) return true;
                    if (t.indexOf('未人工回复用户消息') >= 0) return true;
                    if (t.indexOf('消息支持撤回') >= 0) return true;
                    if (t.indexOf('语音转文字了') >= 0) return true;
                    if (t.indexOf('会话智能回复自动开启') >= 0) return true;
                    if (t.indexOf('新消息3分钟未回复则恢复AI托管') >= 0) return true;
                    if (t.indexOf('用户主动发起会话') >= 0) return true;
                    if (t.indexOf('用户长时间未回复') >= 0) return true;
                    if (t.indexOf('会话已自动结束') >= 0) return true;
                    if (t.indexOf('会话结束') >= 0) return true;
                    return false;
                };
                const isOwnByContent = (t) => {
                    if (!t) return false;
                    // Shop self-messages: any bubble containing the shop name is our own.
                    if (t.indexOf('全家便利店') >= 0) return true;
                    if (t.indexOf('[机器人') >= 0) return true;
                    if (t.indexOf('[AI客服') >= 0) return true;
                    if (t.indexOf('[商家') >= 0) return true;
                    if (t.indexOf('本消息由店铺') >= 0) return true;
                    if (t.indexOf('自动回复') >= 0) return true;
                    if (t.indexOf('以上信息是店铺AI会话') >= 0) return true;
                    return false;
                };
                const peers = [];
                for (let i = 0; i < nodes.length; i++) {
                    const el = nodes[i];
                    const t = (el.innerText || '').trim();
                    if (isJunk(t)) continue;
                    const c = (el.className || '').toString().toLowerCase();
                    if (c.includes('input') || c.includes('editor')) continue;
                    if (c.includes('time') || c.includes('divider') || c.includes('system') || c.includes('notice') || c.includes('tip') || c.includes('hint')) continue;
                    // Strong class hints first
                    let leftHint = c.includes('left') || c.includes('peer') || c.includes('other') || c.includes('customer') || c.includes('opposite');
                    let rightHint = c.includes('right') || c.includes('self') || c.includes('mine') || c.includes('own') || c.includes('me-');
                    // Walk a few ancestors to find a left/right marker if the element itself has none
                    if (!leftHint && !rightHint) {
                        let p = el.parentElement; for (let d = 0; d < 5 && p; d++, p = p.parentElement) {
                            const pc = (p.className || '').toString().toLowerCase();
                            if (pc.includes('left') || pc.includes('peer') || pc.includes('other') || pc.includes('customer') || pc.includes('opposite')) { leftHint = true; break; }
                            if (pc.includes('right') || pc.includes('self') || pc.includes('mine') || pc.includes('own')) { rightHint = true; break; }
                        }
                    }
                    if (rightHint) continue;
                    if (isOwnByContent(t)) continue;
                    if (!leftHint) {
                        // Geometry fallback: customer bubbles start on the LEFT half of the container.
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 4) continue;
                        const elMid = r.left + r.width / 2;
                        // Drop only when the bubble center sits in the right half (clearly own).
                        if (elMid > midX) continue;
                    }
                    const _r = el.getBoundingClientRect(); peers.push({ text: t, idx: i, top: _r.top, bottom: _r.bottom });
                }
                if (!peers.length) return { text: '', idx: -1, total: nodes.length, peer_total: 0, peer_texts: [], midX: midX };
                // Sort peers by visual position; the bottom-most visible customer bubble is the newest.
                peers.sort((a, b) => (a.bottom - b.bottom) || (a.top - b.top) || (a.idx - b.idx));
                const last = peers[peers.length - 1];
                return { text: last.text, idx: last.idx, total: nodes.length, peer_total: peers.length, peer_texts: peers.map(p => p.text), peer_debug: peers.slice(-8).map(p => ({text:p.text, idx:p.idx, top:Math.round(p.top), bottom:Math.round(p.bottom)})), midX: midX };
            }""")
        except Exception as e:
            log.debug("position-based bubble pick failed: %s", e)
            picked = None

        if picked:
            last_text = picked.get("text", "")
            peer_total = picked.get("peer_total", 0)
            peer_idx = picked.get("idx", -1)
            log.info("inbound bubble picked peer_total=%s idx=%s text=%r", peer_total, peer_idx, last_text[:60])
            try:
                _pt = picked.get("peer_texts", []) or []
                for _i, _t in enumerate(_pt[-6:]):
                    log.info("  peer[%d]=%r", _i, str(_t)[:80])
                _pd = picked.get("peer_debug", []) or []
                for _i, _p in enumerate(_pd[-6:]):
                    log.debug("  peer_pos[%d]=idx:%s top:%s bottom:%s text:%r", _i, _p.get("idx"), _p.get("top"), _p.get("bottom"), str(_p.get("text", ""))[:40])
            except Exception:
                pass
        else:
            last_text = ""
            peer_total = 0
            peer_idx = -1

        customer = expected_customer or self._extract_customer_name(item)

        # No customer message at all -> nothing to do
        if not last_text or last_text in ("[暂无消息]", "暂无消息", "加载中…", "加载中..."):
            log.debug("no inbound peer message for %s, skip", customer)
            return

        # Per-customer welcome state (keyed by customer name only)
        welcome_key = f"welcome_sent:{customer}"
        welcome_sent_before = self.state.already_replied(welcome_key, ttl_seconds=86400)

        # 新消息判据：只看会话卡片是否处于待回复状态。
        # 只要出现倒计时 Ns / N秒 或 超时未回复，就说明有顾客消息需要回复。
        # 不按顾客名或消息文本去重；顾客重复发送同一句，也必须一句回一句。
        card_text = (expected_card_text or "")
        is_pending = card_has_pending_signal(card_text)
        if not is_pending:
            log.debug("card not pending for %s (no countdown/timeout/robot-tag-new-msg), skip", customer)
            return
        # card is in a pending state (countdown / timeout / [\u673a\u5668\u4eba*] tag).
        # self-AI phrases inside the preview are shop-AI history, not a customer
        # message; user explicitly wants bot to reply under any [\u673a\u5668\u4eba] label.
        if is_self_ai_reply(card_text):
            log.info("self-ai preview present but card pending - continue customer=%s text=%r", customer, card_text[:120])

        import hashlib as _hashlib
        peer_fp = _hashlib.md5(((last_text or "").strip()).encode("utf-8")).hexdigest()[:12]
        wm_key = f"last_peer_fp:{customer}"
        last_fp = ""
        last_fp_age = 9999.0
        try:
            last_fp_raw = self.state.get_value(wm_key)
            if isinstance(last_fp_raw, str) and "|" in last_fp_raw:
                _fp, _ts = last_fp_raw.rsplit("|", 1)
                try:
                    last_fp_age = time.time() - float(_ts)
                except ValueError:
                    last_fp_age = 9999.0
                last_fp = _fp
        except Exception:
            last_fp = ""
        log.info("watermark customer=%s pending=1 fp=%s last_fp=%s age=%.0fs peer_total=%s",
                 customer, peer_fp, last_fp, last_fp_age, peer_total)
        # Force reply when countdown or 超时未回复 is present - never skip.
        has_countdown = parse_countdown(card_text) is not None
        has_timeout = "超时未回复" in (card_text or "")
        if has_countdown or has_timeout:
            if has_countdown:
                log.info("countdown detected=%ds - forcing reply customer=%s", parse_countdown(card_text), customer)
            else:
                log.info("timeout detected - forcing reply customer=%s", customer)
        elif last_fp == peer_fp:
            # No countdown/timeout but same bubble - skip to avoid loop
            log.info("watermark skip same visual bubble customer=%s fp=%s age=%.1fs", customer, peer_fp, last_fp_age)
            return
        # Anti-loop: limit sending the SAME reply text to the same customer
        # for a SPECIFIC message to MAX_SAME_REPLY (3) times.
        # Keyed by (customer + message fingerprint + reply text) so that when
        # the customer sends a new message the counter resets automatically.
        MAX_SAME_REPLY = 3

        def _same_reply_key(cust, msg_fp, reply_text):
            import hashlib as _rh
            return "same_reply:%s:%s:%s" % (
                cust or "unknown",
                msg_fp or "nofp",
                _rh.md5((reply_text or "").strip().encode("utf-8")).hexdigest()[:12],
            )

        def _same_reply_exceeded(cust, msg_fp, reply_text):
            try:
                cnt = int(self.state.get_value(_same_reply_key(cust, msg_fp, reply_text)) or 0)
            except Exception:
                cnt = 0
            return cnt >= MAX_SAME_REPLY

        def _inc_same_reply(cust, msg_fp, reply_text):
            k = _same_reply_key(cust, msg_fp, reply_text)
            try:
                cnt = int(self.state.get_value(k) or 0)
            except Exception:
                cnt = 0
            self.state.set_value(k, str(cnt + 1))

        decision = decide_reply(last_text, self.cfg, is_first_message=False)
        kw_hit = decision.rule not in ("first_message", "fallback")

        if not welcome_sent_before:
            # 第一次给这个客户回复：只发 welcome，不联动关键词。
            # 如果第一条消息有关键词，也只是先 welcome；客户再来一条时
            # 才走 keyword/fallback 路线。
            welcome = decide_reply("", self.cfg, is_first_message=True)
            log.info("first peer msg for %s: sending first_message", customer)
            if _same_reply_exceeded(customer, peer_fp, welcome.reply):
                log.info("same-reply limit (%d) for welcome customer=%s - skip", MAX_SAME_REPLY, customer)
            else:
                ok = self._send_reply(page, welcome.reply)
                if ok:
                    self.state.mark_replied(welcome_key)
                    self.last_cool = time.time()
                    _inc_same_reply(customer, peer_fp, welcome.reply)
                    log.info("sent welcome to %s", customer)
            self.state.set_value(wm_key, f"{peer_fp}|{time.time()}")
            self._record_reply_event(customer, last_text, "first_message", "first_message", welcome.reply, ok, peer_fp=peer_fp, card_text=card_text)
            return

        # Subsequent peer messages: keyword reply if matched, otherwise fallback.
        if kw_hit:
            log.info("subsequent peer msg rule=%s", decision.rule)
            if _same_reply_exceeded(customer, peer_fp, decision.reply):
                log.info("same-reply limit (%d) for keyword rule=%s customer=%s - skip", MAX_SAME_REPLY, decision.rule, customer)
            else:
                ok = self._send_reply(page, decision.reply)
                if ok:
                    self.state.set_value(wm_key, f"{peer_fp}|{time.time()}")
                    self.last_cool = time.time()
                    _inc_same_reply(customer, peer_fp, decision.reply)
                    log.info("sent keyword reply rule=%s", decision.rule)
            self._record_reply_event(customer, last_text, "keyword", decision.rule, decision.reply, ok, peer_fp=peer_fp, card_text=card_text)
        else:
            log.info("subsequent peer msg rule=fallback")
            if _same_reply_exceeded(customer, peer_fp, decision.reply):
                log.info("same-reply limit (%d) for fallback customer=%s - skip", MAX_SAME_REPLY, customer)
            else:
                ok = self._send_reply(page, decision.reply)
                if ok:
                    self.state.set_value(wm_key, f"{peer_fp}|{time.time()}")
                    self.last_cool = time.time()
                    _inc_same_reply(customer, peer_fp, decision.reply)
                    log.info("sent fallback reply")
            self._record_reply_event(customer, last_text, "fallback", decision.rule, decision.reply, ok, peer_fp=peer_fp, card_text=card_text)

    def _record_reply_event(self, customer: str, message: str, action: str, rule: str, reply: str, ok: bool, **extra: Any) -> None:
        try:
            dedupe_seconds = float(extra.pop("dedupe_seconds", 0) or 0)
            if dedupe_seconds > 0:
                import hashlib as _event_hashlib
                fp_src = "|".join([customer or "", message or "", action or "", rule or "", str(extra.get("card_text", ""))])
                event_key = "event_fp:" + _event_hashlib.md5(fp_src.encode("utf-8")).hexdigest()[:16]
                if self.state.already_replied(event_key, ttl_seconds=int(dedupe_seconds)):
                    return
                self.state.mark_replied(event_key)
            self.state.add_event({
                "customer": customer,
                "message": (message or "")[:240],
                "action": action,
                "rule": rule,
                "reply": (reply or "")[:240],
                "ok": bool(ok),
                **extra,
            })
        except Exception as e:
            log.debug("record reply event failed: %s", e)

    def _find_bubbles(self, page: Page) -> Optional[List[Any]]:
        """找消息气泡：用 textarea 作为锚点向上找聊天面板，再在面板内找消息列表。

        美团 IM 的聊天面板结构一般是：
          <div class=chat-panel>
            <header>...</header>
            <div class=message-list>
              <div class=message>...</div>  ← 我们要的就是这些
              ...
            </div>
            <footer>
              <textarea>...</textarea>
              <button>发送</button>
            </footer>
          </div>
        """
        # 第一阶段：尝试常规 selector（保留兼容）
        quick_sels = (
            "[class*='msg-bubble']",
            "[class*='message-bubble']",
            "[class*='chat-bubble']",
            "[class*='msg-item']",
            "[class*='message-item']",
            "[class*='chat-message']",
            "[class*='reception'] [class*='message']",
            "[class*='chat'] [class*='message']",
        )
        for sel in quick_sels:
            try:
                cand = page.query_selector_all(sel)
                if cand and 1 <= len(cand) <= 60:
                    log.debug("bubbles matched quick sel=%r count=%d", sel, len(cand))
                    return cand
            except Exception:
                continue

        # 第二阶段：用 JS 找包含 textarea 的聊天面板，再提取消息列表
        try:
            bubbles = page.evaluate_handle("""() => {
                const ta = document.querySelector('textarea');
                if (!ta) return null;
                // 向上找聊天面板（包含 >= 3 个直接子元素）
                let p = ta;
                for (let i = 0; i < 10 && p; i++) {
                    p = p.parentElement;
                    if (!p) break;
                    if (p.children.length >= 3) {
                        // 在面板里找：子元素数量 >= 1 且 < 50，且不含 textarea
                        for (const c of p.children) {
                            if (c.querySelector && c.querySelector('textarea')) continue;
                            if (c.children.length >= 1 && c.children.length <= 50) {
                                const t = (c.innerText || '').trim();
                                // 消息列表通常有内容但不太长
                                if (t.length >= 1 && t.length < 3000) {
                                    return c;
                                }
                            }
                        }
                    }
                }
                return null;
            }""")
            if bubbles:
                # 拿这个 handle 里的子元素
                # JSHandle 可以 evaluate_as_element 取 ElementHandle
                el = bubbles.as_element()
                if el:
                    # 在这个元素内找消息
                    for sel_inner in ("[class*='message']", "[class*='bubble']", "[class*='item']", "*"):
                        try:
                            cand = el.query_selector_all(sel_inner)
                            if cand and 1 <= len(cand) <= 60:
                                log.debug("bubbles matched panel-inner sel=%r count=%d", sel_inner, len(cand))
                                return cand
                        except Exception:
                            continue
        except Exception as e:
            log.debug("js-bubble-scan failed: %s", e)

        # 第三阶段：iframe 退化（虽然 diagnostics 显示 iframe=0，但保留兼容）
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                for sel in quick_sels:
                    try:
                        cand = frame.query_selector_all(sel)
                        if cand and 1 <= len(cand) <= 60:
                            log.debug("bubbles matched frame url=%s sel=%r count=%d", frame.url[:60], sel, len(cand))
                            return cand
                    except Exception:
                        continue
        except Exception as e:
            log.debug("iframe scan failed: %s", e)
        return None

    @staticmethod
    def _extract_name_from_text(txt: str) -> str:
        """从已经拿到的 candidate 文本里抠出客户名。

        和 _extract_customer_name 保持一致的规则，但直接吃字符串，
        避免在第二次读取 item.inner_text() 时拿到已经变化的 DOM。
        """
        if not txt:
            return "unknown"
        BAD_PREFIXES = ("待回复", "已回复", "超时未回复", "机器人", "客服", "门店新客", "已下")
        # Reject tokens that look like a countdown ("58s", "59") or a card label
        # ("已经到底啦~", "已回复N"). Real customer names are short Chinese
        # or masked Latin like "v**" / "w**".
        COUNTDOWN_RE = re.compile(r"^\d{1,3}s?$")
        tokens = [seg.strip().replace("\xa0", " ").strip()
                  for seg in re.split(r"[\n|]+", txt or "")]
        for line in tokens:
            if not line:
                continue
            if any(line.startswith(p) for p in BAD_PREFIXES):
                continue
            if re.fullmatch(r"[\d:.\-/\s]+", line):
                continue
            if COUNTDOWN_RE.match(line):
                continue
            if len(line) > 8:
                continue
            return line
        return "unknown"

    def _extract_customer_name(self, item: Any) -> str:
        try:
            txt = (item.inner_text() or "").strip()
        except Exception:
            return "unknown"
        BAD_PREFIXES = ("待回复", "已回复", "超时未回复", "机器人", "客服", "门店新客", "已下")
        COUNTDOWN_RE = re.compile(r"^\d{1,3}s?$")
        # Split by newline OR vertical bar OR multiple spaces
        tokens = [seg.strip().replace("\xa0", " ").strip()
                  for seg in re.split(r"[\n|]+", txt)]
        for line in tokens:
            if not line:
                continue
            if any(line.startswith(p) for p in BAD_PREFIXES):
                continue
            if re.fullmatch(r"[\d:.\-/\s]+", line):
                continue
            if COUNTDOWN_RE.match(line):
                continue
            if len(line) > 16:
                continue
            # Strong filter: real customer names are short (<= 8 chars) and
            # often masked like 'v**' / 'w**' / Chinese 2-3 char.
            if len(line) > 8:
                continue
            return line
        return "unknown"

    def _send_reply(self, page, text):
        try:
            input_sels = (
                INPUT_BOX_SEL, "textarea", "input[type='text']", "[contenteditable='true']",
                "[class*='editor']", "[class*='input']", "[class*='chat-input']",
            )
            box = self._query_in_frames(page, input_sels, role="input")
            if not box:
                log.warning("input box not found (all selectors+frames failed)")
                self._dump_dom_diagnostics(page, tag="send-no-input")
                return False
            try:
                box.click()
            except Exception:
                pass
            try:
                page.evaluate(
                    "el => { if (el && 'value' in el) { el.value = ''; } else if (el) { el.innerText = ''; } }",
                    box,
                )
            except Exception:
                pass
            try:
                page.keyboard.type(text, delay=15)
            except Exception as e:
                log.warning("keyboard.type failed, falling back to box.fill: %s", e)
                try:
                    box.fill(text)
                except Exception as e2:
                    log.warning("box.fill also failed: %s", e2)
                    return False
            try:
                tinfo = page.evaluate(JS_TAG_EVAL, box)
                log.info("send-debug: tag=%s ce=%s cls=%s", tinfo.get("tag"), tinfo.get("ce"), tinfo.get("cls"))
            except Exception as e:
                log.debug("send-debug eval failed: %s", e)

            def _bubble_total():
                try:
                    return int(page.evaluate(JS_BUBBLE_TOTAL_EVAL) or 0)
                except Exception:
                    return 0

            def _check_hits(txt):
                try:
                    return page.evaluate(JS_HITS_EVAL, {"text": txt})
                except Exception as e:
                    log.debug("after-send check failed: %s", e)
                    return None

            def _find_chat_send_btn():
                try:
                    h = box.evaluate_handle(JS_BTN_EVAL)
                    return h.as_element() if h else None
                except Exception as e:
                    log.debug("chat send btn locator failed: %s", e)
                    return None

            total_before = _bubble_total()
            btn = _find_chat_send_btn()
            clicked_via_btn = False
            if btn:
                try:
                    btn.click()
                    clicked_via_btn = True
                    log.info("send-debug chat-btn clicked")
                except Exception as e:
                    log.debug("chat-btn click failed: %s", e)
            if not clicked_via_btn:
                log.info("send-debug chat-btn not found, using Enter")
                try:
                    page.keyboard.press("Enter")
                except Exception as e:
                    log.warning("Enter fallback failed: %s", e)
                    return False

            time.sleep(1.2)
            hits = _check_hits(text)
            total_after = _bubble_total()
            log.info("send-debug after-send: hits=%s total_before=%s total_after=%s clicked_via_btn=%s",
                     hits, total_before, total_after, clicked_via_btn)
            text_sent = bool(hits and hits.get("count", 0) > 0)

            if not text_sent and total_after > total_before:
                log.warning("send-reality-check: bubble count grew but our text NOT found -> likely order-card hijack. One Enter retry.")
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                time.sleep(1.0)
                hits = _check_hits(text)
                text_sent = bool(hits and hits.get("count", 0) > 0)
                log.info("send-debug after-enter: hits=%s", hits)

            if not text_sent and total_after == total_before:
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                time.sleep(1.0)
                hits = _check_hits(text)
                text_sent = bool(hits and hits.get("count", 0) > 0)
                log.info("send-debug nothing-sent retry: hits=%s", hits)

            ok = text_sent
            if not ok:
                log.warning("send-reality-check FAILED: text not found in message list (hits=%s)", hits)
            return ok
        except Exception as e:
            log.exception("send_reply error: %s", e)
            return False
    def _query_in_frames(self, page: Page, selectors, role: str = ""):
        """在主 frame + 所有 iframe 里按顺序尝试 selectors，返回第一个匹配的元素。"""
        # 主 frame
        for sel in selectors:
            try:
                cand = page.query_selector(sel)
                if cand:
                    log.debug("%s matched main sel=%r", role or "el", sel)
                    return cand
            except Exception:
                continue
        # 子 frames
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                for sel in selectors:
                    try:
                        cand = frame.query_selector(sel)
                        if cand:
                            log.debug("%s matched frame url=%s sel=%r", role or "el", frame.url[:60], sel)
                            return cand
                    except Exception:
                        continue
        except Exception as e:
            log.debug("frame scan failed: %s", e)
        return None

    def _query_send_btn_in_frames(self, page: Page, selectors):
        """找发送按钮：要求 aria/text 含"发送"/"send"，避免拿到任意 button。"""
        # 主 frame
        for sel in selectors:
            try:
                cand = page.query_selector(sel)
            except Exception:
                cand = None
            if not cand:
                continue
            aria = (cand.get_attribute("aria-label") or "").lower()
            txt = (cand.inner_text() or "").strip()
            if "send" in sel.lower() or "send" in aria or "发送" in aria or "发送" in txt or "send" in txt.lower():
                log.debug("send btn matched main sel=%r aria=%r text=%r", sel, aria, txt[:20])
                return cand
        # 子 frames
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                for sel in selectors:
                    try:
                        cand = frame.query_selector(sel)
                    except Exception:
                        cand = None
                    if not cand:
                        continue
                    aria = (cand.get_attribute("aria-label") or "").lower()
                    txt = (cand.inner_text() or "").strip()
                    if "send" in sel.lower() or "send" in aria or "发送" in aria or "发送" in txt or "send" in txt.lower():
                        log.debug("send btn matched frame url=%s sel=%r", frame.url[:60], sel)
                        return cand
        except Exception as e:
            log.debug("send btn frame scan failed: %s", e)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    bot = MeituanBot(cfg)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
