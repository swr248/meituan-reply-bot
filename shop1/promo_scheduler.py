#!/usr/bin/env python3
"""美团 一站式推广 开关定时调度器

- 通过 browser-control HTTP API 操控：goto / eval / click
- 读取 config.yaml -> promotion_scheduler.windows 决定期望状态
- 期望状态 = 当前时间落在任一 [on, off) 窗口内 -> ON，否则 OFF
- 与现有 bot / remote_browser 互不冲突（独立进程）
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import parse, request

try:
    import yaml
except Exception:
    print("PyYAML required", file=sys.stderr)
    raise


def _http_get(url: str, timeout: int = 30) -> dict:
    with request.urlopen(url, timeout=timeout) as r:
        import json as _json
        return _json.loads(r.read().decode())


def _http_post(url: str, timeout: int = 30) -> dict:
    req = request.Request(url, method="POST")
    with request.urlopen(req, timeout=timeout) as r:
        import json as _json
        return _json.loads(r.read().decode())


class BrowserClient:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def goto(self, url: str, wait_ms: int = 5000) -> dict:
        qs = parse.urlencode({"url": url, "wait_ms": wait_ms, "token": self.token})
        return _http_post(f"{self.base}/api/goto?{qs}", timeout=60)

    def eval(self, js: str) -> dict:
        qs = parse.urlencode({"js": js, "token": self.token})
        return _http_post(f"{self.base}/api/eval?{qs}", timeout=60)

    def click(self, selector: str) -> dict:
        qs = parse.urlencode({"selector": selector, "token": self.token})
        return _http_post(f"{self.base}/api/click?{qs}", timeout=60)

    def get_state(self) -> dict | None:
        js = r'''(function(){
          var sw = document.querySelector(".sg-onestop-header-switch");
          if(!sw) return null;
          var input = sw.querySelector("input[type=checkbox]");
          return {on: !!(input && input.checked), url: location.href};
        })()'''
        try:
            r = self.eval(js)
            if r.get("ok") and r.get("result") is not None and r["result"] != "null":
                # result is already a JSON object (dict), not a string
                return r["result"]
        except Exception:
            return None
        return None


def parse_hhmm(s: str) -> int:
    h, m = s.strip().split(":")[:2]
    return int(h) * 60 + int(m)


def in_any_window(now_min: int, windows: list) -> bool:
    for w in windows:
        try:
            on = parse_hhmm(w["start"])
            off = parse_hhmm(w["end"])
        except Exception:
            continue
        if on == off:
            continue
        if on < off:
            if on <= now_min < off:
                return True
        else:
            # 跨夜窗口
            if now_min >= on or now_min < off:
                return True
    return False


def now_minutes() -> int:
    return datetime.now().hour * 60 + datetime.now().minute


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="检查一次后退出")
    args = ap.parse_args()
    cfg_path = Path(args.config)

    log_path = Path(cfg_path).parent / "logs" / "promo_scheduler.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] promo: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("promo")

    def _load_config():
        try:
            return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            log.warning("config load failed: %s", e)
            return {}

    server = _load_config().get("server", {})
    base = f"http://127.0.0.1:{server.get('remote_browser_port', 5901)}"
    token = server.get("auth_token", "")
    bc = BrowserClient(base, token)
    log.info("started; base=%s", base)

    COOLDOWN = 60
    last_action_ts = 0.0
    _logged_disabled = False

    while True:
        try:
            cfg = _load_config()
            sched = cfg.get("promotion_scheduler") or {}
            if not sched.get("enabled", False):
                if not _logged_disabled:
                    log.info("scheduler disabled; idle")
                    _logged_disabled = True
                if args.once:
                    return 0
                time.sleep(int(sched.get("check_interval_sec", 30)))
                continue
            _logged_disabled = False
            target_url = sched.get(
                "target_url",
                "https://waimaieapp.meituan.com/ad/v1/rpc?&#/subapp/isomor_sg_onestop/pages/onestop/index",
            )
            switch_sel = sched.get("switch_selector", ".sg-onestop-header-switch")
            interval = int(sched.get("check_interval_sec", 30))
            windows = sched.get("windows", [])
            now = datetime.now()
            now_m = now.hour * 60 + now.minute
            desired = "on" if in_any_window(now_m, windows) else "off"
            current = bc.get_state()
            if current is None:
                log.info("state unknown, navigating to %s", target_url)
                bc.goto(target_url, wait_ms=8000)
                for _ in range(20):
                    time.sleep(1)
                    current = bc.get_state()
                    if current is not None:
                        break
            if current is None:
                log.warning("still unknown after navigate, will retry next tick")
            else:
                cur_on = bool(current.get("on"))
                cur_label = "on" if cur_on else "off"
                log.info("tick: current=%s desired=%s url=%s", cur_label, desired, current.get("url", ""))
                if cur_label != desired and (time.time() - last_action_ts) >= COOLDOWN:
                    log.info("toggle required: clicking %s", switch_sel)
                    r = bc.click(switch_sel)
                    log.info("click result: %s", r)
                    last_action_ts = time.time()
                    for _ in range(10):
                        time.sleep(1)
                        new_state = bc.get_state()
                        if new_state and (new_state.get("on") != current.get("on")):
                            break
                    log.info("post-click state: %s", new_state)
        except Exception as e:
            log.exception("tick error: %s", e)

        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
