#!/usr/bin/env python3
"""美团 一站式推广 开关定时调度器

Capture 按需启动：
- 调度器常驻只计算当前时间窗口，不常驻浏览器。
- 启动时确认一次状态。
- 只有期望状态发生变化时，临时启动 capture，确认/点击后如果是本进程启动的就停止。
"""

import argparse
import fcntl
import logging
import subprocess
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


def _run_systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class CaptureLease:
    def __init__(self, service: str, lock_name: str, log: logging.Logger):
        self.service = service
        self.lock_name = lock_name
        self.log = log
        self.lock_file = None
        self.started = False

    def __enter__(self):
        Path("/run").mkdir(parents=True, exist_ok=True)
        self.lock_file = open(f"/run/meituan-capture-{self.lock_name}.lock", "w")
        self.log.info("waiting capture lock: %s", self.lock_name)
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)
        active = _run_systemctl("is-active", "--quiet", self.service).returncode == 0
        if not active:
            self.log.info("starting capture service: %s", self.service)
            r = _run_systemctl("start", self.service)
            if r.returncode != 0:
                raise RuntimeError(f"systemctl start {self.service} failed: {r.stderr.strip()}")
            self.started = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.started:
            self.log.info("stopping capture service: %s", self.service)
            _run_systemctl("stop", self.service)
        if self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
        return False


class BrowserClient:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def health(self) -> dict:
        qs = parse.urlencode({"token": self.token})
        return _http_get(f"{self.base}/api/health?{qs}", timeout=5)

    def wait_health(self, seconds: int = 180) -> None:
        deadline = time.time() + seconds
        last_error = None
        while time.time() < deadline:
            try:
                r = self.health()
                if r.get("ok") and (r.get("roles") or {}).get("promo"):
                    return
                last_error = r
            except Exception as e:
                last_error = e
            time.sleep(2)
        raise RuntimeError(f"capture health timeout: {last_error}")

    def goto(self, url: str, wait_ms: int = 5000) -> dict:
        qs = parse.urlencode({"url": url, "wait_ms": wait_ms, "token": self.token, "role": "promo"})
        return _http_post(f"{self.base}/api/goto?{qs}", timeout=60)

    def eval(self, js: str) -> dict:
        qs = parse.urlencode({"js": js, "token": self.token, "role": "promo"})
        return _http_post(f"{self.base}/api/eval?{qs}", timeout=60)

    def click(self, selector: str) -> dict:
        qs = parse.urlencode({"selector": selector, "token": self.token, "role": "promo"})
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
                return r["result"]
        except Exception:
            return None
        return None


def parse_hhmm(s: str) -> int:
    h, m = str(s).strip().split(":")[:2]
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
            if now_min >= on or now_min < off:
                return True
    return False


def infer_shop(root: Path, server: dict) -> tuple[str, str]:
    suffix = str(server.get("instance_suffix") or "")
    if suffix == "-shop2" or root.name.endswith("shop2"):
        return "shop2", "meituan-capture-meituan-reply-bot-shop2.service"
    return "shop1", "meituan-capture-meituan-reply-bot.service"


def load_capture_token(cfg_path: Path, token: str, log: logging.Logger) -> str:
    if token and token not in ("<set-your-token>", "<redacted-token>"):
        return token
    try:
        capture_cfg = cfg_path.parent / "capture" / "config.yaml"
        if capture_cfg.exists():
            cap = yaml.safe_load(capture_cfg.read_text(encoding="utf-8")) or {}
            cap_token = (cap.get("server") or {}).get("auth_token", "")
            if cap_token and cap_token not in ("<set-your-token>", "<redacted-token>"):
                log.info("using capture service token from capture/config.yaml")
                return cap_token
    except Exception as e:
        log.warning("capture token fallback failed: %s", e)
    return token


def reconcile(bc: BrowserClient, target_url: str, switch_sel: str, desired: str, log: logging.Logger) -> bool:
    current = bc.get_state()
    if current is None:
        log.info("state unknown, navigating to %s", target_url)
        bc.goto(target_url, wait_ms=8000)
        for _ in range(25):
            time.sleep(1)
            current = bc.get_state()
            if current is not None:
                break
    if current is None:
        log.warning("still unknown after navigate")
        return False

    cur_on = bool(current.get("on"))
    cur_label = "on" if cur_on else "off"
    log.info("reconcile: current=%s desired=%s url=%s", cur_label, desired, current.get("url", ""))
    if cur_label == desired:
        return True

    log.info("toggle required: clicking %s", switch_sel)
    r = bc.click(switch_sel)
    log.info("click result: %s", r)
    new_state = None
    for _ in range(12):
        time.sleep(1)
        new_state = bc.get_state()
        if new_state and (bool(new_state.get("on")) != cur_on):
            break
    log.info("post-click state: %s", new_state)
    return bool(new_state) and (("on" if new_state.get("on") else "off") == desired)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="检查一次后退出")
    args = ap.parse_args()
    cfg_path = Path(args.config)

    log_path = cfg_path.parent / "logs" / "promo_scheduler.log"
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

    initial_cfg = _load_config()
    server = initial_cfg.get("server", {}) or {}
    port = server.get("capture_port") or server.get("remote_browser_port") or 5901
    base = f"http://127.0.0.1:{int(port)}"
    token = load_capture_token(cfg_path, server.get("auth_token", ""), log)
    shop_name, capture_service = infer_shop(cfg_path.parent, server)
    bc = BrowserClient(base, token)
    log.info("started; base=%s shop=%s capture_service=%s", base, shop_name, capture_service)

    last_reconciled_desired = None
    _logged_disabled = False

    while True:
        try:
            cfg = _load_config()
            sched = cfg.get("promotion_scheduler") or {}
            interval = int(sched.get("check_interval_sec", 30))
            if not sched.get("enabled", False):
                if not _logged_disabled:
                    log.info("scheduler disabled; idle")
                    _logged_disabled = True
                if args.once:
                    return 0
                time.sleep(interval)
                continue
            _logged_disabled = False

            target_url = sched.get(
                "target_url",
                "https://waimaieapp.meituan.com/ad/v1/rpc?&#/subapp/isomor_sg_onestop/pages/onestop/index",
            )
            switch_sel = sched.get("switch_selector", ".sg-onestop-header-switch")
            windows = sched.get("windows", [])
            now = datetime.now()
            now_m = now.hour * 60 + now.minute
            desired = "on" if in_any_window(now_m, windows) else "off"

            if args.once or desired != last_reconciled_desired:
                log.info("desired state requires reconcile: previous=%s desired=%s", last_reconciled_desired, desired)
                with CaptureLease(capture_service, shop_name, log):
                    bc.wait_health()
                    if reconcile(bc, target_url, switch_sel, desired, log):
                        last_reconciled_desired = desired
                    else:
                        log.warning("reconcile failed; will retry next tick")
                if args.once:
                    return 0
            else:
                log.debug("idle: desired=%s already reconciled", desired)
        except Exception as e:
            log.exception("tick error: %s", e)
            if args.once:
                return 1

        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
