"""Cookie capture: per-shop two-role (im/promo) service with VNC so users
can drive the browser from the page itself.

Architecture:
- One FastAPI process per shop (port 5901 or 5902).
- Inside the process we launch:
    * Xvfb on DISPLAY=:88
    * x11vnc on :88 capturing the screen, listening on TCP 5911
    * websockify on :5900 + :60 web server pointing at /usr/share/novnc
    * Two Playwright sessions sharing :88 but using two profile dirs
      (`profiles/im/profile`, `profiles/promo/profile`).
- The page (/vnc/ws, /vnc/index.html, /novnc/*) serves noVNC and forwards
  the WebSocket to x11vnc over `localhost:5911`. Captured browsers are
  visible on screen so users can complete the manual login (scan QR / type
  SMS code etc.).
- Active role is chosen via ?role=im|promo. Tabs at the top switch views.
- Independent "Save IM Cookie" / "Save Promo Cookie" buttons always export
  the matching profile's cookies to its own state dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

from browser_common import (
    build_launch_options,
    ensure_profile_dir,
    load_config,
    log,
    pre_start_cleanup,
)
from cookie_sync import export_cookies, cookie_file_path, cookie_file_age_seconds, load_cookies
from auth_ticket import consume_ticket


LOGIN_FALLBACK = "https://shangoue.meituan.com/"
# Per-shop X display + VNC ports, set in main() based on --port.
# Shop1 (5901): display 88, rfb 5911, ws 5900
# Shop2 (5902): display 89, rfb 5912, ws 5905
DISPLAY_NUM = 88
VNC_RFB_PORT = 5911
WEBSOCK_PORT = 5900

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Cookie 采集</title>
<style>
body{font-family:-apple-system,Segoe UI,sans-serif;background:#0f172a;color:#e2e8f0;margin:0}
.wrap{max-width:1000px;margin:0 auto;padding:16px}
.card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:14px;margin-bottom:10px}
button{background:#2563eb;border:0;color:#fff;padding:10px 18px;border-radius:4px;cursor:pointer;margin-right:6px;font-size:14px}
button.green{background:#16a34a}
.muted{color:#94a3b8;font-size:12px}
.url{word-break:break-all;color:#93c5fd;font-family:monospace}
.ok{color:#34d399}.bad{color:#f87171}
.tabbar{display:flex;gap:6px;margin-bottom:10px}
.tabbar a{color:#93c5fd;text-decoration:none;padding:6px 14px;border-radius:4px;background:#334155;font-size:13px}
.tabbar a.active{background:#2563eb;color:#fff}
.status{font-size:12px;color:#94a3b8}
iframe{border:1px solid #334155;background:#000;border-radius:4px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.col{flex:1 1 480px;min-width:320px}
</style></head>
<body><div class="wrap">
<h1>Cookie 采集 <span class="muted">(__SHOP__ / 端口 __PORT__)</span></h1>
<div id="tokenHint" class="muted" style="margin-bottom:8px"></div>
<div class="tabbar">
  <a id="tab-im" href="#">IM（在线联系）登录</a>
  <a id="tab-promo" href="#">推广（一站式）登录</a>
</div>
<div class="row">
<div class="col">
<div class="card">
  <div class="muted">当前视图：<code id="curRole">im</code></div>
  <div class="muted">浏览器 URL（点击"去登录页"后这里会更新）</div>
  <div class="url" id="curUrl">--</div>
  <div style="margin-top:8px">
    <button onclick="nav('login')">去登录页</button>
    <button onclick="nav('reload')">刷新当前页</button>
  </div>
</div>
<div class="card">
  <div class="muted">手动保存 Cookie（两个按钮独立导出对应 profile 的 Cookie）</div>
  <div style="margin-top:8px">
    <button class="green" onclick="saveCookie('im')">保存 IM Cookie</button>
    <button class="green" onclick="saveCookie('promo')">保存推广 Cookie</button>
  </div>
  <div class="muted" style="margin-top:6px">登录 IM 视图保存 IM；切到推广视图保存推广。</div>
</div>
<div class="card">
  <div class="muted">Cookie 状态</div>
  <div id="ck-im" class="status">IM: --</div>
  <div id="ck-promo" class="status">推广: --</div>
</div>
</div>
<div class="col">
<div class="card">
  <div class="muted">服务器浏览器实时画面（双视图共享屏幕；保存前先点对应 Tab）</div>
  <iframe id="vnc" width="100%" height="540" allow="fullscreen"></iframe>
  <div class="muted" style="margin-top:4px">提示：点画面可点击；扫码 / 输短信码都在这里完成。</div>
</div>
</div>
</div>
</div>
<script>
const TOKEN = new URL(location.href).searchParams.get('token') || '';
const ROLE = new URL(location.href).searchParams.get('role') || 'im';
for (const role of ['im', 'promo']) {
  const tabUrl = new URL(location.href);
  tabUrl.searchParams.set('role', role);
  document.getElementById('tab-' + role).href = tabUrl.toString();
}
document.getElementById('curRole').textContent = ROLE;
document.getElementById('tab-' + ROLE).classList.add('active');
if (!TOKEN) {
  document.getElementById('tokenHint').innerHTML =
    '<span class="bad">[未授权]</span> 请用配置中的真实 token 打开本页，例如 ?token=&lt;auth_token&gt;&amp;role=im|promo';
} else {
  document.getElementById('tokenHint').innerHTML =
    '<span class="ok">[已授权]</span> token 已加载，role=<code>' + ROLE + '</code>';
}

function withToken(url){
  const u = new URL(url, location.origin);
  u.searchParams.set('token', TOKEN);
  return u.toString();
}

async function api(p, opts, role){
  const u = new URL(p, location.origin);
  u.searchParams.set('token', TOKEN);
  if (role) u.searchParams.set('role', role);
  const r = await fetch(u, opts||{});
  let body = null;
  try { body = await r.json(); } catch(e) { body = null; }
  if (!r.ok) {
    const detail = (body && body.detail) ? body.detail : '';
    const err = new Error('HTTP ' + r.status + (detail ? ': ' + detail : ''));
    err.status = r.status; err.detail = detail; err.body = body;
    throw err;
  }
  return body;
}
async function nav(kind){
  await api('/api/nav?kind='+kind, {method:'POST'}, ROLE);
  refresh();
}
async function saveCookie(role){
  try {
    const r = await api('/api/export-cookies', {method:'POST'}, role);
    if (r.ok) {
      alert('已保存 ' + role + ' Cookie：' + r.cookie_count + ' 条\\n路径：' + r.path);
    } else {
      alert('保存失败: ' + (r.error || ''));
    }
  } catch(e) {
    alert('保存失败: ' + (e && e.message ? e.message : e));
  }
  refreshCookie();
}
async function refresh(){
  const el = document.getElementById('curUrl');
  if (!TOKEN) {
    el.textContent = '[未授权] 缺少 ?token= 参数';
    refreshCookie();
    return;
  }
  try {
    const r = await api('/api/info', null, ROLE);
    document.getElementById('curUrl').textContent = r.url || '(未打开)';
  } catch(e) {
    document.getElementById('curUrl').textContent = '[错误] ' + (e && e.message ? e.message : e);
  }
  refreshCookie();
}
async function refreshCookie(){
  for (const role of ['im', 'promo']) {
    const el = document.getElementById('ck-' + role);
    if (!TOKEN) {
      el.innerHTML = '<span class="bad">[未授权]</span> 缺少 ?token= 参数';
      continue;
    }
    try {
      const r = await api('/api/cookie-status', null, role);
      if (r.exists) {
        el.innerHTML = '<span class="ok">[已保存]</span> ' + r.cookie_count + ' 个 (年龄 ' + r.age_str + ') -> <span class="url">' + r.path + '</span>';
      } else {
        el.innerHTML = '<span class="bad">[尚未保存]</span> 写入路径：<span class="url">' + r.path + '</span>';
      }
    } catch(e) {
      el.innerHTML = '<span class="bad">[错误]</span> ' + (e && e.message ? e.message : e);
    }
  }
}
function connectVnc(){
  const params = 'host=' + encodeURIComponent(location.hostname)
    + '&port=' + encodeURIComponent(location.port || '443')
    + '&path=' + encodeURIComponent('vnc/ws')
    + '&token=' + encodeURIComponent(TOKEN)
    + '&autoconnect=true&resize=scale&reconnect=true&show_dot=true';
  document.getElementById('vnc').src = '/novnc/vnc_lite.html?v=' + Date.now() + '&' + params;
}
connectVnc();
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""


# --- per-role Playwright worker ---------------------------------------------

class RoleWorker:
    def __init__(self, role: str, base_cfg: Dict[str, Any], profile_root: Path):
        self.role = role
        self.cfg: Dict[str, Any] = deepcopy(base_cfg)
        profile_dir = profile_root / role / "profile"
        state_dir = profile_root / role / "state"
        profile_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        browser = self.cfg.setdefault("browser", {})
        browser["profile_dir"] = str(profile_dir)
        self.profile_dir = profile_dir
        self.state_dir = state_dir
        self.playwright = None
        self.browser = None
        self.page = None
        self.queue: "queue.Queue" = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.ready = threading.Event()
        self._start()

    def _run(self) -> None:
        self.ready.clear()
        try:
            pre_start_cleanup(self.cfg)
            ensure_profile_dir(str(self.profile_dir))
            self.playwright = sync_playwright().start()
            opts = build_launch_options(self.cfg, headless_override=False)
            self.browser = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                **opts,
            )
            injected = _inject_main_cookies(self.browser, self.role)
            self.page = self.browser.pages[0] if self.browser.pages else self.browser.new_page()
            monitor = self.cfg.get("monitor", {}) or {}
            if monitor.get("startup_navigate", False):
                try:
                    url = (self.cfg.get("meituan", {}) or {}).get("login_url") or LOGIN_FALLBACK
                    self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    log.info("capture[%s]: loaded %s", self.role, url)
                except Exception as e:
                    log.warning("capture[%s]: initial goto failed: %s", self.role, e)
            else:
                if injected and self.page.url and "login" in self.page.url.lower():
                    try:
                        url = (self.cfg.get("meituan", {}) or {}).get("chat_url") or LOGIN_FALLBACK
                        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        log.info("capture[%s]: login page refreshed with injected cookies: %s", self.role, url)
                    except Exception as e:
                        log.warning("capture[%s]: injected-cookie navigation failed: %s", self.role, e)
                log.info("capture[%s]: preserving profile startup page %s", self.role, self.page.url)
            log.info("capture[%s]: browser launched", self.role)
            self.ready.set()
        except Exception as e:
            log.exception("capture[%s]: launch failed: %s", self.role, e)

        # Keep the worker thread alive: process submitted jobs, restart browser if dead.
        while True:
            try:
                try:
                    job = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                fn, args, kwargs, rq = job
                if fn is None:
                    self._close_in_worker()
                    rq.put(("ok", None))
                    return
                if not self._alive():
                    self._restart_browser()
                try:
                    res = fn(*args, **kwargs)
                    rq.put(("ok", res))
                except Exception as e:
                    log.exception("capture[%s] job err: %s", self.role, e)
                    rq.put(("err", e))
            except Exception as e:
                log.exception("capture[%s] loop err: %s", self.role, e)

    def _alive(self) -> bool:
        if self.browser is None or self.page is None:
            return False
        try:
            if self.page.is_closed():
                return False
            self.browser.cookies()
            return True
        except Exception:
            return False

    def _restart_browser(self) -> None:
        self.ready.clear()
        log.warning("capture[%s]: restart browser", self.role)
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        opts = build_launch_options(self.cfg, headless_override=False)
        self.browser = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            **opts,
        )
        injected = _inject_main_cookies(self.browser, self.role)
        self.page = self.browser.pages[0] if self.browser.pages else self.browser.new_page()
        monitor = self.cfg.get("monitor", {}) or {}
        if monitor.get("startup_navigate", False):
            try:
                url = (self.cfg.get("meituan", {}) or {}).get("login_url") or LOGIN_FALLBACK
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log.warning("capture[%s]: restart goto failed: %s", self.role, e)
        elif injected and self.page.url and "login" in self.page.url.lower():
            try:
                url = (self.cfg.get("meituan", {}) or {}).get("chat_url") or LOGIN_FALLBACK
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log.warning("capture[%s]: restart injected-cookie navigation failed: %s", self.role, e)
        self.ready.set()

    def is_ready(self) -> bool:
        return self.ready.is_set() and self.thread is not None and self.thread.is_alive()

    def _start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"capture-{self.role}")
        self.thread.start()

    def submit(self, fn, *args, **kwargs):
        rq: "queue.Queue" = queue.Queue(maxsize=1)
        self.queue.put((fn, args, kwargs, rq))
        try:
            status, payload = rq.get(timeout=60)
        except queue.Empty as exc:
            raise RuntimeError(f"capture[{self.role}] job timed out") from exc
        if status == "err":
            raise RuntimeError(payload)
        return payload

    def _close_in_worker(self) -> None:
        self.ready.clear()
        try:
            if self.browser:
                self.browser.close()
        except Exception as exc:
            log.debug("capture[%s]: browser already closed during shutdown: %s", self.role, exc)
        finally:
            self.browser = None
            self.page = None
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception as exc:
            log.debug("capture[%s]: playwright already stopped during shutdown: %s", self.role, exc)
        finally:
            self.playwright = None

    def close(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            return
        rq: "queue.Queue" = queue.Queue(maxsize=1)
        self.queue.put((None, (), {}, rq))
        try:
            status, payload = rq.get(timeout=60)
        except queue.Empty as exc:
            raise RuntimeError(f"capture[{self.role}] close timed out") from exc
        if status == "err":
            raise RuntimeError(payload)
        self.thread.join(timeout=5)


# --- Xvfb + x11vnc + websockify lifecycle ------------------------------------

class XStack:
    def __init__(self) -> None:
        os.environ["DISPLAY"] = f":{DISPLAY_NUM}"
        self.xvfb = None
        self.x11vnc = None
        self.websockify = None

    @staticmethod
    def _port_listening(port: int) -> bool:
        return os.system(f"ss -ltn 2>/dev/null | grep -q ':{port} '") == 0

    def start(self) -> None:
        try:
            self.xvfb = subprocess.Popen(
                ["Xvfb", f":{DISPLAY_NUM}", "-screen", "0", "1440x900x24",
                 "-ac", "+extension", "GLX", "+render", "-noreset"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.warning("Xvfb not on PATH; assuming an external X server")
        for _ in range(40):
            if os.system(f"xdpyinfo -display :{DISPLAY_NUM} >/dev/null 2>&1") == 0:
                break
            time.sleep(0.5)
        log.info("Xvfb ready on :%s", DISPLAY_NUM)

        # x11vnc binds to localhost to avoid a password warning.
        try:
            self.x11vnc = subprocess.Popen(
                ["x11vnc", "-display", f":{DISPLAY_NUM}",
                 "-rfbport", str(VNC_RFB_PORT), "-forever", "-shared",
                 "-nopw", "-noxdamage",
                 "-no6",
                 "-listen", "127.0.0.1",
                 "-logfile", f"/tmp/x11vnc_capture_{VNC_RFB_PORT}.log"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RuntimeError("x11vnc missing on PATH")
        for _ in range(40):
            if self._port_listening(VNC_RFB_PORT):
                break
            if self.x11vnc and self.x11vnc.poll() is not None:
                raise RuntimeError(f"x11vnc exited early rc={self.x11vnc.returncode}; see /tmp/x11vnc_capture_{VNC_RFB_PORT}.log")
            time.sleep(0.5)
        if not self._port_listening(VNC_RFB_PORT):
            raise RuntimeError(f"x11vnc did not listen on {VNC_RFB_PORT}; see /tmp/x11vnc_capture_{VNC_RFB_PORT}.log")
        log.info("x11vnc ready on :%s", VNC_RFB_PORT)

        # websockify serves noVNC static files + proxies WS -> x11vnc RFB.
        try:
            self.websockify = subprocess.Popen(
                ["websockify", "--web", "/usr/share/novnc",
                 str(WEBSOCK_PORT), f"localhost:{VNC_RFB_PORT}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RuntimeError("websockify missing on PATH")
        for _ in range(40):
            if self._port_listening(WEBSOCK_PORT):
                break
            if self.websockify and self.websockify.poll() is not None:
                raise RuntimeError(f"websockify exited early rc={self.websockify.returncode}")
            time.sleep(0.5)
        if not self._port_listening(WEBSOCK_PORT):
            raise RuntimeError(f"websockify did not listen on {WEBSOCK_PORT}")
        log.info("websockify ready on :%s", WEBSOCK_PORT)

    def stop(self) -> None:
        for p in (self.websockify, self.x11vnc, self.xvfb):
            try:
                if p:
                    p.terminate()
                    p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


# --- FastAPI app -------------------------------------------------------------

app = FastAPI(title="Cookie Capture")
_cfg: Dict[str, Any] = {}
_shop: str = "meituan-reply-bot"
_listen_port: int = 5901
_workers: Dict[str, RoleWorker] = {}
_worker_lock = threading.Lock()
_xstack: Optional[XStack] = None
_AUTH_SESSIONS: Dict[str, float] = {}
_AUTH_SESSION_LOCK = threading.Lock()
_AUTH_SESSION_TTL = 600
_main_config_path: Optional[Path] = None


def _main_config() -> Dict[str, Any]:
    if _main_config_path and _main_config_path.exists():
        try:
            return load_config(_main_config_path)
        except Exception as e:
            log.warning("capture: failed to load main config %s: %s", _main_config_path, e)
    return deepcopy(_cfg)


def _main_cookie_path() -> Path:
    cfg = _main_config()
    state_dir = (cfg.get("state") or {}).get("dir")
    return Path(state_dir) / "cookies.json" if state_dir else cookie_file_path(cfg)


def _role_cookie_path(role: str) -> Path:
    return cookie_file_path(_role_config(role))


def _should_inject_main_cookies(role: str) -> bool:
    if role != "im":
        return False
    main = _main_cookie_path()
    role_cookie = _role_cookie_path(role)
    if not main.exists():
        return False
    if not role_cookie.exists():
        return True
    return main.stat().st_mtime >= role_cookie.stat().st_mtime


def _inject_main_cookies(context, role: str) -> int:
    if not _should_inject_main_cookies(role):
        return 0
    cookies = load_cookies(_main_config())
    if not cookies:
        return 0
    context.add_cookies(cookies)
    log.info("capture[%s]: injected %d main cookies", role, len(cookies))
    return len(cookies)


def _export_main_cookies(context, role: str) -> Dict[str, Any]:
    count = export_cookies(context, _main_config())
    return {"cookie_count": count, "path": str(_main_cookie_path())}

# Mount noVNC static files so the VNC client is served from the same port.
_NOVNC_DIR = "/usr/share/novnc"
if Path(_NOVNC_DIR).is_dir():
    app.mount("/novnc", StaticFiles(directory=_NOVNC_DIR), name="novnc")
    log.info("capture: mounted noVNC from %s", _NOVNC_DIR)
else:
    log.warning("capture: noVNC dir not found: %s", _NOVNC_DIR)


def _valid_tokens() -> list[str]:
    server = _cfg.get("server", {}) or {}
    expected = server.get("auth_token", "")
    valid = [expected] if expected else []
    for item in server.get("legacy_auth_tokens", []) or []:
        if item and item not in valid:
            valid.append(item)
    if not expected or expected == "<set-your-token>":
        return []
    return valid


def _token_is_valid(token: Optional[str]) -> bool:
    if token and token in _valid_tokens():
        return True
    if not token:
        return False
    now = time.time()
    with _AUTH_SESSION_LOCK:
        expires = _AUTH_SESSIONS.get(token, 0)
        if expires < now:
            _AUTH_SESSIONS.pop(token, None)
            return False
        return True


def _issue_auth_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _AUTH_SESSION_LOCK:
        expired = [key for key, expires in _AUTH_SESSIONS.items() if expires < now]
        for key in expired:
            _AUTH_SESSIONS.pop(key, None)
        _AUTH_SESSIONS[token] = now + _AUTH_SESSION_TTL
    return token


def _ticket_shop_id() -> str:
    return "shop2" if _shop in ("shop2", "meituan-reply-bot-shop2") else "shop1"


def _check_token(token: Optional[str]) -> None:
    if not _valid_tokens():
        raise HTTPException(500, "auth_token not set in config.yaml")
    if not _token_is_valid(token):
        raise HTTPException(401, "invalid token")


def _get_worker(role: str) -> RoleWorker:
    if role not in ("im", "promo"):
        raise HTTPException(400, f"bad role: {role}")
    with _worker_lock:
        current = _workers.get(role)
        if current is not None:
            return current
        for worker in list(_workers.values()):
            worker.close()
        _workers.clear()
        current = RoleWorker(role, _role_config(role), Path.cwd() / "profiles")
        _workers[role] = current
        log.info("capture: role worker %s started lazily", role)
        return current


def _role_config(role: str) -> Dict[str, Any]:
    """Return a config copy with the profile_dir pointed at this role's dir."""
    cfg = deepcopy(_cfg)
    profile_root = Path(_cfg.get("browser", {}).get("profile_dir", "profiles"))
    # The service WorkingDirectory is the capture dir; profile_root is relative.
    if not profile_root.is_absolute():
        profile_root = Path.cwd() / "profiles"
    profile_dir = profile_root / role / "profile"
    cfg.setdefault("browser", {})["profile_dir"] = str(profile_dir)
    return cfg


@app.on_event("startup")
def _startup() -> None:
    global _xstack, _workers
    _xstack = XStack()
    _xstack.start()

    log.info("capture: browser worker will start on first role request")


@app.on_event("shutdown")
def _shutdown() -> None:
    for w in _workers.values():
        w.close()
    if _xstack:
        _xstack.stop()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = INDEX_HTML.replace("__SHOP__", _shop).replace("__PORT__", str(_listen_port))
    return HTMLResponse(html)


@app.get("/auth/exchange")
def auth_exchange(ticket: str = Query(...)) -> RedirectResponse:
    secret = str((_cfg.get("server", {}) or {}).get("auth_token", "") or "")
    try:
        consume_ticket(ticket, secret, _ticket_shop_id(), target="browser")
    except ValueError as exc:
        raise HTTPException(401, "invalid auth ticket") from exc
    session = _issue_auth_session()
    return RedirectResponse(f"/?token={session}&role=im", status_code=302)


@app.get("/api/info")
def info(token: str = Query(""), role: str = Query("im")):
    _check_token(token)
    w = _get_worker(role)

    def job():
        return {"url": (w.page.url if w.page else None)}

    try:
        return w.submit(job)
    except Exception as e:
        return {"url": None, "error": str(e)}


@app.post("/api/nav")
def nav(kind: str = Query(...), token: str = Query(""), role: str = Query("im")):
    _check_token(token)
    w = _get_worker(role)
    meituan = w.cfg.get("meituan", {}) or {}

    def job():
        if w.page is None:
            raise RuntimeError("browser not ready")
        if kind == "login":
            url = meituan.get("login_url") or LOGIN_FALLBACK
            w.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        elif kind == "reload":
            w.page.reload(wait_until="domcontentloaded", timeout=30000)
        else:
            raise HTTPException(400, "bad kind")
        return {"ok": True, "url": w.page.url}

    return w.submit(job)


@app.post("/api/goto")
def goto_url(
    url: str = Query(...),
    wait_ms: int = Query(5000, ge=0, le=30000),
    token: str = Query(""),
    role: str = Query("promo"),
):
    _check_token(token)
    w = _get_worker(role)

    def job():
        if w.page is None:
            raise RuntimeError("browser not ready")
        w.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if wait_ms:
            w.page.wait_for_timeout(wait_ms)
        return {"ok": True, "url": w.page.url}

    return w.submit(job)


@app.post("/api/eval")
def eval_js(js: str = Query(...), token: str = Query(""), role: str = Query("promo")):
    _check_token(token)
    w = _get_worker(role)

    def job():
        if w.page is None:
            raise RuntimeError("browser not ready")
        result = w.page.evaluate(js)
        frame_url = w.page.url
        if result is None:
            for frame in w.page.frames:
                if frame == w.page.main_frame:
                    continue
                try:
                    frame_result = frame.evaluate(js)
                except Exception:
                    continue
                if frame_result is not None:
                    result = frame_result
                    frame_url = frame.url
                    break
        return {"ok": True, "result": result, "frame_url": frame_url}

    return w.submit(job)


@app.post("/api/click")
def click_selector(
    selector: str = Query(...),
    token: str = Query(""),
    role: str = Query("promo"),
):
    _check_token(token)
    w = _get_worker(role)

    def job():
        if w.page is None:
            raise RuntimeError("browser not ready")
        for frame in w.page.frames:
            locator = frame.locator(selector)
            try:
                if locator.count() > 0:
                    locator.first.click(timeout=10000)
                    return {"ok": True, "url": w.page.url, "frame_url": frame.url}
            except Exception:
                continue
        raise RuntimeError(f"selector not found: {selector}")

    return w.submit(job)


@app.post("/api/export-cookies")
def export_now(token: str = Query(""), role: str = Query("im")):
    _check_token(token)
    w = _get_worker(role)

    def job():
        if w.browser is None:
            raise RuntimeError("browser not started")
        n = export_cookies(w.browser, w.cfg)
        payload = {"ok": True, "cookie_count": n, "path": str(cookie_file_path(w.cfg))}
        if role == "im":
            payload["main_sync"] = _export_main_cookies(w.browser, role)
        return payload

    return w.submit(job)


@app.get("/api/cookie-status")
def cookie_status(token: str = Query(""), role: str = Query("im")):
    _check_token(token)
    w = _get_worker(role)
    path = cookie_file_path(w.cfg)
    exists = path.exists()
    age = cookie_file_age_seconds(w.cfg) if exists else None
    age_str = ""
    cookie_count = 0
    if exists:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cookie_count = data.get("cookie_count", 0)
        except Exception:
            pass
        if age is not None:
            if age < 60:
                age_str = f"{int(age)}s"
            elif age < 3600:
                age_str = f"{int(age/60)}m"
            else:
                age_str = f"{age/3600:.1f}h"
    return {
        "exists": exists,
        "age_seconds": age,
        "age_str": age_str,
        "cookie_count": cookie_count,
        "path": str(path),
        "main_path": str(_main_cookie_path()) if role == "im" else "",
    }


@app.get("/api/health")
def health(token: str = Query(""), role: Optional[str] = Query(None)):
    _check_token(token)
    if role is not None:
        _get_worker(role)
    return {
        "ok": True,
        "shop": _shop,
        "port": _listen_port,
        "roles": {r: w.is_ready() for r, w in _workers.items()},
    }


# --- VNC reverse proxy (noVNC static files + WebSocket) ----------------------
#
# websockify already serves noVNC on :5900. We mount /novnc/* as a passthrough
# so users can reach it from the same port as the capture UI. The WebSocket
# endpoint /vnc/ws is proxied to the local websockify instance.

@app.websocket("/vnc/ws")
async def vnc_ws(ws: WebSocket):
    if not _token_is_valid(ws.query_params.get("token")):
        await ws.close(code=1008, reason="invalid token")
        return
    # noVNC sends 'binary' subprotocol; accept it so the handshake succeeds.
    sub = ws.headers.get("sec-websocket-protocol", "")
    protocols = [s.strip() for s in sub.split(",") if s.strip()] if sub else None
    await ws.accept(subprotocol=protocols[0] if protocols else None)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_RFB_PORT)
    except Exception as e:
        log.warning("capture: VNC proxy connect failed: %s", e)
        await ws.close()
        return
    try:
        async def ws_to_tcp():
            try:
                while True:
                    data = await ws.receive_bytes()
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass

        async def tcp_to_ws():
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except Exception:
                pass

        await asyncio.gather(ws_to_tcp(), tcp_to_ws())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def main() -> int:
    global _cfg, _shop, _listen_port, _main_config_path, DISPLAY_NUM, VNC_RFB_PORT, WEBSOCK_PORT
    parser = argparse.ArgumentParser(description="Cookie capture service")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None)
    parser.add_argument("--shop", default="meituan-reply-bot")
    parser.add_argument("--main-config", default=None)
    args = parser.parse_args()

    _cfg = load_config(args.config)
    _shop = args.shop
    _main_config_path = Path(args.main_config).resolve() if args.main_config else None
    server = _cfg.get("server", {}) or {}
    host = args.host or server.get("remote_browser_host", "0.0.0.0")
    port = int(args.port or server.get("remote_browser_port", 5901))
    _listen_port = port

    # Per-shop X display + VNC ports so two services don't collide.
    DISPLAY_NUM = {5901: 88, 5902: 89}.get(port, 88)
    VNC_RFB_PORT = {5901: 5911, 5902: 5912}.get(port, 5911)
    WEBSOCK_PORT = {5901: 5900, 5902: 5905}.get(port, 5900)
    log.info("capture: starting shop=%s host=%s port=%s display=:%s rfb=%s ws=%s", _shop, host, port, DISPLAY_NUM, VNC_RFB_PORT, WEBSOCK_PORT)
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
