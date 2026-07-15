"""Meituan bot master admin dashboard."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from auth_ticket import DEFAULT_TICKET_TTL_SECONDS, issue_ticket

MASTER_TOKEN = os.environ.get("MASTER_ADMIN_TOKEN", "")
PUBLIC_HOST = os.environ.get("MEITUAN_PUBLIC_HOST", "")

SHOPS = [
    {
        "id": "shop1",
        "name": "北三环路一段店",
        "root": Path("/home/ubuntu/meituan-reply-bot"),
        "admin_external_port": 43579,
        "browser_external_port": 41171,
        "bot_unit": "meituan-reply-bot.service",
        "admin_unit": "meituan-reply-bot-admin.service",
        "browser_unit": "meituan-capture-meituan-reply-bot.service",
    },
    {
        "id": "shop2",
        "name": "人民南路一段店",
        "root": Path("/home/ubuntu/meituan-reply-bot-shop2"),
        "admin_external_port": 30097,
        "browser_external_port": 33941,
        "bot_unit": "meituan-reply-bot-shop2.service",
        "admin_unit": "meituan-reply-bot-admin-shop2.service",
        "browser_unit": "meituan-capture-meituan-reply-bot-shop2.service",
    },
]

app = FastAPI(title="美团机器人统一主管理台")


def _check_token(token: str) -> None:
    if not MASTER_TOKEN or MASTER_TOKEN == "YOUR_MASTER_TOKEN_HERE":
        raise HTTPException(503, "master token not configured")
    if not token or not hmac.compare_digest(token, MASTER_TOKEN):
        raise HTTPException(401, "invalid master token")


def _run(cmd: List[str], timeout: int = 20) -> Dict[str, Any]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"ok": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def _load_shop_config(root: Path) -> Dict[str, Any]:
    config_path = root / "config.yaml"
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _service_status(unit: str) -> Dict[str, Any]:
    result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
    active = result.stdout.strip() == "active"
    show = subprocess.run(
        ["systemctl", "show", unit, "--property=MainPID,MemoryCurrent,ActiveEnterTimestamp,NRestarts", "--no-pager"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    raw: Dict[str, str] = {}
    for line in (show.stdout or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            raw[key] = value
    return {"active": active, "raw": raw}


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds / 60)}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    return f"{seconds / 86400:.1f}天"


def _cookie_status(root: Path) -> Dict[str, Any]:
    cfg = _load_shop_config(root)
    sys.path.insert(0, str(root))
    try:
        from cookie_sync import cookie_file_age_seconds, cookie_file_exists, cookie_file_path
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass

    exists = cookie_file_exists(cfg)
    age = cookie_file_age_seconds(cfg)
    cookie_count = 0
    exported_at = ""
    if exists:
        try:
            data = json.load(cookie_file_path(cfg).open("r", encoding="utf-8"))
            cookie_count = int(data.get("cookie_count", 0) or 0)
            exported_at = str(data.get("export_time_str", "") or "")
        except Exception:
            pass

    if not exists:
        status = "none"
        status_text = "未导出"
    elif age is not None and age < 20 * 3600:
        status = "valid"
        status_text = "有效"
    elif age is not None and age < 24 * 3600:
        status = "warn"
        status_text = "即将到期"
    else:
        status = "expired"
        status_text = "可能失效"

    return {
        "exists": exists,
        "status": status,
        "status_text": status_text,
        "cookie_count": cookie_count,
        "age_seconds": age,
        "age_display": _format_age(age),
        "exported_at": exported_at,
    }


def _business_health(root: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    profile_dir = str((cfg.get("browser", {}) or {}).get("profile_dir", "") or "")
    state_dir = Path(profile_dir).parent / "state" if profile_dir else root / "state"
    path = state_dir / "last_session.json"
    try:
        session = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        return {"ok": False, "status": "invalid", "path": str(path), "error": str(exc)}
    timestamp = float(session.get("timestamp", 0) or 0)
    url = str(session.get("url", "") or "")
    age = max(0.0, time.time() - timestamp) if timestamp else None
    lowered = url.lower()
    if not timestamp:
        status = "missing"
    elif "nopermission" in lowered:
        status = "no_permission"
    elif any(marker in lowered for marker in ("passport", "login", "signin")):
        status = "login_required"
    elif session.get("scan_status") == "mismatch":
        status = "scan_mismatch"
    elif age is not None and age > 120:
        status = "stale"
    else:
        status = "healthy"
    return {"ok": status == "healthy", "status": status, "url": url, "age_seconds": age, "last_status": session.get("last_status", ""), "path": str(path)}


def _meminfo() -> Dict[str, Any]:
    values: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith(("MemTotal:", "MemAvailable:", "MemFree:", "Buffers:", "Cached:")):
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
    except Exception:
        return {}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(total - available, 0)
    percent = round((used / total) * 100, 1) if total else 0
    cached = values.get("Cached", 0) + values.get("Buffers", 0)
    return {
        "total_kb": total,
        "available_kb": available,
        "free_kb": values.get("MemFree", 0),
        "cached_kb": cached,
        "used_percent": percent,
        "total_gb": round(total / 1024 / 1024, 1) if total else 0,
        "available_gb": round(available / 1024 / 1024, 1) if available else 0,
        "cached_gb": round(cached / 1024 / 1024, 1),
    }


def _uptime() -> str:
    try:
        seconds = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        if days:
            return f"{days}天{hours}小时"
        if hours:
            return f"{hours}小时{minutes}分钟"
        return f"{minutes}分钟"
    except Exception:
        return "未知"


def _tail_file(path: Path, lines: int = 60) -> str:
    if not path.exists():
        return ""
    result = subprocess.run(["tail", "-n", str(lines), str(path)], capture_output=True, text=True, timeout=5)
    out = result.stdout.strip()
    return "\n".join(reversed(out.splitlines())) if out else ""


def _journal_tail(unit: str, lines: int = 60) -> str:
    result = subprocess.run(
        ["journalctl", "-u", unit, "--no-pager", "-n", str(lines), "-o", "short-iso"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    out = result.stdout.strip()
    return "\n".join(reversed(out.splitlines())) if out else ""


def _overview_logs() -> Dict[str, str]:
    return {
        "master": _journal_tail("meituan-master-admin.service", 50),
        "shop1_bot": _tail_file(Path("/home/ubuntu/meituan-reply-bot/logs/bot.log"), 80),
        "shop2_bot": _tail_file(Path("/home/ubuntu/meituan-reply-bot-shop2/logs/bot.log"), 80),
    }


def _shop_payload(shop: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _load_shop_config(shop["root"])
    server = cfg.get("server", {}) or {}
    replies = cfg.get("replies", {}) or {}
    return {
        "id": shop["id"],
        "name": shop["name"],
        "admin_url": f"http://{PUBLIC_HOST}:{shop['admin_external_port']}",
        "browser_url": f"http://{PUBLIC_HOST}:{shop['browser_external_port']}",
        "bot_unit": shop["bot_unit"],
        "admin_unit": shop["admin_unit"],
        "browser_unit": shop["browser_unit"],
        "bot": _service_status(shop["bot_unit"]),
        "admin": _service_status(shop["admin_unit"]),
        "browser": _service_status(shop["browser_unit"]),
        "cookie": _cookie_status(shop["root"]),
        "business_health": _business_health(shop["root"], cfg),
        "first_message": replies.get("first_message", ""),
        "fallback": replies.get("fallback", ""),
        "rules": replies.get("rules", []) or [],
    }


@app.get("/api/shop/{shop_id}/open/{target}")
def api_open_shop(shop_id: str, target: str, token: str = Query(...)) -> RedirectResponse:
    _check_token(token)
    shop = next((item for item in SHOPS if item["id"] == shop_id), None)
    if not shop or target not in ("admin", "browser"):
        raise HTTPException(404, "shop target not found")
    cfg = _load_shop_config(shop["root"])
    secret = str((cfg.get("server", {}) or {}).get("auth_token", "") or "")
    if not secret:
        raise HTTPException(500, "shop auth token not configured")
    ticket = issue_ticket(secret, shop_id, target, ttl=DEFAULT_TICKET_TTL_SECONDS)
    port = shop["admin_external_port"] if target == "admin" else shop["browser_external_port"]
    return RedirectResponse(f"http://{PUBLIC_HOST}:{port}/auth/exchange?ticket={ticket}", status_code=302)


@app.get("/api/overview")
def api_overview(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    shops = [_shop_payload(shop) for shop in SHOPS]
    alerts = []
    for shop in shops:
        if not shop["bot"]["active"]:
            alerts.append(f"{shop['name']} 机器人离线")
        elif not shop["business_health"]["ok"]:
            alerts.append(f"{shop['name']} 业务异常：{shop['business_health']['status']}")
        if shop["cookie"]["status"] in ("none", "expired"):
            alerts.append(f"{shop['name']} Cookie 需要重新导出")
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": _uptime(),
        "memory": _meminfo(),
        "shops": shops,
        "alerts": alerts,
        "logs": _overview_logs(),
    }


@app.get("/api/shop/{shop_id}")
def api_shop_detail(shop_id: str, token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    shop = next((item for item in SHOPS if item["id"] == shop_id), None)
    if not shop:
        raise HTTPException(404, "shop not found")
    return _shop_payload(shop)


@app.post("/api/cleanup/light")
def api_cleanup_light(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    before = _meminfo()
    result = _run(["sudo", "-n", "/usr/local/sbin/meituan-drop-caches"], timeout=20)
    after = _meminfo()
    return {"ok": result["ok"], "mode": "light", "before": before, "after": after, "stdout": result["stdout"], "stderr": result["stderr"]}


@app.post("/api/cleanup/deep")
def api_cleanup_deep(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    before = _meminfo()
    steps = []
    browser_units = [shop["browser_unit"] for shop in SHOPS]
    active_browsers = [unit for unit in browser_units if _service_status(unit)["active"]]
    if active_browsers:
        raise HTTPException(409, "stop active noVNC sessions from the shop admin before deep cleanup")
    bot_units = [shop["bot_unit"] for shop in SHOPS]
    admin_units = [shop["admin_unit"] for shop in SHOPS]

    for unit in bot_units:
        steps.append({"step": f"stop {unit}", **_run(["sudo", "-n", "systemctl", "stop", unit], timeout=30)})
    steps.append({"step": "drop caches", **_run(["sudo", "-n", "/usr/local/sbin/meituan-drop-caches"], timeout=20)})
    for unit in admin_units + bot_units:
        steps.append({"step": f"start {unit}", **_run(["sudo", "-n", "systemctl", "start", unit], timeout=40)})

    after = _meminfo()
    return {"ok": all(step["ok"] for step in steps), "mode": "deep", "before": before, "after": after, "steps": steps}


@app.post("/api/shop/{shop_id}/service/{unit}/{action}")
def api_service_action(shop_id: str, unit: str, action: str, token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    shop = next((item for item in SHOPS if item["id"] == shop_id), None)
    if not shop:
        raise HTTPException(404, "shop not found")
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "bad action")
    if unit not in {shop["bot_unit"], shop["admin_unit"], shop["browser_unit"]}:
        raise HTTPException(400, "bad unit")
    if unit == shop["browser_unit"]:
        raise HTTPException(409, "capture must be controlled from the shop admin")

    result = _run(["sudo", "-n", "systemctl", action, unit], timeout=20)
    return result


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美团机器人统一主管理台</title>
<style>
:root{--bg:#0a1020;--panel:#111b2e;--panel2:#17233a;--line:#27364f;--text:#e5eefb;--muted:#8ea0bb;--blue:#4f8cff;--green:#2bd576;--yellow:#f6c453;--red:#ff6575;--cyan:#48d5e8}
*{box-sizing:border-box}body{margin:0;height:100vh;overflow:hidden;background:radial-gradient(circle at 20% 10%,#19345b 0,#0a1020 34%,#070b14 100%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}.app{display:flex;height:100vh}.sidebar{width:292px;padding:22px 18px;background:rgba(13,22,37,.88);border-right:1px solid rgba(148,163,184,.18);box-shadow:20px 0 80px rgba(0,0,0,.22);display:flex;flex-direction:column}.brand{padding:8px 8px 22px}.brand h1{font-size:22px;line-height:1.1;margin:0 0 8px}.brand p{margin:0;color:var(--muted);font-size:13px}.nav{display:flex;flex-direction:column;gap:10px}.nav-item{width:100%;border:1px solid transparent;background:transparent;color:var(--text);padding:14px 14px;border-radius:12px;text-align:left;cursor:pointer;font-size:14px;transition:.15s}.nav-item:hover{background:rgba(79,140,255,.11)}.nav-item.active{background:linear-gradient(135deg,rgba(79,140,255,.26),rgba(72,213,232,.12));border-color:rgba(79,140,255,.36)}.nav-title{display:flex;justify-content:space-between;align-items:center;gap:8px}.tag{font-size:12px;padding:3px 8px;border-radius:99px;background:rgba(148,163,184,.13);color:var(--muted)}.tag.ok{background:rgba(43,213,118,.14);color:#89f6b5}.tag.warn{background:rgba(246,196,83,.15);color:#ffe09a}.tag.bad{background:rgba(255,101,117,.14);color:#ffb4bd}.main{flex:1;min-width:0;display:flex;flex-direction:column}.topbar{height:78px;display:flex;align-items:center;justify-content:space-between;padding:0 30px;border-bottom:1px solid rgba(148,163,184,.14);background:rgba(10,16,32,.48);backdrop-filter:blur(12px)}.topbar h2{font-size:21px;margin:0}.topbar .sub{font-size:13px;color:var(--muted);margin-top:5px}.content{padding:26px 30px;overflow:auto}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}.card{grid-column:span 12;background:linear-gradient(180deg,rgba(23,35,58,.92),rgba(17,27,46,.88));border:1px solid rgba(148,163,184,.15);border-radius:18px;padding:20px;box-shadow:0 18px 60px rgba(0,0,0,.22)}.card.half{grid-column:span 6}.card.third{grid-column:span 4}.card h3{margin:0 0 16px;font-size:16px}.metric{display:flex;align-items:flex-end;gap:10px}.metric strong{font-size:30px;line-height:1}.metric span{color:var(--muted);font-size:13px;margin-bottom:4px}.rows{display:grid;gap:10px}.row{display:flex;align-items:center;justify-content:space-between;gap:14px;color:var(--muted);font-size:14px}.row b{color:var(--text);font-weight:600}.progress{height:9px;background:rgba(148,163,184,.16);border-radius:999px;overflow:hidden;margin-top:14px}.bar{height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan));border-radius:999px}.actions{display:flex;gap:10px;flex-wrap:wrap}.btn{border:0;border-radius:11px;padding:10px 13px;color:#fff;background:rgba(79,140,255,.9);cursor:pointer;text-decoration:none;font-size:13px}.btn.secondary{background:rgba(148,163,184,.18);color:var(--text);border:1px solid rgba(148,163,184,.16)}.btn.danger{background:rgba(255,101,117,.88)}.table{width:100%;border-collapse:collapse}.table th,.table td{padding:11px 8px;border-bottom:1px solid rgba(148,163,184,.12);text-align:left;font-size:13px;vertical-align:top}.table th{color:#9db3d4;font-weight:500}.logbox{background:#060a12;border:1px solid rgba(148,163,184,.14);border-radius:14px;padding:14px;max-height:280px;overflow:auto;white-space:pre-wrap;font-family:Consolas,"Microsoft YaHei Mono",monospace;font-size:12px;line-height:1.55;color:#b8f7d1}.muted{color:var(--muted)}.ok{color:var(--green)}.warn{color:var(--yellow)}.bad{color:var(--red)}@media(max-width:980px){.sidebar{width:240px}.card.half,.card.third{grid-column:span 12}}
</style>
</head>
<body>
<div class="app"><aside class="sidebar"><div class="brand"><h1>美团机器人</h1><p>统一主管理台</p></div><nav id="nav" class="nav"><button class="nav-item active" data-view="overview"><span class="nav-title"><span>概览</span><span class="tag">总览</span></span></button></nav></aside><main class="main"><header class="topbar"><div><h2 id="title">概览</h2><div class="sub" id="subtitle">正在加载运行状态</div></div><div class="tag" id="timeTag">--</div></header><section class="content" id="content"></section></main></div>
<script>
const TOKEN=new URL(location.href).searchParams.get('token')||'';let current='overview';let overview=null;if(!TOKEN){document.body.innerHTML='<div style="padding:40px;color:white">缺少 ?token= 参数</div>'}
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,opt={}){const u=new URL(path,location.origin);u.searchParams.set('token',TOKEN);const r=await fetch(u,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function stateTag(active){return active?'<span class="tag ok">运行中</span>':'<span class="tag bad">已停止</span>'}function cookie(c){if(!c)return'<span class="tag warn">未知</span>';let cls=c.status==='valid'?'ok':c.status==='warn'?'warn':'bad';return `<span class="tag ${cls}">${esc(c.status_text)}</span> <span class="muted">${esc(c.age_display||'')}</span>`}
function logsHtml(logs){logs=logs||{};return `<div class="card"><h3>\u8fd0\u884c\u65e5\u5fd7 <span class="muted" style="font-size:12px;font-weight:400">\u6700\u65b0\u5728\u4e0a\uff0c\u5237\u65b0\u4e0d\u6539\u53d8\u6eda\u52a8\u4f4d\u7f6e</span></h3><div class="grid"><div class="card third" style="margin:0"><h3>\u4e3b\u7ba1\u7406\u53f0</h3><div class="logbox" data-log="master">${esc(logs.master||'\u6682\u65e0\u65e5\u5fd7')}</div></div><div class="card third" style="margin:0"><h3>\u5317\u4e09\u73af\u8def\u4e00\u6bb5\u5e97 Bot</h3><div class="logbox" data-log="shop1_bot">${esc(logs.shop1_bot||'\u6682\u65e0\u65e5\u5fd7')}</div></div><div class="card third" style="margin:0"><h3>\u4eba\u6c11\u5357\u8def\u4e00\u6bb5\u5e97 Bot</h3><div class="logbox" data-log="shop2_bot">${esc(logs.shop2_bot||'\u6682\u65e0\u65e5\u5fd7')}</div></div></div></div>`}
function saveLogScroll(){const pos={};document.querySelectorAll('.logbox[data-log]').forEach(el=>{pos[el.dataset.log]=el.scrollTop});return pos}
function restoreLogScroll(pos){if(!pos)return;document.querySelectorAll('.logbox[data-log]').forEach(el=>{if(Object.prototype.hasOwnProperty.call(pos,el.dataset.log))el.scrollTop=pos[el.dataset.log]})}
function renderNav(){let html=`<button class="nav-item ${current==='overview'?'active':''}" data-view="overview"><span class="nav-title"><span>概览</span><span class="tag">总览</span></span></button>`;(overview?.shops||[]).forEach(s=>{let cls=s.bot.active?'ok':s.admin.active?'warn':'bad';html+=`<button class="nav-item ${current===s.id?'active':''}" data-view="${s.id}"><span class="nav-title"><span>${esc(s.name)}</span><span class="tag ${cls}">${s.bot.active?'在线':'离线'}</span></span></button>`});$('nav').innerHTML=html}
function renderOverview(){const logPos=saveLogScroll();current='overview';renderNav();$('title').textContent='概览';$('subtitle').textContent='服务器、店铺、Cookie、清理与运行日志';$('timeTag').textContent=overview.time||'--';const m=overview.memory||{};const alerts=overview.alerts||[];$('content').innerHTML=`<div class="grid"><div class="card third"><h3>服务器内存</h3><div class="metric"><strong>${m.used_percent??0}%</strong><span>已使用</span></div><div class="progress"><div class="bar" style="width:${m.used_percent??0}%"></div></div><div class="row" style="margin-top:12px"><span>可用</span><b>${m.available_gb??0} GB</b></div><div class="row"><span>缓存</span><b>${m.cached_gb??0} GB</b></div><div class="actions" style="margin-top:16px"><button class="btn secondary" onclick="cleanup('light')">不影响 Bot 清理</button><button class="btn danger" onclick="cleanup('deep')">影响 Bot 大清理</button></div></div><div class="card third"><h3>运行时长</h3><div class="metric"><strong>${esc(overview.uptime||'未知')}</strong></div><div class="muted" style="margin-top:12px">系统时间：${esc(overview.time||'--')}</div></div><div class="card third"><h3>报警</h3>${alerts.length?alerts.map(a=>`<div class="row"><b class="bad">${esc(a)}</b></div>`).join(''):'<div class="metric"><strong class="ok">正常</strong><span>暂无报警</span></div>'}</div><div class="card"><h3>店铺总览</h3><div class="grid">${(overview.shops||[]).map(s=>`<div class="card half" style="margin:0"><h3>${esc(s.name)}</h3><div class="rows"><div class="row"><span>机器人</span><b>${stateTag(s.bot.active)}</b></div><div class="row"><span>管理端</span><b>${stateTag(s.admin.active)}</b></div><div class="row"><span>登录浏览器</span><b>${stateTag(s.browser.active)}</b></div><div class="row"><span>Cookie</span><b>${cookie(s.cookie)}</b></div></div><div class="actions" style="margin-top:16px"><button class="btn" onclick="openShop('${s.id}')">查看详情</button><a class="btn secondary" target="_blank" href="${location.origin}/api/shop/${s.id}/open/admin?token=${encodeURIComponent(TOKEN)}">分管理页</a><a class="btn secondary" target="_blank" href="${location.origin}/api/shop/${s.id}/open/browser?token=${encodeURIComponent(TOKEN)}">登录浏览器</a></div></div>`).join('')}</div></div>${logsHtml(overview.logs)}</div>`;restoreLogScroll(logPos)}
async function renderShop(id){current=id;renderNav();const s=await api(`/api/shop/${id}`);$('title').textContent=s.name;$('subtitle').textContent='店铺运行状态、服务控制和回复规则';$('content').innerHTML=`<div class="grid"><div class="card third"><h3>机器人</h3>${stateTag(s.bot.active)}<div class="muted" style="margin-top:10px">PID：${esc(s.bot.raw.MainPID||'-')}</div></div><div class="card third"><h3>管理端</h3>${stateTag(s.admin.active)}<div class="muted" style="margin-top:10px">PID：${esc(s.admin.raw.MainPID||'-')}</div></div><div class="card third"><h3>Cookie</h3>${cookie(s.cookie)}<div class="muted" style="margin-top:10px">数量：${s.cookie.cookie_count||0} 个<br>导出：${esc(s.cookie.exported_at||'-')}</div></div><div class="card"><h3>服务控制</h3><div class="actions"><button class="btn" onclick="svc('${s.id}','${s.bot_unit}','start')">启动机器人</button><button class="btn danger" onclick="svc('${s.id}','${s.bot_unit}','stop')">停止机器人</button><button class="btn secondary" onclick="svc('${s.id}','${s.bot_unit}','restart')">重启机器人</button><a class="btn secondary" target="_blank" href="${location.origin}/api/shop/${s.id}/open/admin?token=${encodeURIComponent(TOKEN)}">打开分管理页</a><a class="btn secondary" target="_blank" href="${location.origin}/api/shop/${s.id}/open/browser?token=${encodeURIComponent(TOKEN)}">打开登录浏览器</a></div></div><div class="card"><h3>回复策略</h3><div class="rows"><div class="row"><span>首条欢迎</span><b>${esc(s.first_message||'-')}</b></div><div class="row"><span>保底回复</span><b>${esc(s.fallback||'-')}</b></div></div><table class="table"><thead><tr><th>规则</th><th>关键词</th><th>回复内容</th></tr></thead><tbody>${(s.rules||[]).map(r=>`<tr><td>${esc(r.name||'-')}</td><td>${esc((r.keywords||[]).join('、'))}</td><td>${esc(r.reply||'-')}</td></tr>`).join('')}</tbody></table></div></div>`}
function openShop(id){renderShop(id).catch(showError)}async function svc(shopId,unit,action){const r=await api(`/api/shop/${shopId}/service/${encodeURIComponent(unit)}/${action}`,{method:'POST'});alert(r.ok?'操作成功':'操作失败：'+(r.stderr||r.stdout||''));await refresh();if(current!=='overview')await renderShop(current)}async function cleanup(mode){if(mode==='deep'&&!confirm('影响 Bot 大清理会停止 bot/browser，再清理缓存并重新启动 bot。确定执行吗？'))return;const label=mode==='deep'?'影响 Bot 大清理':'不影响 Bot 清理';const r=await api(`/api/cleanup/${mode}`,{method:'POST'});alert(`${label}${r.ok?'完成':'失败'}\n清理前可用内存：${r.before?.available_gb??'-'} GB\n清理后可用内存：${r.after?.available_gb??'-'} GB`);await refresh()}function showError(e){$('content').innerHTML=`<div class="card"><h3 class="bad">加载失败</h3><div class="muted">${esc(e.message)}</div></div>`}
$('nav').addEventListener('click',e=>{const b=e.target.closest('.nav-item');if(!b)return;b.dataset.view==='overview'?renderOverview():openShop(b.dataset.view)});async function refresh(){overview=await api('/api/overview');renderNav();if(current==='overview')renderOverview()}refresh().catch(showError);setInterval(()=>refresh().catch(()=>{}),10000);
</script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
def index(token: str = Query(...)) -> HTMLResponse:
    _check_token(token)
    return HTMLResponse(INDEX_HTML)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5904)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
