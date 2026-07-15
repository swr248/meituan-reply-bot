"""公共：配置加载、浏览器启动、日志。

改动点（针对之前审查时发现的"崩溃后无法重启到正确监控画面"问题）：
- pre_start_kill_chromium: 启动浏览器前杀掉残留 chromium 子进程，
  防止 systemd 拉起新 Python 进程时旧 Chromium 锁住 profile。
- navigate_chat_url: 启动后强制跳到 chat_url。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format=LOG_FORMAT,
    stream=sys.stdout,
)
log = logging.getLogger("meituan-bot")


def load_config(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def _kill_chromium_for_profile(profile_dir: str) -> None:
    """杀掉占用同一 profile 目录的残留 chromium 进程。

    systemd Restart=always 拉起新 Python 时，旧 Chromium 子进程可能没退，
    导致新 Playwright 启动失败且被吞。Playwright 启动前先清理。
    """
    try:
        # 用 lsof 反查：哪些进程打开了 profile_dir 下的文件
        r = subprocess.run(
            ["bash", "-c", f"lsof +D '{profile_dir}' 2>/dev/null | awk 'NR>1{{print $2}}' | sort -u"],
            capture_output=True, text=True, timeout=10,
        )
        pids = [p for p in r.stdout.split() if p.isdigit()]
        for pid in pids:
            try:
                os.kill(int(pid), 9)
                log.info("killed stale chromium pid=%s", pid)
            except ProcessLookupError:
                pass
    except Exception as e:
        log.debug("kill_chromium cleanup skipped: %s", e)


def build_launch_options(cfg: Dict[str, Any], headless_override: bool | None = None) -> Dict[str, Any]:
    """构造 Playwright chromium launch 参数。"""
    browser_cfg = cfg.get("browser", {}) or {}
    headless = browser_cfg.get("headless", True) if headless_override is None else headless_override
    args = list(browser_cfg.get("launch_args", []) or [])
    if headless and "--headless=new" not in args and "headless" not in " ".join(args):
        args.append("--headless=new")
    opts = {
        "headless": headless,
        "args": args,
        "viewport": browser_cfg.get("viewport", {"width": 1440, "height": 900}),
    }
    ua = browser_cfg.get("user_agent")
    if ua:
        opts["user_agent"] = ua
    return opts


def ensure_profile_dir(profile_dir: str) -> None:
    p = Path(profile_dir)
    p.mkdir(parents=True, exist_ok=True)
    # 兼容：profile 损坏时直接清空（OOM/强杀后可能半损坏）
    sentinel = p / ".ready"
    if not sentinel.exists():
        log.warning("profile sentinel missing, treating as cold start: %s", p)


def pre_start_cleanup(cfg: Dict[str, Any]) -> None:
    """在 Playwright 启动之前调用：杀掉残留 chromium。"""
    browser_cfg = cfg.get("browser", {}) or {}
    if not browser_cfg.get("pre_start_kill_chromium", True):
        return
    profile = browser_cfg.get("profile_dir", "")
    if profile:
        _kill_chromium_for_profile(profile)


def navigate_chat_url(page, cfg: Dict[str, Any], log_prefix: str = "") -> None:
    """强制跳到正确的 chat_url。"""
    meituan = cfg.get("meituan", {}) or {}
    url = meituan.get("chat_url", "")
    if not url:
        log.error("%sno chat_url in config", log_prefix)
        return
    log.info("%snavigating to chat_url url=%s", log_prefix, url)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)


def url_is_correct(page, cfg: Dict[str, Any]) -> bool:
    """检查当前 URL 是否在白名单。
    注意：page.url 不含 hash (#) 部分，要兼容路由放在 fragment 里的 SPA。
    """
    monitor = cfg.get("monitor", {}) or {}
    pattern = monitor.get("url_guard_pattern", "im/page/workbench/reception")
    if not pattern:
        return True
    try:
        cur = page.url or ""
    except Exception:
        return False
    if pattern in cur:
        return True
    # 兼容：pattern 可能在 URL 的 hash 里（SPA 路由）
    try:
        hash_part = page.evaluate("() => location.hash || ''") or ""
    except Exception:
        hash_part = ""
    return pattern in hash_part


def url_is_bad(page, cfg: Dict[str, Any]) -> bool:
    """检查是否被重定向到错误页面。"""
    meituan = cfg.get("meituan", {}) or {}
    bad = meituan.get("bad_url_pattern", "")
    if not bad:
        return False
    try:
        cur = page.url or ""
    except Exception:
        return False
    return bad in cur
