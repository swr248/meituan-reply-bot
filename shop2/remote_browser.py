"""Login browser control page (port 5901).

Changes:
- After login, auto-export cookies to JSON for bot to use
- Cookie keepalive: periodically refresh page + re-export cookies
- Both bot and browser-control can run simultaneously (no profile lock)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
import threading
import json
import queue
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

from browser_common import (
    build_launch_options,
    ensure_profile_dir,
    load_config,
    log,
    navigate_chat_url,
    pre_start_cleanup,
)
from cookie_sync import export_cookies, cookie_file_exists, cookie_file_age_seconds

def _write_status(logged_in: bool, url: str, last_export_ts: float = 0.0, error: str = "", manual_login_needed: bool = False) -> None:
    log.info("status write called: logged_in=%s manual=%s", logged_in, manual_login_needed)
    """keepalive 状态写到 state/cookie_status.json，供 admin.py / auto-refresh 读取。"""
    try:
        sd = _state_dir_path()
        log.info("status write: step1 _state_dir=%s", sd)
        sd.mkdir(parents=True, exist_ok=True)
        sf = sd / "cookie_status.json"
        log.info("status write: step2 sf=%s exists=%s", sf, sf.exists())
        try:
            if sf.exists() and (time.time() - sf.stat().st_mtime) < 2.0:
                log.info("status write: skipped recent")
                return
        except Exception as e:
            log.warning("status write: throttle check failed: %s", e)
        data = {
            "logged_in": bool(logged_in),
            "manual_login_needed": bool(manual_login_needed),
            "last_check_ts": time.time(),
            "last_url": url or "",
            "last_export_ts": float(last_export_ts) if last_export_ts else 0.0,
            "last_error": error or "",
        }
        tmp = sf.with_suffix(".json.tmp")
        log.info("status write: step3 tmp=%s", tmp)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log.info("status write: step4 wrote tmp")
        tmp.replace(sf)
        log.info("status write: step5 replaced sf OK")
    except Exception as e:
        log.warning("write status failed: %s", e)


app = FastAPI(title="Meituan Login Browser")
ROOT = Path(__file__).resolve().parent

def _resolve_state_dir() -> Path:
    """与 cookie_sync.cookie_file_path 保持一致：在 profile_dir 同级 state/ 下。"""
    try:
        profile_dir = (_cfg.get("browser", {}) or {}).get("profile_dir", "") if _cfg else ""
        if profile_dir:
            return Path(profile_dir).parent / "state"
    except Exception:
        pass
    return ROOT / "state"

def _state_dir_path() -> Path:
    return _resolve_state_dir()

_state_dir = _resolve_state_dir()  # legacy

_cfg: Dict[str, Any] = {}
_browser = None
_page = None
_playwright = None
_worker_thread = None
_worker_queue = queue.Queue()
_worker_ready = threading.Event()
_worker_stop = threading.Event()
_keepalive_stop = threading.Event()
_status_last_export_ts = 0.0
NOVNC_DIR = "/usr/share/novnc"
VNC_RFB_HOST = "127.0.0.1"
VNC_RFB_PORT = 5900

def _check_token(token):
    server = (_cfg.get("server", {}) or {})
    expected = server.get("auth_token", "")
    valid_tokens = []
    if expected:
        valid_tokens.append(expected)
    for item in server.get("legacy_auth_tokens", []) or []:
        if item and item not in valid_tokens:
            valid_tokens.append(item)
    if not expected:
        raise HTTPException(500, "auth_token not set in config.yaml")
    if token not in valid_tokens:
        raise HTTPException(401, "invalid token")


def _worker_run():
    """Single thread that owns the Playwright instance + browser + page."""
    global _browser, _page, _playwright
    try:
        pre_start_cleanup(_cfg)
        ensure_profile_dir(_cfg["browser"]["profile_dir"])
        _playwright = sync_playwright().start()
        opts = build_launch_options(_cfg, headless_override=False)
        _browser = _playwright.chromium.launch_persistent_context(
            user_data_dir=_cfg["browser"]["profile_dir"],
            **opts,
        )
        _page = _browser.pages[0] if _browser.pages else _browser.new_page()
        try:
            if (_cfg.get("monitor", {}) or {}).get("startup_navigate", True):
                navigate_chat_url(_page, _cfg, log_prefix="[keepalive-startup] ")
        except Exception as e:
            log.warning("keepalive startup navigation failed: %s", e)
        log.info("playwright worker: browser launched")
    except Exception as e:
        log.error("playwright worker: launch failed: %s", e)
        _worker_ready.set()
        return

    _worker_ready.set()

    keepalive_interval = int((_cfg.get("monitor", {}) or {}).get("keepalive_seconds", 60))
    export_interval = int((_cfg.get("monitor", {}) or {}).get("cookie_export_seconds", 300))
    refresh_interval = int((_cfg.get("monitor", {}) or {}).get("keepalive_refresh_seconds", 1800))
    last_keepalive = time.time()
    last_export = 0.0
    last_refresh = 0.0

    def _browser_alive():
        if _browser is None or _page is None:
            return False
        try:
            _browser.cookies()  # real CDP round trip
            return True
        except Exception:
            return False

    def _restart_browser():
        global _browser, _page
        log.warning("worker: browser dead, restarting...")
        try:
            if _browser is not None:
                _browser.close()
        except Exception:
            pass
        opts2 = build_launch_options(_cfg, headless_override=False)
        _browser = _playwright.chromium.launch_persistent_context(
            user_data_dir=_cfg["browser"]["profile_dir"],
            **opts2,
        )
        _page = _browser.pages[0] if _browser.pages else _browser.new_page()
        try:
            navigate_chat_url(_page, _cfg, log_prefix="[keepalive-restart] ")
        except Exception as e:
            log.warning("keepalive restart navigation failed: %s", e)
        log.info("worker: browser restarted")

    while not _worker_stop.is_set():
        try:
            try:
                job = _worker_queue.get(timeout=1.0)
            except queue.Empty:
                job = None

            if job is not None:
                fn, args, kwargs, result_q = job
                if not _browser_alive():
                    try:
                        _restart_browser()
                    except Exception as e:
                        log.error("restart_browser failed: %s", e)
                        result_q.put(("err", e))
                        continue
                try:
                    res = fn(*args, **kwargs)
                    result_q.put(("ok", res))
                except Exception as e:
                    log.exception("worker job failed: %s", e)
                    result_q.put(("err", e))
                    if not _browser_alive():
                        try: _restart_browser()
                        except Exception as ee: log.error("restart after job fail: %s", ee)

            now = time.time()
            if not _keepalive_stop.is_set() and (now - last_keepalive) >= keepalive_interval:
                last_keepalive = now
                try:
                    if not _browser_alive():
                        _restart_browser()
                    cur_url = _page.url or ""
                    if not cur_url or "meituan" not in cur_url:
                        try:
                            navigate_chat_url(_page, _cfg, log_prefix="[keepalive-url] ")
                            cur_url = _page.url or ""
                        except Exception as e:
                            log.warning("keepalive navigate chat failed: %s", e)
                    if "meituan" in cur_url:
                        on_im = ("imworkbench" in cur_url) or ("/im/" in cur_url)
                        if (now - last_refresh) >= refresh_interval:
                            try:
                                _page.reload(wait_until="domcontentloaded", timeout=60000)
                                last_refresh = now
                                cur_url = _page.url or cur_url
                                log.info("keepalive: refreshed page url=%s", cur_url[:100])
                            except Exception as e:
                                log.warning("keepalive refresh failed: %s", e)
                        if (now - last_export) >= export_interval:
                            try:
                                count = export_cookies(_browser, _cfg)
                                last_export = now
                                _status_last_export_ts = now
                                log.info("keepalive: exported %d cookies (url=%s)", count, cur_url[:80])
                            except Exception as e:
                                log.warning("keepalive export failed: %s", e)
                        _write_status(logged_in=on_im, url=cur_url, last_export_ts=_status_last_export_ts)
                    else:
                        log.warning("keepalive: not on meituan url=%s", cur_url[:100])
                        _write_status(logged_in=False, url=cur_url, last_export_ts=_status_last_export_ts, manual_login_needed=True)
                except Exception as e:
                    log.error("keepalive error: %s", e)
        except Exception as e:
            log.exception("worker loop error: %s", e)

    log.info("playwright worker: stopping")
    try:
        if _browser is not None:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass


def _ensure_worker():
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_ready.clear()
    _worker_thread = threading.Thread(target=_worker_run, daemon=True, name="pw-worker")
    _worker_thread.start()
    _worker_ready.wait(timeout=60)


def _submit(fn, *args, **kwargs):
    """Submit a callable to the worker thread and wait for its result."""
    _ensure_worker()
    if _browser is None and not _worker_ready.is_set():
        raise HTTPException(500, "playwright worker not ready")
    rq: "queue.Queue" = queue.Queue(maxsize=1)
    _worker_queue.put((fn, args, kwargs, rq))
    status, payload = rq.get()
    if status == "err":
        raise HTTPException(500, f"worker error: {payload}")
    return payload


def _stop_keepalive():
    _keepalive_stop.set()



INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>\u7f8e\u56e2 \u767b\u5f55\u6d4f\u89c8\u5668</title>
<style>body{font-family:-apple-system,Segoe UI,sans-serif;background:#0f172a;color:#e2e8f0;margin:0}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:16px;margin-bottom:12px}
button{background:#2563eb;border:0;color:#fff;padding:8px 16px;border-radius:4px;cursor:pointer;margin-right:6px}
button.green{background:#16a34a}
button.danger{background:#dc2626}
.scr{border:1px solid #334155;background:#000;padding:8px;border-radius:4px;max-width:100%}
.muted{color:#94a3b8;font-size:12px}
.url{word-break:break-all;color:#93c5fd}
.ok{color:#34d399}.bad{color:#f87171}
iframe{border:0;background:#000;border-radius:4px}
.barcode{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
</style></head>
<body><div class="wrap">
<h1>\u7f8e\u56e2 \u767b\u5f55\u6d4f\u89c8\u5668 (5901)</h1>
<div class="card">
  <div class="muted">\u5f53\u524d URL</div>
  <div class="url" id="curUrl">--</div>
  <div style="margin-top:10px" class="barcode">
    <button onclick="nav('login')">\u53bb\u767b\u5f55\u9875</button>
    <button onclick="nav('chat')">\u53bb IM \u5de5\u4f5c\u53f0</button>
    <button onclick="nav('reload')">\u5237\u65b0</button>
    <button onclick="snap()">\u622a\u56fe</button>
    <button onclick="connectVnc()">\u8fde\u63a5 VNC \u753b\u9762</button>
    <button class="green" onclick="exportNow()">\u5bfc\u51fa Cookie</button>
  </div>
</div>
<div class="card">
  <div class="muted">Cookie \u72b6\u6001</div>
  <div id="cookieStatus">--</div>
</div>
<div class="card">
  <div class="muted">\u5b9e\u65f6\u753b\u9762\uff08\u670d\u52a1\u5668\u4e0a Chromium\uff0c\u53ef\u70b9\u51fb/\u8f93\u5165\uff09</div>
  <iframe id="vnc" width="100%" height="640" style="display:none" allow="fullscreen"></iframe>
  <div id="vncHint" class="muted" style="margin-top:6px">\u70b9\u51fb"\u8fde\u63a5 VNC \u753b\u9762"\u6309\u94ae\u540e\uff0c\u5728\u6b64\u5904\u663e\u793a\u8fdc\u7a0b\u6d4f\u89c8\u5668\u5b9e\u65f6\u753b\u9762\u3002</div>
</div>
<div class="card">
  <div class="muted">\u622a\u56fe\u5feb\u7167\uff08\u70b9"\u622a\u56fe"\u6309\u94ae\u751f\u6210\uff09</div>
  <img class="scr" id="snap" style="display:none">
</div>
<div class="muted">\u767b\u5f55 IM \u5de5\u4f5c\u53f0\u540e\uff0ccookie \u4f1a\u81ea\u52a8\u5bfc\u51fa\u5e76\u4fdd\u6d3b\u3002bot \u673a\u5668\u4eba\u53ef\u540c\u65f6\u8fd0\u884c\uff08\u65e0\u9700\u505c\u672c\u670d\u52a1\uff09\u3002</div>
</div>
<script>
const TOKEN = new URL(location.href).searchParams.get('token') || '';
async function api(p, opts){
  const u = new URL(p, location.origin); u.searchParams.set('token', TOKEN);
  const r = await fetch(u, opts||{}); return r.json();
}
async function nav(kind){
  const r = await api('/api/nav?kind='+kind, {method:'POST'}); refresh();
}
async function refresh(){
  const r = await api('/api/info'); document.getElementById('curUrl').textContent = r.url || '(\u672a\u6253\u5f00)';
  refreshCookie();
}
async function snap(){
  const r = await api('/api/snap'); if (r.image){ const img=document.getElementById('snap'); img.src='data:image/png;base64,'+r.image; img.style.display='block'; }
}
async function exportNow(){
  const r = await api('/api/export-cookies', {method:'POST'}); refreshCookie();
}
async function refreshCookie(){
  try {
    const r = await api('/api/cookie-status');
    const el = document.getElementById('cookieStatus');
    if (r.exists) {
      el.innerHTML = '<span class="ok">Cookie \u5df2\u5bfc\u51fa</span> (' + r.cookie_count + ' \u4e2a, ' + r.age_str + ') <span class="muted">\u4fdd\u6d3b\u4e2d</span>';
    } else {
      el.innerHTML = '<span class="bad">Cookie \u672a\u5bfc\u51fa</span> <span class="muted">\u8bf7\u5148\u767b\u5f55\u540e\u70b9"\u5bfc\u51fa Cookie"</span>';
    }
  } catch(e) {}
}
function connectVnc(){
  const f = document.getElementById('vnc');
  const params = '?host=' + encodeURIComponent(location.hostname)
    + '&port=' + encodeURIComponent(location.port || '443')
    + '&path=' + encodeURIComponent('vnc/ws')
    + '&autoconnect=true&resize=scale&reconnect=true&show_dot=true';
  f.src = '/novnc/vnc_lite.html' + params;
  f.style.display = 'block';
  document.getElementById('vncHint').textContent = 'VNC \u5df2\u8fde\u63a5\u3002\u53ef\u76f4\u63a5\u70b9\u51fb\u753b\u9762\u3001\u7528\u952e\u76d8\u8f93\u5165\u3002';
}
refresh(); setInterval(refresh, 4000);
</script></body></html>
"""
@app.on_event("startup")
def _on_startup():
    try:
        _ensure_worker()
    except Exception as e:
        log.error("startup ensure_worker failed: %s", e)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


# === noVNC ===
if os.path.isdir(NOVNC_DIR):
    app.mount("/novnc", StaticFiles(directory=NOVNC_DIR, html=True), name="novnc")


@app.websocket("/vnc/ws")
async def vnc_ws(ws: WebSocket) -> None:
    requested = ws.headers.get("sec-websocket-protocol", "")
    subprotocol = "binary" if "binary" in requested.lower() else None
    await ws.accept(subprotocol=subprotocol)
    try:
        reader, writer = await asyncio.open_connection(VNC_RFB_HOST, VNC_RFB_PORT)
    except Exception as e:
        log.error("vnc_ws: cannot connect to RFB %s:%d err=%s", VNC_RFB_HOST, VNC_RFB_PORT, e)
        await ws.close(code=1011, reason=f"vnc backend down: {e}")
        return

    async def client_to_rfb() -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.receive":
                    data = msg.get("bytes")
                    if data is None:
                        continue
                    writer.write(data)
                    await writer.drain()
                elif msg["type"] == "websocket.disconnect":
                    break
        except Exception as e:
            log.debug("vnc_ws client_to_rfb done: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def rfb_to_client() -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception as e:
            log.debug("vnc_ws rfb_to_client done: %s", e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    await asyncio.gather(client_to_rfb(), rfb_to_client())


@app.get("/api/info")
def info(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    def job():
        return {"url": (_page.url if _page else None)}
    try:
        return _submit(job)
    except Exception as e:
        return {"url": None, "error": str(e)}



@app.post("/api/nav")
def nav(kind: str, token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    meituan = _cfg.get("meituan", {}) or {}
    def job():
        if kind == "login":
            _page.goto(meituan.get("login_url", "https://e.waimai.meituan.com/"))
        elif kind == "chat":
            navigate_chat_url(_page, _cfg)
        elif kind == "reload":
            _page.reload()
        else:
            raise HTTPException(400, "bad kind")
        if _browser:
            try:
                export_cookies(_browser, _cfg)
            except Exception:
                pass
        return {"ok": True, "url": _page.url}
    return _submit(job)



@app.get("/api/snap")
def snap(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    def job():
        png = _page.screenshot(full_page=False, type="png")
        return {"image": base64.b64encode(png).decode("ascii"), "url": _page.url}
    return _submit(job)



@app.post("/api/export-cookies")
def export_now(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    def job():
        if _browser is None:
            raise HTTPException(400, "browser not started")
        return {"ok": True, "cookie_count": export_cookies(_browser, _cfg)}
    return _submit(job)



@app.get("/api/cookie-status")
def cookie_status(token: str = Query(...)) -> Dict[str, Any]:
    """?? cookie ?????"""
    _check_token(token)
    exists = cookie_file_exists(_cfg)
    age = cookie_file_age_seconds(_cfg)
    age_str = ""
    cookie_count = 0
    if exists:
        try:
            from cookie_sync import cookie_file_path
            import json
            data = json.load(cookie_file_path(_cfg).open("r", encoding="utf-8"))
            cookie_count = data.get("cookie_count", 0)
        except Exception:
            pass
        if age is not None:
            if age < 60:
                age_str = f"{int(age)}??"
            elif age < 3600:
                age_str = f"{int(age/60)}???"
            else:
                age_str = f"{age/3600:.1f}???"
    return {"exists": exists, "age_seconds": age, "age_str": age_str, "cookie_count": cookie_count}


def main() -> int:
    global _cfg, VNC_RFB_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None)
    args = parser.parse_args()
    _cfg = load_config(args.config)
    server = _cfg.get("server", {}) or {}
    try:
        VNC_RFB_PORT = int(server.get("vnc_port") or VNC_RFB_PORT)
    except (TypeError, ValueError):
        pass
    host = args.host or server.get("remote_browser_host", "0.0.0.0")
    port = int(args.port or server.get("remote_browser_port", 5901))
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
