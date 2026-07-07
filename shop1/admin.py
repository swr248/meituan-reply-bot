"""管理台后端 + 中文网页。

功能：
- 查看机器人/登录浏览器/系统/机器人内存状态
- 启动/停止 systemd 服务
- 编辑/新增/删除关键词规则
- 测试规则匹配
- 查看最近日志
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

import yaml
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from browser_common import load_config, log
from cookie_sync import cookie_file_exists, cookie_file_age_seconds, cookie_file_path
from rules import decide_reply

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _runtime_state_dir() -> Path:
    try:
        cfg = load_config(CONFIG_PATH)
        state_cfg = (cfg.get("state", {}) or {}).get("dir")
        if state_cfg:
            return Path(state_cfg)
        profile_dir = (cfg.get("browser", {}) or {}).get("profile_dir", "")
        if profile_dir:
            return Path(profile_dir).parent / "state"
    except Exception:
        pass
    return STATE_DIR


def _token_fingerprint(value: str) -> str:
    if not value:
        return "???"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _server_tokens(server: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    primary = str(server.get("auth_token", "") or "")
    if primary:
        tokens.append(primary)
    for item in server.get("legacy_auth_tokens", []) or []:
        value = str(item or "")
        if value and value not in tokens:
            tokens.append(value)
    return tokens


def _current_auth_token(server: Dict[str, Any]) -> str:
    return str(server.get("auth_token", "") or "")


def _token_compat_count(server: Dict[str, Any]) -> int:
    return len([x for x in (server.get("legacy_auth_tokens", []) or []) if str(x or "")])


PUBLIC_ADMIN_PORTS = {3003: 43579, 3004: 30097}


def _public_admin_url(server: Dict[str, Any]) -> str:
    configured = str(server.get("admin_public_url", "") or "").rstrip("/")
    if configured:
        return configured
    try:
        internal_port = int(server.get("admin_port") or 0)
    except (TypeError, ValueError):
        internal_port = 0
    public_port = PUBLIC_ADMIN_PORTS.get(internal_port)
    if not public_port:
        return ""
    browser_url = str(server.get("remote_browser_public_url", "") or "")
    parsed = urlparse(browser_url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "103.236.96.82"
    return f"{scheme}://{host}:{public_port}"

app = FastAPI(title="美团外卖自动回复机器人 管理台")

CONFIG_PATH = ROOT / "config.yaml"
BACKUP_DIR = ROOT / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 服务名常量（与 systemd 单元名一致）
def _instance_suffix():
    try:
        cfg = load_config(CONFIG_PATH)
        return (cfg.get("server", {}) or {}).get("instance_suffix", "") or ""
    except Exception:
        return ""
_SUF = _instance_suffix()
SVC_BOT = f"meituan-reply-bot{_SUF}.service"
SVC_ADMIN = f"meituan-reply-bot-admin{_SUF}.service"
SVC_BROWSER = f"meituan-browser-control{_SUF}.service"


# ---------- 鉴权 ----------
def _check_token(token: Optional[str]) -> None:
    cfg = load_config(CONFIG_PATH)
    server = cfg.get("server", {}) or {}
    expected = _current_auth_token(server)
    valid_tokens = _server_tokens(server)
    if not expected or expected == "CHANGE_ME_TO_A_LONG_RANDOM_STRING":
        raise HTTPException(500, "auth_token not set in config.yaml")
    if not token or token not in valid_tokens:
        raise HTTPException(401, "invalid token")


# ---------- systemd 包装 ----------
def _sudo_systemctl(action: str, unit: str) -> Dict[str, Any]:
    systemctl_path = subprocess.run(["which", "systemctl"], capture_output=True, text=True).stdout.strip() or "/bin/systemctl"
    cmd = ["sudo", "-n", systemctl_path, action, unit]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {
            "ok": r.returncode == 0,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "exit": r.returncode,
        }
    except Exception as e:
        return {"ok": False, "stderr": str(e), "exit": -1}


def _service_status(unit: str) -> Dict[str, Any]:
    systemctl_path = subprocess.run(["which", "systemctl"], capture_output=True, text=True).stdout.strip() or "/bin/systemctl"
    r = subprocess.run([systemctl_path, "is-active", unit], capture_output=True, text=True, timeout=8)
    active = (r.stdout.strip() == "active")
    show = subprocess.run(
        [systemctl_path, "show", unit, "--property=MainPID,MemoryCurrent,MemoryPeak,NRestarts,ActiveEnterTimestamp", "--no-pager"],
        capture_output=True, text=True, timeout=8,
    )
    info: Dict[str, str] = {}
    for line in (show.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    return {"active": active, "raw": info}


# ---------- API ----------
@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "ts": time.time()}


@app.get("/api/status")
def api_status(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    return {
        "bot": _service_status(SVC_BOT),
        "admin": _service_status(SVC_ADMIN),
        "browser": _service_status(SVC_BROWSER),
        "system_mem": _system_mem(),
    }


@app.post("/api/service/{action}")
def api_service(action: str, token: str = Query(...), unit: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "bad action")
    if unit not in (SVC_BOT, SVC_BROWSER):
        raise HTTPException(400, "bad unit")

    return _sudo_systemctl(action, unit)


@app.get("/api/remote-browser-url")
def api_remote_browser_url(token: str = Query(...)) -> Dict[str, Any]:
    """返回登录浏览器的公网入口 URL（带 token）。"""
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    server = cfg.get("server", {}) or {}
    base = server.get("remote_browser_public_url", "").rstrip("/")
    auth = _current_auth_token(server)
    if not base or not auth:
        raise HTTPException(500, "remote_browser_public_url or auth_token not configured")
    return {"url": f"{base}/?token={auth}", "token_fingerprint": _token_fingerprint(auth), "legacy_tokens": _token_compat_count(server)}


@app.get("/api/units")
def api_units(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    return {"bot": SVC_BOT, "admin": SVC_ADMIN, "browser": SVC_BROWSER}


@app.get("/api/instance")
def api_instance(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    server = cfg.get("server", {}) or {}
    suffix = server.get("instance_suffix", "") or ""
    name = "shop2 人民南路" if suffix == "-shop2" else "shop1 北三环"
    return {
        "name": name,
        "suffix": suffix,
        "admin_port": server.get("admin_port"),
        "admin_url": _public_admin_url(server),
        "browser_url": (server.get("remote_browser_public_url", "") or "").rstrip("/"),
        "bot_unit": SVC_BOT,
        "browser_unit": SVC_BROWSER,
        "token_fingerprint": _token_fingerprint(_current_auth_token(server)),
        "legacy_token_count": _token_compat_count(server),
        "security_note": "管理端已关闭明文访问日志，避免 token 出现在 admin.log。",
    }

def _read_keepalive_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """读取 remote_browser 的 keepalive 状态（state/cookie_status.json）。"""
    state_dir = _state_dir_for_config(ROOT, cfg)
    sf = Path(state_dir) / "cookie_status.json"
    out: Dict[str, Any] = {
        "exists": False,
        "logged_in": False,
        "manual_login_needed": False,
        "last_check_ts": 0.0,
        "last_url": "",
        "last_export_ts": 0.0,
        "last_error": "",
        "age_seconds": None,
    }
    if not sf.exists():
        return out
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        out["exists"] = True
        out["logged_in"] = bool(data.get("logged_in", False))
        out["manual_login_needed"] = bool(data.get("manual_login_needed", False))
        out["last_check_ts"] = float(data.get("last_check_ts", 0) or 0)
        out["last_url"] = str(data.get("last_url", "") or "")
        out["last_export_ts"] = float(data.get("last_export_ts", 0) or 0)
        out["last_error"] = str(data.get("last_error", "") or "")
        out["age_seconds"] = max(0.0, time.time() - out["last_check_ts"])
    except Exception:
        return out
    return out


def _cookie_status_for_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    exists = cookie_file_exists(cfg)
    age = cookie_file_age_seconds(cfg)
    cookie_count = 0
    export_time_str = ""
    if exists:
        try:
            data = json.load(cookie_file_path(cfg).open("r", encoding="utf-8"))
            if isinstance(data, dict):
                cookie_count = data.get("cookie_count", 0)
                export_time_str = data.get("export_time_str", "")
        except Exception:
            pass
    ka = _read_keepalive_status(cfg)
    manual_needed = ka.get("manual_login_needed", False)
    on_im = ka.get("logged_in", False)
    has_service = ka.get("exists", False)
    ka_age = ka.get("age_seconds")
    ka_fresh = (ka_age is not None and ka_age < 300)
    age_hours = (age / 3600) if age else 0
    age_days = (age / 86400) if age else 0
    age_display = ""
    if age is not None:
        if age < 60:
            age_display = f"{int(age)}秒"
        elif age < 3600:
            age_display = f"{int(age/60)}分钟"
        elif age < 86400:
            age_display = f"{age/3600:.1f}小时"
        else:
            age_display = f"{age_days:.1f}天"
    if manual_needed:
        status = "login_required"
        status_text = "需重新登录"
    elif not exists:
        status = "none"
        status_text = "未导出"
    elif age_hours < 12:
        status = "valid"
        status_text = f"有效（最近导出 {age_display}）"
    elif age_hours < 24:
        status = "renewing"
        status_text = "即将到期（{ago}）".format(ago=age_display)
    else:
        status = "stale"
        status_text = "可能失效（{ago}）".format(ago=age_display)
    return {
        "exists": exists,
        "status": status,
        "status_text": status_text,
        "cookie_count": cookie_count,
        "age_seconds": age,
        "age_display": age_display,
        "age_days": round(age_days, 1) if age_days else 0,
        "export_time": export_time_str,
        "keepalive": ka,
        "auto_active": has_service and on_im and not manual_needed,
    }


def _shop_entry(name: str, root: Path) -> Dict[str, Any]:
    cfg_path = root / "config.yaml"
    cfg = load_config(cfg_path)
    server = cfg.get("server", {}) or {}
    suffix = server.get("instance_suffix", "") or ""
    return {
        "name": name,
        "root": str(root),
        "admin_port": server.get("admin_port"),
        "admin_url": _public_admin_url(server),
        "remote_browser_url": (server.get("remote_browser_public_url", "") or "").rstrip("/"),
        "token_fingerprint": _token_fingerprint(_current_auth_token(server)),
        "legacy_token_count": _token_compat_count(server),
        "link_token": _current_auth_token(server),
        "bot_unit": f"meituan-reply-bot{suffix}.service",
        "admin_unit": f"meituan-reply-bot-admin{suffix}.service",
        "browser_unit": f"meituan-browser-control{suffix}.service",
        "bot": _service_status(f"meituan-reply-bot{suffix}.service"),
        "admin": _service_status(f"meituan-reply-bot-admin{suffix}.service"),
        "browser": _service_status(f"meituan-browser-control{suffix}.service"),
        "cookie": _cookie_status_for_config(cfg),
    }


def _state_dir_for_config(root: Path, cfg: Dict[str, Any]) -> str:
    state_cfg = (cfg.get("state", {}) or {}).get("dir")
    if state_cfg:
        return str(Path(state_cfg))
    profile_dir = (cfg.get("browser", {}) or {}).get("profile_dir", "")
    if profile_dir:
        return str(Path(profile_dir).parent / "state")
    return str(root / "state")


def _cookie_path_for_config(cfg: Dict[str, Any]) -> str:
    try:
        return str(cookie_file_path(cfg))
    except Exception:
        return ""


def _shop_isolation_entry(name: str, root: Path) -> Dict[str, Any]:
    cfg = load_config(root / "config.yaml")
    server = cfg.get("server", {}) or {}
    browser = cfg.get("browser", {}) or {}
    return {
        "name": name,
        "root": str(root),
        "admin_port": server.get("admin_port"),
        "admin_url": _public_admin_url(server),
        "remote_browser_port": server.get("remote_browser_port"),
        "vnc_port": server.get("vnc_port"),
        "public_browser_url": (server.get("remote_browser_public_url", "") or "").rstrip("/"),
        "profile_dir": str(browser.get("profile_dir", "") or ""),
        "state_dir": _state_dir_for_config(root, cfg),
        "cookie_path": _cookie_path_for_config(cfg),
        "token_fingerprint": _token_fingerprint(_current_auth_token(server)),
        "legacy_token_count": _token_compat_count(server),
    }


def _duplicates(items: List[Dict[str, Any]], field: str) -> List[str]:
    seen: Dict[str, int] = {}
    for item in items:
        value = str(item.get(field) or "")
        if not value:
            continue
        seen[value] = seen.get(value, 0) + 1
    return [value for value, count in seen.items() if count > 1]


def _legacy_token_summary() -> Dict[str, Any]:
    shops = []
    for name, root in (("shop1", Path("/home/ubuntu/meituan-reply-bot")), ("shop2", Path("/home/ubuntu/meituan-reply-bot-shop2"))):
        cfg = load_config(root / "config.yaml")
        server = cfg.get("server", {}) or {}
        legacy = [str(x) for x in (server.get("legacy_auth_tokens", []) or []) if str(x or "")]
        shops.append({
            "name": name,
            "root": str(root),
            "token_fingerprint": _token_fingerprint(_current_auth_token(server)),
            "legacy_count": len(legacy),
            "legacy_fingerprints": [_token_fingerprint(x) for x in legacy],
        })
    return {"shops": shops, "total_legacy": sum(s["legacy_count"] for s in shops)}


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except Exception:
            pass
    return total


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _remove_path(path: Path) -> Dict[str, Any]:
    before = _dir_size(path)
    removed = False
    try:
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
            removed = True
        elif path.is_dir():
            shutil.rmtree(path)
            removed = True
    except Exception as e:
        return {"path": str(path), "before": before, "before_text": _human_bytes(before), "removed": False, "error": str(e)}
    return {"path": str(path), "before": before, "before_text": _human_bytes(before), "removed": removed}


def _run_cmd(cmd: List[str], timeout: int = 30) -> Dict[str, Any]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "exit": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip(), "cmd": cmd}
    except Exception as e:
        return {"ok": False, "exit": -1, "stderr": str(e), "cmd": cmd}


def _maintenance_targets() -> Dict[str, Any]:
    cfg = load_config(CONFIG_PATH)
    profile_dir = Path((cfg.get("browser", {}) or {}).get("profile_dir", "") or "")
    light_paths = [ROOT / "__pycache__"]
    browser_cache_paths: List[Path] = []
    if profile_dir:
        for rel in ("Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/Service Worker/CacheStorage"):
            browser_cache_paths.append(profile_dir / rel)
    logs = list((ROOT / "logs").glob("*.log")) + list((ROOT / "logs").glob("*.log-*")) + list((ROOT / "logs").glob("*.log.*"))
    return {"logs": logs, "light_paths": light_paths, "browser_cache_paths": browser_cache_paths}


def _maintenance_snapshot() -> Dict[str, Any]:
    targets = _maintenance_targets()
    logs = [{"path": str(p), "size": p.stat().st_size if p.exists() else 0, "size_text": _human_bytes(p.stat().st_size if p.exists() else 0)} for p in targets["logs"]]
    light = [{"path": str(p), "size": _dir_size(p), "size_text": _human_bytes(_dir_size(p)), "exists": p.exists()} for p in targets["light_paths"]]
    browser = [{"path": str(p), "size": _dir_size(p), "size_text": _human_bytes(_dir_size(p)), "exists": p.exists()} for p in targets["browser_cache_paths"]]
    return {
        "logs": logs,
        "light_cache": light,
        "browser_cache": browser,
        "total_log_size": _human_bytes(sum(x["size"] for x in logs)),
        "total_light_cache_size": _human_bytes(sum(x["size"] for x in light)),
        "total_browser_cache_size": _human_bytes(sum(x["size"] for x in browser)),
        "log_policy": "logrotate: daily, rotate 7, maxsize 20M, copytruncate",
    }


@app.get("/api/maintenance/status")
def api_maintenance_status(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    return _maintenance_snapshot()


@app.post("/api/maintenance/light")
def api_maintenance_light(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    targets = _maintenance_targets()
    results = [_remove_path(path) for path in targets["light_paths"]]
    return {"ok": True, "mode": "light", "note": "不停止机器人，只清理 Python 缓存等安全文件", "results": results, "status": _maintenance_snapshot()}


@app.post("/api/maintenance/deep")
def api_maintenance_deep(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    stopped = [_sudo_systemctl("stop", SVC_BROWSER), _sudo_systemctl("stop", SVC_BOT)]
    targets = _maintenance_targets()
    cache_results = [_remove_path(path) for path in (targets["light_paths"] + targets["browser_cache_paths"])]
    logrotate = _run_cmd(["sudo", "-n", "logrotate", "-f", "/etc/logrotate.d/meituan-reply-bot"], timeout=60)
    drop_cache = _run_cmd(["sudo", "-n", "/usr/local/sbin/meituan-drop-caches"], timeout=60)
    restarted = [_sudo_systemctl("start", SVC_BOT)]
    return {
        "ok": all(x.get("ok") for x in restarted),
        "mode": "deep",
        "note": "会停止当前店铺机器人和登录浏览器，清浏览器缓存，强制轮转日志，释放系统页缓存，然后重启机器人",
        "stopped": stopped,
        "cache_results": cache_results,
        "logrotate": logrotate,
        "drop_cache": drop_cache,
        "restarted": restarted,
        "status": _maintenance_snapshot(),
    }


@app.post("/api/maintenance/all")
def api_maintenance_all(token: str = Query(...)) -> Dict[str, Any]:
    return api_maintenance_deep(token)

@app.get("/api/token-legacy-status")
def api_token_legacy_status(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    return _legacy_token_summary()


@app.post("/api/token-legacy-clear")
def api_token_legacy_clear(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    changed = []
    for name, root in (("shop1", Path("/home/ubuntu/meituan-reply-bot")), ("shop2", Path("/home/ubuntu/meituan-reply-bot-shop2"))):
        cfg_path = root / "config.yaml"
        cfg = load_config(cfg_path)
        server = cfg.get("server", {}) or {}
        legacy = server.get("legacy_auth_tokens", []) or []
        if legacy:
            backup = root / "backups" / f"config.before-clear-legacy-token.{int(time.time())}.yaml"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
            server["legacy_auth_tokens"] = []
            with cfg_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            changed.append({"name": name, "removed": len(legacy), "backup": str(backup)})
    return {"ok": True, "changed": changed, "status": _legacy_token_summary()}


@app.get("/api/isolation")
def api_isolation(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    shops = [
        _shop_isolation_entry("shop1", Path("/home/ubuntu/meituan-reply-bot")),
        _shop_isolation_entry("shop2", Path("/home/ubuntu/meituan-reply-bot-shop2")),
    ]
    checks = []
    for field in ("admin_port", "remote_browser_port", "vnc_port", "profile_dir", "state_dir", "cookie_path", "token_fingerprint"):
        dup = _duplicates(shops, field)
        level = "bad" if dup and field != "token_fingerprint" else ("warn" if dup else "ok")
        checks.append({"field": field, "level": level, "duplicates": dup})
    return {"shops": shops, "checks": checks}


@app.get("/api/shops")
def api_shops(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    shops = [
        _shop_entry("shop1 北三环", Path("/home/ubuntu/meituan-reply-bot")),
        _shop_entry("shop2 人民南路", Path("/home/ubuntu/meituan-reply-bot-shop2")),
    ]
    return {"shops": shops}


@app.get("/api/cookie-status")
def api_cookie_status(token: str = Query(...)) -> Dict[str, Any]:
    """?? cookie ?????????????????/???"""
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    return _cookie_status_for_config(cfg)


@app.get("/api/rules")
def api_get_rules(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    replies = (cfg.get("replies", {}) or {})
    return {
        "first_message": replies.get("first_message", ""),
        "fallback": replies.get("fallback", ""),
        "rules": replies.get("rules", []) or [],
    }


class RuleIn(BaseModel):
    name: str
    keywords: List[str]
    reply: str


class RulesUpdate(BaseModel):
    first_message: Optional[str] = None
    fallback: Optional[str] = None
    rules: Optional[List[RuleIn]] = None


@app.post("/api/rules")
def api_set_rules(payload: RulesUpdate, token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    cfg.setdefault("replies", {})
    if payload.first_message is not None:
        cfg["replies"]["first_message"] = payload.first_message
    if payload.fallback is not None:
        cfg["replies"]["fallback"] = payload.fallback
    if payload.rules is not None:
        cfg["replies"]["rules"] = [r.dict() for r in payload.rules]
    backup = BACKUP_DIR / f"config.{int(time.time())}.yaml"
    try:
        backup.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        backup = None
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return {"ok": True, "backup": str(backup) if backup else ""}


@app.get("/api/rules/backups")
def api_rule_backups(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    items = []
    for path in sorted(BACKUP_DIR.glob("config.*.yaml"), reverse=True)[:20]:
        try:
            ts = int(path.stem.split(".")[1])
        except Exception:
            ts = int(path.stat().st_mtime)
        items.append({"name": path.name, "path": str(path), "ts": ts, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))})
    return {"backups": items}


@app.post("/api/rules/restore")
def api_restore_rules(token: str = Query(...), name: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    if "/" in name or "\\" in name or not name.startswith("config.") or not name.endswith(".yaml"):
        raise HTTPException(400, "bad backup name")
    src = BACKUP_DIR / name
    if not src.exists():
        raise HTTPException(404, "backup not found")
    current = BACKUP_DIR / f"config.before-restore.{int(time.time())}.yaml"
    try:
        current.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    CONFIG_PATH.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return {"ok": True, "restored": name, "current_backup": str(current)}


class TestIn(BaseModel):
    message: str
    is_first_message: bool = False


@app.post("/api/rules/test")
def api_test_rule(payload: TestIn, token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    d = decide_reply(payload.message, cfg, payload.is_first_message)
    return {
        "message": payload.message,
        "rule": d.rule,
        "reply": d.reply,
        "auto_appended_ai": d.auto_appended_ai,
    }


class PromoWindowIn(BaseModel):
    start: str
    end: str


class PromoSchedulerUpdate(BaseModel):
    enabled: Optional[bool] = None
    windows: Optional[List[PromoWindowIn]] = None


@app.get("/api/promo-scheduler")
def api_get_promo(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    sched = (cfg.get("promotion_scheduler") or {})
    return {
        "enabled": bool(sched.get("enabled", False)),
        "check_interval_sec": int(sched.get("check_interval_sec", 30)),
        "windows": list(sched.get("windows") or []),
    }


@app.post("/api/promo-scheduler")
def api_set_promo(payload: PromoSchedulerUpdate, token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)

    def _hhmm(s: str) -> bool:
        try:
            h, m = s.strip().split(":")[:2]
            return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            return False

    new_windows = []
    if payload.windows is not None:
        if len(payload.windows) > 3:
            raise HTTPException(400, "最多 3 段窗口")
        for w in payload.windows:
            if not (_hhmm(w.start) and _hhmm(w.end)):
                raise HTTPException(400, f"时间格式错误：{w.start} / {w.end}，应为 HH:MM")
            new_windows.append({"start": w.start, "end": w.end})

    sched = cfg.setdefault("promotion_scheduler", {})
    if payload.enabled is not None:
        sched["enabled"] = bool(payload.enabled)
    sched.setdefault("check_interval_sec", 30)
    sched.setdefault("target_url", "https://waimaieapp.meituan.com/ad/v1/rpc?&#/subapp/isomor_sg_onestop/pages/onestop/index")
    sched.setdefault("switch_selector", ".sg-onestop-header-switch")
    sched["windows"] = new_windows

    backup = BACKUP_DIR / f"config.{int(time.time())}.yaml"
    try:
        backup.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        backup = None
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return {"ok": True, "backup": str(backup) if backup else "", "promotion_scheduler": sched}


@app.get("/api/logs")
def api_logs(token: str = Query(...), tail: int = 120) -> Dict[str, Any]:
    _check_token(token)
    log_file = ROOT / "logs" / "bot.log"
    if not log_file.exists():
        return {"lines": []}
    try:
        # 用 tail -n 避免大文件 IO
        r = subprocess.run(["tail", "-n", str(tail), str(log_file)], capture_output=True, text=True, timeout=5)
        return {"lines": r.stdout.splitlines()}
    except Exception as e:
        return {"lines": [], "error": str(e)}


KEY_LOG_PATTERNS = (
    "inbound bubble picked",
    "watermark customer=",
    "first peer msg",
    "subsequent peer msg",
    "sent welcome",
    "sent keyword",
    "sent fallback",
    "same last peer fingerprint",
    "card not pending",
    "send-debug",
    "send_reply error",
    "scan error",
    "no inbound peer message",
)


@app.get("/api/key-logs")
def api_key_logs(token: str = Query(...), tail: int = 400, limit: int = 120) -> Dict[str, Any]:
    _check_token(token)
    log_file = ROOT / "logs" / "bot.log"
    if not log_file.exists():
        return {"lines": []}
    try:
        r = subprocess.run(["tail", "-n", str(max(50, min(tail, 2000))), str(log_file)], capture_output=True, text=True, timeout=5)
        lines = []
        for line in r.stdout.splitlines():
            if any(p in line for p in KEY_LOG_PATTERNS):
                line = re.sub(r"\s+", " ", line).strip()
                lines.append(line)
        return {"lines": lines[-max(20, min(limit, 300)):]}
    except Exception as e:
        return {"lines": [], "error": str(e)}


def _tail_lines(path: Path, n: int) -> List[str]:
    if not path.exists():
        return []
    try:
        r = subprocess.run(["tail", "-n", str(n), str(path)], capture_output=True, text=True, timeout=5)
        return r.stdout.splitlines()
    except Exception:
        return []


def _recent_reply_events(limit: int = 30) -> List[Dict[str, Any]]:
    path = _runtime_state_dir() / "replies.json"
    if not path.exists():
        return []
    try:
        data = json.load(path.open("r", encoding="utf-8"))
        events = data.get("reply_events", []) if isinstance(data, dict) else []
        if not isinstance(events, list):
            return []
        return list(reversed(events[-limit:]))
    except Exception:
        return []


@app.get("/api/alerts")
def api_alerts(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    alerts: List[Dict[str, Any]] = []
    status = api_status(token)
    cookie = _cookie_status_for_config(cfg)
    if not status.get("bot", {}).get("active"):
        alerts.append({"level": "bad", "title": "机器人未运行", "detail": "bot service 当前不是 active"})
    cookie_status = cookie.get("status", "valid")
    browser_active = bool(status.get("browser", {}).get("active"))
    if cookie_status == "login_required":
        alerts.append({"level": "bad", "title": "登录账号已失效", "detail": "请手动打开登录浏览器重新登录，登录后系统会自动导出 Cookie。"})
    elif cookie_status in ("none", "stale") and not browser_active:
        try:
            r = _sudo_systemctl("start", SVC_BROWSER)
            if r.get("ok"):
                alerts.append({"level": "warn", "title": "Cookie 正在自动续期", "detail": "已启动登录浏览器，等待首次 keepalive 导出。"})
            else:
                alerts.append({"level": "bad", "title": "Cookie 未导出 / 可能失效", "detail": (cookie.get("status_text", "") + " / " + (cookie.get("age_display") or "") + " · 启动服务失败: " + (r.get("stderr") or r.get("stdout") or "unknown"))[:240]})
        except Exception as e:
            alerts.append({"level": "bad", "title": "Cookie 未导出 / 可能失效", "detail": (cookie.get("status_text", "") + " / " + (cookie.get("age_display") or "") + " · 启动异常: " + str(e))[:240]})
    elif cookie_status == "renewing":
        pass  # 保活中，不报警
    elif cookie_status == "stale" and browser_active:
        alerts.append({"level": "warn", "title": "Cookie 正在自动续期", "detail": "keepalive 运行中，下一次刷新将会更新 cookie 文件。"})
    try:
        shop_tokens = []
        legacy_shared = []
        for root in (Path("/home/ubuntu/meituan-reply-bot"), Path("/home/ubuntu/meituan-reply-bot-shop2")):
            c = load_config(root / "config.yaml")
            server = c.get("server", {}) or {}
            shop_tokens.append(server.get("auth_token", ""))
            legacy_shared.extend(server.get("legacy_auth_tokens", []) or [])
        if len(set(shop_tokens)) == 1 and shop_tokens[0]:
            alerts.append({"level": "warn", "title": "\u4e24\u5bb6\u5e97\u5171\u7528\u7ba1\u7406 token", "detail": "\u5f53\u524d\u4e3b token \u4ecd\u76f8\u540c\uff0c\u5efa\u8bae\u5207\u72ec\u7acb token"})
        if legacy_shared:
            alerts.append({"level": "warn", "title": "\u65e7 token \u517c\u5bb9\u4e2d", "detail": "\u65e7\u94fe\u63a5\u6682\u65f6\u53ef\u7528\uff1b\u786e\u8ba4\u65b0\u94fe\u63a5\u53ef\u7528\u540e\u53ef\u79fb\u9664\u65e7 token"})
    except Exception:
        pass

    events = _recent_reply_events(30)
    failed = [e for e in events if e.get("ok") is False and not str(e.get("action", "")).startswith("skip_")]
    if failed:
        alerts.append({"level": "bad", "title": "最近存在发送失败", "detail": f"最近 {len(failed)} 条回复事件失败"})
    skips = [e for e in events if str(e.get("action", "")).startswith("skip_")]
    if len(skips) >= 10:
        alerts.append({"level": "warn", "title": "跳过事件偏多", "detail": f"最近 {len(skips)} 次跳过，可能没有待回复或防重生效"})
    if not events:
        alerts.append({"level": "warn", "title": "暂无回复记录", "detail": "机器人尚未产生 reply_events，建议发一条测试消息验证"})
    if not alerts:
        alerts.append({"level": "ok", "title": "暂无明显风险", "detail": "bot / Cookie / 最近回复状态正常"})
    return {"alerts": alerts}


def _try_stop_browser_service() -> Dict[str, Any]:
    """关闭登录浏览器，节省内存。"""
    try:
        if not _service_status(SVC_BROWSER).get("active"):
            return {"stopped": False, "reason": "service 本就不运行"}
    except Exception:
        pass
    r = _sudo_systemctl("stop", SVC_BROWSER)
    return {"stopped": r.get("ok", False), "detail": r.get("stderr") or r.get("stdout") or ""}


def _try_auto_refresh_cookies() -> Dict[str, Any]:
    """尝试自动续 cookie：检查状态，如需要则启动登录浏览器，等 keepalive 导出。
    成功后默认关闭浏览器节省内存，如果需要手动登录则保留。"""
    cfg = load_config(CONFIG_PATH)
    cookie = _cookie_status_for_config(cfg)
    ka = cookie.get("keepalive", {}) or {}
    if ka.get("manual_login_needed"):
        try:
            if not _service_status(SVC_BROWSER).get("active"):
                _sudo_systemctl("start", SVC_BROWSER)
        except Exception:
            pass
        return {"ok": False, "manual_login_needed": True, "reason": "keepalive 检测到需要手动登录，已保留登录浏览器", "last_url": ka.get("last_url", "")}
    if cookie.get("status") == "valid":
        stop = _try_stop_browser_service()
        return {"ok": True, "skipped": True, "reason": "cookie 有效，已关闭登录浏览器节省内存", "cookie": cookie, "stop_result": stop}
    browser_active = False
    try:
        browser_active = bool(_service_status(SVC_BROWSER).get("active"))
    except Exception:
        browser_active = False
    if not browser_active:
        r = _sudo_systemctl("start", SVC_BROWSER)
        if not r.get("ok"):
            return {"ok": False, "reason": "启动登录浏览器失败: " + (r.get("stderr") or r.get("stdout") or "unknown")}
    deadline = time.time() + 150
    last: Dict[str, Any] = {}
    sf = Path(_state_dir_for_config(ROOT, cfg)) / "cookie_status.json"
    started_at = time.time()
    while time.time() < deadline:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            last = data
            ts = float(data.get("last_check_ts", 0) or 0)
            fresh = ts > started_at - 5
            if data.get("logged_in") and not data.get("manual_login_needed") and fresh:
                cnt = 0
                try:
                    cnt = int((json.loads(cookie_file_path(cfg).read_text(encoding="utf-8")) or {}).get("cookie_count", 0) or 0)
                except Exception:
                    pass
                stop = _try_stop_browser_service()
                return {"ok": True, "cookie_count": cnt, "reason": "已启动 keepalive 并导出新 cookie，随后关闭浏览器节省内存", "stop_result": stop}
            if data.get("manual_login_needed") and fresh:
                return {"ok": False, "manual_login_needed": True, "reason": "keepalive 报需手动登录，已保留登录浏览器", "last_url": data.get("last_url", "")}
        except Exception:
            pass
        time.sleep(2)
    return {"ok": False, "reason": "超时：keepalive 150s 内未完成 cookie 导出", "last_status": last}


@app.post("/api/auto-refresh-cookies")
def api_auto_refresh_cookies(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    return _try_auto_refresh_cookies()


@app.get("/api/diagnostics")
def api_diagnostics(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    cfg = load_config(CONFIG_PATH)
    state_path = _runtime_state_dir() / "replies.json"
    events: List[Dict[str, Any]] = []
    try:
        data = json.load(state_path.open("r", encoding="utf-8")) if state_path.exists() else {}
        raw_events = data.get("reply_events", []) if isinstance(data, dict) else []
        if isinstance(raw_events, list):
            events = list(reversed(raw_events[-30:]))
    except Exception:
        events = []
    bot_log = ROOT / "logs" / "bot.log"
    admin_log = ROOT / "logs" / "admin.log"
    key_lines = []
    for line in _tail_lines(bot_log, 800):
        if any(p in line for p in KEY_LOG_PATTERNS):
            key_lines.append(re.sub(r"\s+", " ", line).strip())
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instance": api_instance(token),
        "units": api_units(token),
        "status": api_status(token),
        "cookie": _cookie_status_for_config(cfg),
        "reply_events": events,
        "key_logs": key_lines[-120:],
        "bot_log_tail": _tail_lines(bot_log, 120),
        "admin_log_tail": _tail_lines(admin_log, 80),
        "state_path": str(state_path),
    }


@app.post("/api/redact-admin-log")
def api_redact_admin_log(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    log_file = ROOT / "logs" / "admin.log"
    if not log_file.exists():
        return {"ok": True, "changed": False, "message": "admin.log 不存在"}
    text = log_file.read_text(encoding="utf-8", errors="ignore")
    redacted = re.sub(r"token=[^\\s&\\\"']+", "token=***", text)
    changed = redacted != text
    backup = ""
    if changed:
        backup_path = log_file.with_suffix(f".log.bak.redact.{int(time.time())}")
        backup_path.write_text(text, encoding="utf-8")
        log_file.write_text(redacted, encoding="utf-8")
        backup = str(backup_path)
    return {"ok": True, "changed": changed, "backup": backup, "path": str(log_file)}


def _load_runtime_state() -> Dict[str, Any]:
    path = _runtime_state_dir() / "replies.json"
    if not path.exists():
        return {}
    try:
        data = json.load(path.open("r", encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_runtime_state(data: Dict[str, Any]) -> None:
    path = _runtime_state_dir() / "replies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@app.get("/api/state-preview")
def api_state_preview(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    data = _load_runtime_state()
    keys = list(data.keys())
    resettable = [k for k in keys if k.startswith("welcome_sent:") or k.startswith("last_peer_fp:")]
    return {
        "path": str(_runtime_state_dir() / "replies.json"),
        "total_keys": len(keys),
        "resettable_keys": len(resettable),
        "reply_events": len(data.get("reply_events", [])) if isinstance(data.get("reply_events"), list) else 0,
        "sample": resettable[:20],
    }


@app.post("/api/state-reset")
def api_state_reset(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    data = _load_runtime_state()
    before = len(data)
    removed_keys = [k for k in list(data.keys()) if k.startswith("welcome_sent:") or k.startswith("last_peer_fp:")]
    for key in removed_keys:
        data.pop(key, None)
    _write_runtime_state(data)
    return {
        "ok": True,
        "path": str(_runtime_state_dir() / "replies.json"),
        "before_keys": before,
        "removed_keys": len(removed_keys),
        "remaining_keys": len(data),
        "kept_reply_events": len(data.get("reply_events", [])) if isinstance(data.get("reply_events"), list) else 0,
    }


@app.post("/api/state-reset-restart")
def api_state_reset_restart(token: str = Query(...)) -> Dict[str, Any]:
    _check_token(token)
    reset_result = api_state_reset(token)
    restart_result = _sudo_systemctl("restart", SVC_BOT)
    return {"ok": bool(reset_result.get("ok")) and bool(restart_result.get("ok")), "reset": reset_result, "restart": restart_result}


@app.get("/api/reply-events")
def api_reply_events(token: str = Query(...), limit: int = 30) -> Dict[str, Any]:
    _check_token(token)
    path = _runtime_state_dir() / "replies.json"
    if not path.exists():
        return {"events": [], "path": str(path)}
    try:
        data = json.load(path.open("r", encoding="utf-8"))
        events = data.get("reply_events", []) if isinstance(data, dict) else []
        if not isinstance(events, list):
            events = []
        normalized = []
        for event in events[-max(1, min(limit, 80)):]: 
            if isinstance(event, dict) and str(event.get("action", "")).startswith("skip_"):
                event = {**event, "ok": None}
            normalized.append(event)
        events = list(reversed(normalized))
        return {"events": events, "path": str(path)}
    except Exception as e:
        return {"events": [], "path": str(path), "error": str(e)}


# ---------- 内存 ----------
def _kb_to_human(kb: float) -> str:
    """把 KB 数转成人性化字符串（GB / MB）。"""
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.2f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{int(kb)} KB"


def _system_mem() -> Dict[str, Any]:
    """读取 /proc/meminfo，返回中文键 + 人性化单位。"""
    raw: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("MemTotal:", "MemAvailable:", "MemFree:", "Buffers:", "Cached:")):
                    k, v = line.split(":", 1)
                    # 形如 "MemTotal:        8141024 kB" → 8141024
                    raw[k] = int(v.strip().split()[0])
    except Exception as e:
        return {"读取失败": str(e)}

    total = raw.get("MemTotal", 0)
    avail = raw.get("MemAvailable", 0)
    used = max(0, total - avail)
    pct = (used / total * 100) if total else 0

    return {
        "总内存": _kb_to_human(total),
        "已用": _kb_to_human(used),
        "可用": _kb_to_human(avail),
        "空闲": _kb_to_human(raw.get("MemFree", 0)),
        "缓冲": _kb_to_human(raw.get("Buffers", 0)),
        "缓存": _kb_to_human(raw.get("Cached", 0)),
        "使用率": f"{pct:.1f}%",
    }


# ---------- 前端 ----------
LANDING_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>美团机器人统一管理入口</title>
<style>
body{font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
.wrap{max-width:980px;margin:0 auto;padding:28px}.muted{color:#94a3b8;font-size:13px}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:16px}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:18px}.card b{color:#60a5fa;font-size:16px}.ok{color:#34d399}.bad{color:#f87171}.warn{color:#fbbf24}
a.btn{display:inline-block;margin-top:12px;margin-right:8px;padding:8px 12px;border-radius:6px;background:#2563eb;color:white;text-decoration:none}.btn.secondary{background:#475569}.small{font-size:12px;word-break:break-all}
</style>
</head>
<body><div class="wrap">
<h1>美团机器人统一管理入口</h1>
<div class="muted">首页只保留店铺入口和核心状态；点击店铺管理进入对应二级管理页。</div>
<div id="shops" class="row">加载中...</div>
<script>
const TOKEN = new URL(location.href).searchParams.get('token') || '';
if (!TOKEN) document.body.innerHTML = '<div class="wrap"><h1>缺少 ?token= 参数</h1></div>'; 
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function state(unit){return unit&&unit.active?'<span class="ok">运行中</span>':'<span class="bad">已停止</span>';}
function cookie(c){if(!c)return '<span class="warn">未知</span>'; return c.status==='valid'?`<span class="ok">有效</span> ${esc(c.age_display||'')}`:`<span class="warn">${esc(c.status_text||'异常')}</span>`;}
async function api(path){const u=new URL(path,location.origin);u.searchParams.set('token',TOKEN);const r=await fetch(u);if(!r.ok)throw new Error(await r.text());return r.json();}
async function load(){const r=await api('/api/shops');const box=document.getElementById('shops');box.innerHTML=(r.shops||[]).map(s=>{const t=encodeURIComponent(s.link_token||TOKEN);const admin=(s.admin_url||location.origin).replace(/\/$/,'')+'/shop?token='+t;const browser=s.remote_browser_url?String(s.remote_browser_url).replace(/\/$/,'')+'/?token='+t:'#';return `<div class="card"><b>${esc(s.name)}</b><div style="margin-top:10px">机器人：${state(s.bot)}<br>登录浏览器：${state(s.browser)}<br>Cookie：${cookie(s.cookie)}</div><a class="btn" href="${admin}">进入店铺管理</a><a class="btn secondary" href="${browser}" target="_blank" rel="noopener">打开登录浏览器</a><div class="muted small">管理外网：${esc(s.admin_url||'-')}<br>浏览器外网：${esc(s.remote_browser_url||'-')}<br>Token：${esc(s.token_fingerprint||'-')}</div></div>`}).join('')||'<div class="muted">暂无店铺</div>';}
load().catch(e=>{document.getElementById('shops').innerHTML='<span class="bad">加载失败：'+esc(e.message)+'</span>';});
</script></div></body></html>"""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>美团外卖自动回复机器人 管理台</title>
<style>
body{font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
.wrap{max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 16px}
h2{font-size:16px;margin:24px 0 8px;color:#93c5fd}
.card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.stat{flex:1;min-width:180px;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:12px}
.stat b{color:#60a5fa;display:block;font-size:12px;margin-bottom:4px}
.stat span{font-size:14px}
button{background:#2563eb;border:0;color:#fff;padding:6px 12px;border-radius:4px;cursor:pointer}
button.danger{background:#dc2626}
button:disabled{opacity:.5;cursor:not-allowed}
input,textarea{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:6px 10px;border-radius:4px;width:100%;box-sizing:border-box;font-family:inherit}
textarea{min-height:60px}
table{width:100%;border-collapse:collapse}
th,td{border-bottom:1px solid #334155;padding:6px;text-align:left;font-size:13px}
th{color:#93c5fd;font-weight:normal}
.log{background:#000;color:#a7f3d0;padding:8px;border-radius:4px;max-height:280px;overflow:auto;font-family:Consolas,monospace;font-size:12px;white-space:pre}
.muted{color:#94a3b8;font-size:12px}
.ok{color:#34d399}.bad{color:#f87171}.tag{display:inline-block;padding:2px 6px;border-radius:999px;background:#334155;color:#cbd5e1;font-size:12px}.tag.ok{background:#064e3b;color:#a7f3d0}.tag.bad{background:#7f1d1d;color:#fecaca}
</style>
</head>
<body>
<div class="wrap">
  <h1>美团外卖自动回复机器人 管理台</h1>
  <div id="instanceBar" class="muted" style="margin:-8px 0 16px">实例加载中...</div>

  <div class="card">
    <h2>访问入口</h2>
    <div class="muted">请使用这里显示的最新入口。每家店使用独立 token，旧共享 token 只是临时兼容。</div>
    <div id="accessLinksBox" class="row" style="margin-top:10px">加载中...</div>
  </div>

  <div class="card">
    <h2>旧 Token 清理</h2>
    <div class="muted">旧共享 token 目前仍临时兼容。确认新链接可用后，再清理旧 token。</div>
    <div class="row" style="margin-top:8px;align-items:center">
      <button onclick="loadLegacyTokenStatus()" style="background:#475569">刷新旧 Token 状态</button>
      <button onclick="clearLegacyTokens(this)" class="danger">清理旧共享 Token</button>
      <span id="legacyTokenBox" class="muted">加载中...</span>
    </div>
  </div>

  <div class="card">
    <h2>运行状态</h2>
    <div class="row" id="statusRow">加载中...</div>
  </div>

  <div class="card">
    <h2>风险告警</h2>
    <div id="alertsBox" class="row">加载中...</div>
  </div>

  <div class="card">
    <h2>多店总控</h2>
    <div class="muted">统一查看每家店的机器人、登录浏览器、Cookie 和管理入口。</div>
    <div id="shopsGrid" class="row" style="margin-top:10px">加载中...</div>
  </div>

  <div class="card">
    <h2>隔离检查</h2>
    <div class="muted">检查两家店的端口、浏览器 profile、状态文件、Cookie 文件和 token 是否互相隔离。</div>
    <div id="isolationGrid" class="row" style="margin-top:10px">加载中...</div>
  </div>

  <div class="card">
    <h2>Cookie 操作指引</h2>
    <div class="muted">根据当前 Cookie 状态提示下一步：Cookie 可能失效时先登录并导出，再启动机器人。</div>
    <div id="cookieOpsBox" class="row" style="margin-top:10px">加载中...</div>
  </div>

  <div class="card">
    <h2>维护清理</h2>
    <div class="muted">日志已配置为自动保留 7 天。轻量清理不影响运行；深度清理会停止当前店铺相关进程，清理更彻底后再启动机器人。</div>
    <div class="row" style="margin-top:8px;align-items:center">
      <button onclick="loadMaintenanceStatus()" style="background:#475569">刷新维护状态</button>
      <button onclick="runLightClean(this)" style="background:#2563eb">轻量清理（不影响运行）</button>
      <button onclick="runDeepClean(this)" class="danger">深度清理（会中断运行）</button>
    </div>
    <div id="maintenanceBox" class="row" style="margin-top:10px">加载中...</div>
  </div>
  <div class="card">
    <h2>服务控制</h2>
    <div class="row">
      <button onclick="svc('start',UNITS.bot,this)">启动机器人</button>
      <button class="danger" onclick="svc('stop',UNITS.bot,this)">停止机器人</button>
      <a id="openBrowser" href="#" target="_blank" rel="noopener"
         style="display:inline-block;padding:8px 14px;background:#1f6feb;color:#fff;
                border-radius:6px;text-decoration:none;margin-right:6px"
         onclick="openRemoteBrowser(event)">打开登录浏览器 ↗</a>
      <button class="danger" onclick="svc('stop',UNITS.browser,this)">停止登录浏览器</button>
      <button onclick="downloadDiagnostics(this)" style="background:#475569">导出诊断包</button>
      <button onclick="redactAdminLog(this)" style="background:#334155">脱敏历史日志</button>
    </div>
    <div class="muted" style="margin-top:6px">登录浏览器保活 Cookie，机器人通过 Cookie 注入独立运行，两者可同时运行。</div>
  </div>

  <div class="card">
    <h2>规则测试</h2>
    <div class="row" style="align-items:flex-end">
      <div style="flex:1">
        <div class="muted">顾客原话</div>
        <input id="tMsg" placeholder="例如：还有没有可乐">
      </div>
      <div>
        <label class="muted"><input type="checkbox" id="tFirst"> 首条消息</label>
      </div>
      <button onclick="testRule()">测试匹配</button>
    </div>
    <div id="tResult" class="log" style="margin-top:8px"></div>
  </div>

  <div class="card">
    <h2>测试流程观察</h2>
    <div class="muted">发送测试消息前先点击“开始观察”，这里会显示识别到的顾客消息、命中规则、回复内容和发送结果。</div>
    <div class="row" style="margin-top:8px;align-items:center">
      <button onclick="startWatchFlow()" style="background:#16a34a">开始观察</button>
      <button onclick="clearWatchFlow()" style="background:#475569">清空观察</button>
      <span id="watchFlowHint" class="muted">未开始</span>
    </div>
    <div id="watchFlowBox" class="row" style="margin-top:10px">
      <div class="stat"><b>等待测试</b><span>点击开始观察后，给当前店铺发送一条测试消息。</span></div>
    </div>
  </div>

  <div class="card">
    <h2>回复规则</h2>
    <div class="muted">编辑后保存。重启机器人后生效。</div>
    <div class="row" style="margin-top:8px">
      <div style="flex:1">
        <div class="muted">first_message</div>
        <textarea id="firstMsg"></textarea>
      </div>
      <div style="flex:1">
        <div class="muted">fallback</div>
        <textarea id="fallbackMsg"></textarea>
      </div>
    </div>
    <h2 style="margin-top:16px">关键词规则</h2>
    <table id="rulesTable">
      <thead><tr><th>名称</th><th>关键词(逗号分隔)</th><th>回复</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
    <div style="margin-top:8px">
      <button onclick="addRule()">新增规则</button>
      <button onclick="saveRules()">保存所有</button>
      <button id="restartAfterSave" style="display:none;background:#16a34a" onclick="svc('restart',UNITS.bot,this)">重启机器人使规则生效</button>
    </div>
    <div id="saveHint" class="muted" style="margin-top:6px"></div>
    <h2 style="margin-top:16px">规则备份/回滚</h2>
    <div class="row" style="align-items:center">
      <button onclick="loadRuleBackups()" style="background:#475569">刷新备份列表</button>
      <span class="muted">每次保存规则会自动备份旧 config.yaml。</span>
    </div>
    <table id="backupTable" style="margin-top:8px">
      <thead><tr><th>备份时间</th><th>文件名</th><th>操作</th></tr></thead>
      <tbody><tr><td colspan="3" class="muted">加载中...</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <h2>推广定时开关</h2>
    <div class="muted">美团"一站式推广"按时间段自动开/关。当前时间落在任一 [开始, 结束) 区间时开启，跨夜窗口也支持（开始>结束表示跨夜）。最多 3 段窗口。保存后立即生效，无需重启。</div>
    <div class="row" style="align-items:center;margin-top:8px">
      <label style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="promoEnabled"> 启用推广定时调度
      </label>
      <span id="promoStatus" class="muted"></span>
    </div>
    <table style="margin-top:8px">
      <thead><tr><th>段</th><th>开始时间</th><th>结束时间</th></tr></thead>
      <tbody id="promoWindows"></tbody>
    </table>
    <div style="margin-top:8px">
      <button onclick="addPromoRow()">新增时段</button>
      <button onclick="savePromo()" style="background:#16a34a">保存并立即生效</button>
    </div>
    <div id="promoHint" class="muted" style="margin-top:6px"></div>
  </div>

  <div class="card">
    <h2>测试状态重置</h2>
    <div class="muted">清理欢迎语和去重状态，便于重复测试；回复记录会保留。</div>
    <div class="row" style="margin-top:8px;align-items:center">
      <button onclick="loadStatePreview()" style="background:#475569">刷新状态</button>
      <button onclick="resetTestState(this)" class="danger">重置测试状态</button>
      <button onclick="resetAndRestartTestState(this)" style="background:#b45309">重置并重启机器人</button>
      <span id="statePreviewBox" class="muted">未加载</span>
    </div>
  </div>

  <div class="card">
    <h2>最近回复记录</h2>
    <div class="muted">展示 bot 本次为什么回复、命中规则、回复内容和发送结果。</div>
    <table id="eventsTable" style="margin-top:8px">
      <thead><tr><th>时间</th><th>顾客</th><th>动作</th><th>规则</th><th>顾客消息</th><th>回复</th><th>结果</th></tr></thead>
      <tbody><tr><td colspan="7" class="muted">加载中...</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <h2>关键事件日志</h2>
    <div class="muted">只显示识别、决策、发送、跳过、错误等关键事件。</div>
    <div class="log" id="keyLogs" style="margin-top:8px">加载中...</div>
  </div>

  <div class="card">
    <h2>原始日志 (bot.log)</h2>
    <div class="log" id="logs">加载中...</div>
  </div>
</div>

<script>
const TOKEN = new URL(location.href).searchParams.get('token') || '';
if (!TOKEN) document.body.innerHTML = '<div class="wrap"><h1>缺少 ?token= 参数</h1></div>';
async function api(path, opts){
  const u = new URL(path, location.origin);
  u.searchParams.set('token', TOKEN);
  const r = await fetch(u, opts || {});
  if (!r.ok) { alert('HTTP '+r.status+': '+await r.text()); throw new Error('http'); }
  return r.json();
}
function el(tag, cls, text){ const e=document.createElement(tag); if(cls)e.className=cls; if(text)e.textContent=text; return e; }
function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function eventTs(e){
  const raw = e.ts || e.time || e.created_at || e.timestamp || 0;
  return typeof raw === 'number' ? raw : (Date.parse(raw) / 1000 || 0);
}
function eventTime(e){
  const ts = eventTs(e);
  return ts ? new Date(ts * 1000).toLocaleTimeString() : '-';
}

function ensureCard(name){
  const row = document.getElementById("statusRow");
  let card = document.getElementById("card-"+name);
  if (!card){
    card = document.createElement("div");
    card.className = "stat";
    card.id = "card-"+name;
    card.innerHTML = '<b class="cardTitle"></b><div class="cardBody"></div>';
    row.appendChild(card);
  }
  return card;
}
function setBody(card, title, html){
  const t = card.querySelector(".cardTitle");
  if (t.textContent !== title) t.textContent = title;
  const b = card.querySelector(".cardBody");
  if (b.innerHTML !== html) b.innerHTML = html;
}
function svcBody(info){
  if (!info || !("active" in info)) return "";
  let html = info.active ? '<span class="ok">运行中</span>' : '<span class="bad">未运行</span>';
  if (info.raw && info.raw.MemoryCurrent) html += "<br>内存: " + (parseInt(info.raw.MemoryCurrent)/1048576).toFixed(0) + " MB";
  if (info.raw && info.raw.NRestarts) html += "<br>重启: " + info.raw.NRestarts;
  return html;
}
function cookieBody(info){
  if (!info || !("exists" in info)) return "";
  const cls = info.status === "valid" ? "ok" : (info.status === "stale" ? "bad" : (info.status === "warning" ? "bad" : "muted"));
  let html = '<span class="' + cls + '">' + (info.status_text || "") + "</span>";
  if (info.exists) html += "<br>数量: " + info.cookie_count;
  if (info.age_display) html += "<br>已运行: " + info.age_display;
  if (info.export_time) html += "<br>导出: " + info.export_time;
  return html;
}
function memBody(info){
  if (!info) return "";
  return Object.entries(info).map(function(kv){return "<div>"+kv[0]+": "+kv[1]+"</div>";}).join("");
}

function unitState(info){ return info && info.active ? '<span class="ok">运行</span>' : '<span class="bad">停止</span>'; }
function shopCookie(info){
  if (!info) return '';
  const cls = info.status === 'valid' ? 'ok' : 'bad';
  return `<span class="${cls}">${info.status_text || ''}</span>${info.age_display ? ' / ' + info.age_display : ''}`;
}
async function loadLegacyTokenStatus(){
  const box = document.getElementById('legacyTokenBox');
  if (!box) return;
  try {
    const r = await api('/api/token-legacy-status');
    const parts = (r.shops || []).map(s => `${s.name}: 旧Token数=${s.legacy_count}`).join(' / ');
    box.textContent = `旧Token总数=${r.total_legacy}; ${parts}`;
  } catch(e) {
    box.textContent = '旧 Token 状态加载失败';
  }
}

async function clearLegacyTokens(btn){
  if (!confirm('确定清理两家店的旧共享 token 兼容吗？新的独立链接会继续可用。')) return;
  btn.disabled = true;
  try {
    const r = await api('/api/token-legacy-clear', {method:'POST'});
    alert(`已清理旧 token。变更店铺数=${(r.changed || []).length}。建议重启管理服务。`);
    await loadLegacyTokenStatus();
  } finally {
    btn.disabled = false;
  }
}

async function loadAccessLinks(){
  const box = document.getElementById('accessLinksBox');
  if (!box) return;
  try {
    const inst = await api('/api/instance');
    const browser = await api('/api/remote-browser-url');
    const shopsResp = await api('/api/shops');
    const currentAdmin = `${(inst.admin_url || location.origin).replace(/\/$/,'')}/shop?token=${encodeURIComponent(TOKEN)}`;
    const current = `<div class="stat" style="min-width:300px"><b>当前店铺</b>
      <div>管理页：<a href="${currentAdmin}" target="_blank" style="color:#93c5fd">打开</a></div>
      <div>登录浏览器：<a href="${browser.url}" target="_blank" style="color:#93c5fd">打开</a></div>
      <div class="muted">${esc(inst.name)} / token=${esc(inst.token_fingerprint || '-')}</div>
    </div>`;
    const shops = (shopsResp.shops || []).map(s => {
      const token = s.link_token || TOKEN;
      const adminUrl = `${(s.admin_url || location.origin).replace(/\/$/,'')}/shop?token=${encodeURIComponent(token)}`;
      const browserUrl = s.remote_browser_url ? `${s.remote_browser_url}/?token=${encodeURIComponent(token)}` : '#';
      return `<div class="stat" style="min-width:300px"><b>${esc(s.name)}</b>
        <div>管理页：<a href="${adminUrl}" target="_blank" style="color:#93c5fd">${esc(adminUrl)}</a></div>
        <div>登录浏览器：<a href="${browserUrl}" target="_blank" style="color:#93c5fd">${esc(browserUrl)}</a></div>
      </div>`;
    }).join('');
    box.innerHTML = current + shops;
  } catch(e) {
    box.innerHTML = '<span class="bad">访问入口加载失败</span>';
  }
}

function checkBadge(level){
  if (level === 'ok') return '<span class="ok">正常</span>';
  if (level === 'warn') return '<span style="color:#fbbf24">警告</span>';
  return '<span class="bad">异常</span>';
}

async function loadIsolation(){
  const grid = document.getElementById('isolationGrid');
  if (!grid) return;
  try {
    const r = await api('/api/isolation');
    const checks = (r.checks || []).map(c => `<div>${esc(c.field)}: ${checkBadge(c.level)}${c.duplicates && c.duplicates.length ? '<br><span class="muted">重复：'+esc(c.duplicates.join(', '))+'</span>' : ''}</div>`).join('');
    const shops = (r.shops || []).map(s => `<div class="stat" style="min-width:300px">
      <b>${esc(s.name)}</b>
      <div>管理端=${esc(s.admin_port)} 登录浏览器=${esc(s.remote_browser_port)} VNC=${esc(s.vnc_port || '-')}</div>
      <div class="muted">profile=${esc(s.profile_dir)}</div>
      <div class="muted">状态目录=${esc(s.state_dir)}</div>
      <div class="muted">Cookie文件=${esc(s.cookie_path)}</div>
      <div class="muted">token=${esc(s.token_fingerprint)} 旧Token数=${esc(s.legacy_token_count)}</div>
    </div>`).join('');
    grid.innerHTML = `<div class="stat" style="min-width:260px"><b>检查项</b>${checks}</div>${shops}`;
  } catch(e) {
    grid.innerHTML = '<span class="bad">隔离检查加载失败</span>';
  }
}

async function loadShops(){
  const grid = document.getElementById('shopsGrid');
  if (!grid) return;
  try {
    const r = await api('/api/shops');
    const shops = r.shops || [];
    grid.innerHTML = shops.map(s => {
      const shopToken = s.link_token || TOKEN;
      const adminUrl = `${(s.admin_url || location.origin).replace(/\/$/,'')}/shop?token=${encodeURIComponent(shopToken)}`;
      const browserUrl = s.remote_browser_url ? `${s.remote_browser_url}/?token=${encodeURIComponent(shopToken)}` : '#';
      return `<div class="stat" style="min-width:280px">
        <b>${s.name}</b>
        <div>机器人：${unitState(s.bot)}　浏览器：${unitState(s.browser)}</div>
        <div>Cookie：${shopCookie(s.cookie)}</div>
        <div class="muted">管理端口：${s.admin_port || '-'}　Token指纹：${s.token_fingerprint || '-'}</div>
        <div style="margin-top:8px">
          <a href="${adminUrl}" target="_blank" style="color:#93c5fd">打开管理页</a>
          ${s.remote_browser_url ? `　<a href="${browserUrl}" target="_blank" style="color:#93c5fd">登录浏览器</a>` : ''}
        </div>
      </div>`;
    }).join('') || '<span class="muted">暂无店铺</span>';
  } catch(e) {
    grid.innerHTML = '<span class="bad">多店状态加载失败</span>';
  }
}

function cookieOpsHtml(info){
  if (!info || !('status' in info)) return '<div class="stat"><b>状态</b><span>未知</span></div>';
  const st = info.status;
  const ka = info.keepalive || {};
  const age = info.age_display ? info.age_display : '-';
  const count = info.cookie_count || 0;
  let cls = 'ok', tip = '', steps = '';
  if (st === 'login_required') {
    cls = 'bad';
    tip = '登录账号已失效。请手动打开登录浏览器重新登录，登录后系统会自动导出 Cookie，无需再点续期。';
    steps = '1. 点击「打开登录浏览器」<br>2. 完成登录并进入 IM 工作台<br>3. 关闭浏览器即可，系统会继续自动续期';
  } else if (st === 'valid') {
    cls = 'ok';
    tip = 'Cookie 有效，登录浏览器已关闭以节省内存。无需任何操作。';
    steps = '系统每 15 分钟检查一次 cookie；发现陈旧会自动拉起登录浏览器并导出，完成后再关闭。你不需要手动绯刷新。';
  } else if (st === 'renewing') {
    cls = 'warn';
    tip = 'Cookie 正在自动续期（keepalive 已运行）。等待下一次刷新。';
    steps = '无需操作。如果按钮 1 分钟内仍显示续期中，可点击「立即续期 Cookie」强制触发。';
  } else {
    cls = 'bad';
    tip = 'Cookie 未导出或可能失效。点击「立即续期 Cookie」可自动启动登录浏览器并导出。';
    steps = '1. 点击「立即续期 Cookie」<br>2. 系统自动启动登录浏览器并跳转到 IM<br>3. 如已登录则自动导出 Cookie；如未登录会提示您手动登录';
  }
  return `
    <div class="stat"><b>Cookie 状态</b><span class="${cls}">${esc(info.status_text || st)}</span><br><span class="muted">已运行=${esc(age)}，数量=${esc(count)}</span></div>
    <div class="stat"><b>建议下一步</b><span>${esc(tip)}</span></div>
    <div class="stat"><b>操作流程</b><span>${steps}</span></div>
    <div class="stat"><b>立即续期 Cookie</b><button class="btn" onclick="triggerAutoRefresh(this)">立即续期 Cookie</button><span class="muted">系统会自动启动登录浏览器、跳转 IM、导出 Cookie；登录失效时会提示您手动登录。</span></div>
    <div class="stat"><b>keepalive 状态</b><span>登录状态=${ka.logged_in?'是':'否'}，URL=${esc((ka.last_url||'').slice(0,60))}，最近检查=${ka.age_seconds!=null?Math.round(ka.age_seconds)+'秒前':'-'}</span></div>
  `;
}

async function triggerAutoRefresh(btn){
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/auto-refresh-cookies', {method:'POST'});
    if (r.ok && r.skipped) { alert('Cookie 有效，无需续期。'); }
    else if (r.ok) { alert('已续期 Cookie，共 '+ (r.cookie_count||0) +' 个。'); }
    else if (r.manual_login_needed) { alert('登录账号已失效。请打开登录浏览器重新登录，登录后会自动续期。\n\n最近 URL：' + (r.last_url||'-')); }
    else { alert('续期失败：' + (r.reason||'未知')); }
    await loadCookieOps();
    await refresh();
  } catch(e) {
    alert('续期请求失败：' + e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadCookieOps(){
  const box = document.getElementById('cookieOpsBox');
  if (!box) return;
  try {
    const r = await api('/api/cookie-status');
    box.innerHTML = cookieOpsHtml(r);
  } catch(e) {
    box.innerHTML = '<span class="bad">Cookie 操作指引加载失败</span>';
  }
}

function maintenanceHtml(r){
  return `
    <div class="stat"><b>日志策略</b><span>自动轮转：每天检查，只保留 7 天；单文件超过 20MB 会提前轮转。</span><br><span class="muted">当前日志：${esc(r.total_log_size || '-')}</span></div>
    <div class="stat"><b>轻量缓存</b><span>${esc(r.total_light_cache_size || '-')}</span><br><span class="muted">不停止机器人，只清 Python 缓存。</span></div>
    <div class="stat"><b>浏览器缓存</b><span>${esc(r.total_browser_cache_size || '-')}</span><br><span class="muted">深度清理会停止浏览器和机器人后清理。</span></div>
  `;
}

async function loadMaintenanceStatus(){
  const box = document.getElementById('maintenanceBox');
  if (!box) return;
  try {
    const r = await api('/api/maintenance/status');
    box.innerHTML = maintenanceHtml(r);
  } catch(e) {
    box.innerHTML = '<span class="bad">维护状态加载失败</span>';
  }
}

async function runLightClean(btn){
  btn.disabled = true;
  try {
    const r = await api('/api/maintenance/light', {method:'POST'});
    alert('轻量清理完成，不影响机器人运行。');
    document.getElementById('maintenanceBox').innerHTML = maintenanceHtml(r.status || {});
    await refresh();
  } finally {
    btn.disabled = false;
  }
}

async function runDeepClean(btn){
  if (!confirm('深度清理会停止当前店铺机器人和登录浏览器，清浏览器缓存、强制轮转日志、释放系统缓存，然后重启机器人。继续？')) return;
  btn.disabled = true;
  try {
    const r = await api('/api/maintenance/deep', {method:'POST'});
    alert(r.ok ? '深度清理完成，机器人已重新启动。' : '深度清理完成，但机器人启动结果异常，请查看运行状态。');
    document.getElementById('maintenanceBox').innerHTML = maintenanceHtml(r.status || {});
    await refresh();
  } finally {
    btn.disabled = false;
  }
}
async function refresh(){
  let s = null, cookieData = null;
  try { s = await api("/api/status"); } catch(e) { return; }
  try { cookieData = await api("/api/cookie-status"); } catch(e) {}
  setBody(ensureCard("bot"),     "机器人",                       svcBody(s.bot));
  setBody(ensureCard("admin"),   "管理台",                       svcBody(s.admin));
  setBody(ensureCard("browser"), "登录浏览器",         svcBody(s.browser));
  setBody(ensureCard("cookie"),  "Cookie",                                       cookieBody(cookieData));
  setBody(ensureCard("mem"),     "系统内存",                 memBody(s.system_mem));
}

async function svc(action, unit, btn){
  btn.disabled = true;
  try {
    const r = await api(`/api/service/${action}?unit=${encodeURIComponent(unit)}`, {method:'POST'});
    alert(r.ok ? '成功' : ('失败: '+(r.stderr||r.stdout||r.error)));
    refresh();
  } finally { btn.disabled = false; }
}

// 打开登录浏览器：先确保服务在跑，再用公网 URL 打开新标签
async function openRemoteBrowser(ev){
  ev.preventDefault();
  const a = document.getElementById('openBrowser');
  // 1) 询问后端当前登录浏览器的公网入口（端口可能受 NAT 映射影响）
  let url;
  try {
    const info = await api('/api/remote-browser-url');
    url = info.url;
  } catch(e) {
    alert('无法获取登录浏赛器公网地址，请检查 config.yaml 的 remote_browser_public_url');
    return;
  }
  // 2) 后台异步启动服务（不等结果，避免阻塞新标签）
  fetch(`/api/service/start?unit=${encodeURIComponent(UNITS.browser)}&token=${encodeURIComponent(TOKEN)}`, {method:'POST'});
  // 3) 新标签打开
  window.open(url, '_blank', 'noopener');
  // 4) 几秒后刷新一下状态卡
  setTimeout(refresh, 1500);
}
// 页面加载时给链接填上默认 href（hover 可见）
let UNITS = {bot:'meituan-reply-bot.service', browser:'meituan-browser-control.service'};
api('/api/units').then(u => { UNITS = u; }).catch(() => {});
api('/api/remote-browser-url').then(r => { document.getElementById('openBrowser').href = r.url; }).catch(() => {});
async function loadInstance(){
  const bar = document.getElementById('instanceBar');
  if (!bar) return;
  try {
    const r = await api('/api/instance');
    bar.innerHTML = `当前实例：<span class="ok">${r.name}</span>　管理端口：${r.admin_port || '-'}　Bot：${r.bot_unit}<br>${r.security_note || ''}`;
  } catch(e) {
    bar.textContent = '当前实例加载失败';
  }
}

async function testRule(){
  const msg = document.getElementById('tMsg').value;
  const isFirst = document.getElementById('tFirst').checked;
  const r = await api('/api/rules/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: msg, is_first_message: isFirst})});
  document.getElementById('tResult').textContent = JSON.stringify(r, null, 2);
}

async function redactAdminLog(btn){
  if (!confirm('会备份 admin.log，然后把历史日志里的 token=... 替换为 token=***。继续？')) return;
  btn.disabled = true;
  try {
    const r = await api('/api/redact-admin-log', {method:'POST'});
    alert(r.changed ? `已脱敏，备份：${r.backup}` : '无需处理，未发现明文 token');
    loadLogs();
  } finally {
    btn.disabled = false;
  }
}

async function loadAlerts(){
  const box = document.getElementById('alertsBox');
  if (!box) return;
  try {
    const r = await api('/api/alerts');
    const alerts = r.alerts || [];
    box.innerHTML = alerts.map(a => {
      const cls = a.level === 'ok' ? 'ok' : (a.level === 'bad' ? 'bad' : 'muted');
      const border = a.level === 'ok' ? '#047857' : (a.level === 'bad' ? '#991b1b' : '#92400e');
      return `<div class="stat" style="border-color:${border}"><b class="${cls}">${a.title}</b><span>${a.detail || ''}</span></div>`;
    }).join('') || '<span class="muted">暂无告警</span>';
  } catch(e) {
    box.innerHTML = '<span class="bad">告警加载失败</span>';
  }
}

async function downloadDiagnostics(btn){
  btn.disabled = true;
  try {
    const r = await api('/api/diagnostics');
    const name = (r.instance && r.instance.name ? r.instance.name : 'meituan-bot').replace(/\s+/g, '-');
    const blob = new Blob([JSON.stringify(r, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${name}-diagnostics-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } finally {
    btn.disabled = false;
  }
}

async function loadRules(){
  const r = await api('/api/rules');
  document.getElementById('firstMsg').value = r.first_message || '';
  document.getElementById('fallbackMsg').value = r.fallback || '';
  const tbody = document.querySelector('#rulesTable tbody');
  tbody.innerHTML = '';
  for (const rule of (r.rules || [])) addRuleRow(rule);
}

function addRuleRow(rule){
  rule = rule || {name:'', keywords:[], reply:''};
  const tbody = document.querySelector('#rulesTable tbody');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input data-k="name" value="${(rule.name||'').replace(/"/g,'&quot;')}"></td>
    <td><input data-k="keywords" value="${(rule.keywords||[]).join(',').replace(/"/g,'&quot;')}"></td>
    <td><textarea data-k="reply">${(rule.reply||'').replace(/</g,'&lt;')}</textarea></td>
    <td><button class="danger" onclick="this.closest('tr').remove()">删除</button></td>
  `;
  tbody.appendChild(tr);
}
function addRule(){ addRuleRow(); }
async function loadRuleBackups(){
  const tbody = document.querySelector('#backupTable tbody');
  if (!tbody) return;
  try {
    const r = await api('/api/rules/backups');
    const items = r.backups || [];
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="muted">暂无备份</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(b => `<tr><td>${b.time}</td><td>${b.name}</td><td><button class="danger" onclick="restoreRules('${b.name}')">恢复</button></td></tr>`).join('');
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="3" class="bad">加载失败</td></tr>';
  }
}
async function restoreRules(name){
  if (!confirm('确认恢复这个规则备份？恢复后需要重启机器人生效。')) return;
  await api(`/api/rules/restore?name=${encodeURIComponent(name)}`, {method:'POST'});

loadRules();
  await loadRuleBackups();
  document.getElementById('saveHint').innerHTML = '<span class="ok">已恢复备份。</span> 需要重启机器人后生效。';
  document.getElementById('restartAfterSave').style.display = 'inline-block';
}

async function saveRules(){
  const first = document.getElementById('firstMsg').value;
  const fallback = document.getElementById('fallbackMsg').value;
  const rules = Array.from(document.querySelectorAll('#rulesTable tbody tr')).map(tr => {
    const o = {};
    tr.querySelectorAll('[data-k]').forEach(e => {
      const k = e.dataset.k;
      if (k === 'keywords') o[k] = e.value.split(',').map(s=>s.trim()).filter(Boolean);
      else o[k] = e.value;
    });
    return o;
  });
  const saved = await api('/api/rules', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({first_message: first, fallback, rules})});
  await loadRuleBackups();
  document.getElementById('saveHint').innerHTML = '<span class="ok">已保存。</span> 新规则需要重启机器人后生效。' + (saved.backup ? ' 已备份旧配置。' : '');
  document.getElementById('restartAfterSave').style.display = 'inline-block';
}

async function loadStatePreview(){
  const box = document.getElementById('statePreviewBox');
  if (!box) return;
  try {
    const r = await api('/api/state-preview');
    box.textContent = `可重置=${r.resettable_keys}，总数=${r.total_keys}，回复记录=${r.reply_events}`;
  } catch(e) {
    box.textContent = '状态加载失败';
  }
}

async function resetTestState(btn){
  if (!confirm('确定重置当前店铺的欢迎语/去重状态吗？回复记录会保留。')) return;
  btn.disabled = true;
  try {
    const r = await api('/api/state-reset', {method:'POST'});
    alert(`已移除 ${r.removed_keys} 个状态键。建议重启机器人后再测试。`);
    await loadStatePreview();
  } finally {
    btn.disabled = false;
  }
}

async function resetAndRestartTestState(btn){
  if (!confirm('确定重置当前店铺测试状态，并立即重启该店机器人吗？')) return;
  btn.disabled = true;
  try {
    const r = await api('/api/state-reset-restart', {method:'POST'});
    const removed = r.reset ? r.reset.removed_keys : 0;
    const ok = r.ok ? '正常' : '失败';
    alert(`${ok}. 已移除 ${removed} 个状态键。重启退出码=${r.restart ? r.restart.exit : 'n/a'}`);
    await loadStatePreview();
    await refresh();
  } finally {
    btn.disabled = false;
  }
}

let watchFlowSince = Number(localStorage.getItem('watchFlowSince') || '0');

function renderEventRow(e){
  const action = String(e.action || '');
  const actionText = {
    skip_not_pending: '未待回复',
    skip_paused: '机器人暂停',
    skip_duplicate: '防重复跳过',
    first_message: '首条欢迎',
    keyword: '关键词回复',
    fallback: '保底回复'
  }[action] || action;
  const ok = action.startsWith('skip_') ? '<span class="muted">已跳过</span>' : (e.ok === true ? '<span class="ok">正常</span>' : (e.ok === false ? '<span class="bad">失败</span>' : '<span class="muted">-</span>'));
  return `<tr>
    <td>${eventTime(e)}</td>
    <td>${esc(e.customer)}</td>
    <td title="${esc(action)}">${esc(actionText)}</td>
    <td>${esc(e.rule)}</td>
    <td>${esc(e.message)}</td>
    <td>${esc(e.reply)}</td>
    <td>${ok}</td>
  </tr>`;
}

async function loadEvents(){
  const tbody = document.querySelector('#eventsTable tbody');
  if (!tbody) return;
  try {
    const r = await api('/api/reply-events?limit=30');
    const events = r.events || [];
    tbody.innerHTML = events.length ? events.map(renderEventRow).join('') : '<tr><td colspan="7" class="muted">暂无回复记录。如果刚发过消息，请查看关键事件日志中的识别或发送错误。</td></tr>';
    renderWatchFlow(events);
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" class="bad">回复记录加载失败</td></tr>';
  }
}

function startWatchFlow(){
  watchFlowSince = Date.now() / 1000;
  localStorage.setItem('watchFlowSince', String(watchFlowSince));
  renderWatchFlow([]);
  loadEvents();
  loadKeyLogs();
}

function clearWatchFlow(){
  watchFlowSince = 0;
  localStorage.removeItem('watchFlowSince');
  renderWatchFlow([]);
}

function renderWatchFlow(events){
  const hint = document.getElementById('watchFlowHint');
  const box = document.getElementById('watchFlowBox');
  if (!hint || !box) return;
  if (!watchFlowSince) {
    hint.textContent = '未开始';
    box.innerHTML = '<div class="stat"><b>等待测试</b><span>点击开始观察后，给当前店铺发送一条测试消息。</span></div>';
    return;
  }
  hint.textContent = '观察开始：' + new Date(watchFlowSince * 1000).toLocaleTimeString();
  const matched = (events || []).filter(e => eventTs(e) >= watchFlowSince).sort((a,b) => eventTs(b) - eventTs(a));
  if (!matched.length) {
    box.innerHTML = '<div class="stat"><b>还没有回复事件</b><span>如果顾客已经发消息，请查看关键事件日志：可能未识别到倒计时/超时卡，或没有找到左侧顾客气泡。</span></div>';
    return;
  }
  const latest = matched[0];
  const latestAction = String(latest.action || '');
  const skipped = latestAction.startsWith('skip_');
  const okCls = skipped ? 'muted' : (latest.ok ? 'ok' : 'bad');
  box.innerHTML = `
    <div class="stat"><b>1. 识别顾客</b><span>${esc(latest.customer || '-')}<br><span class="muted">${esc(latest.card_text || '')}</span></span></div>
    <div class="stat"><b>2. 顾客消息</b><span>${esc(latest.message || '-')}</span></div>
    <div class="stat"><b>3. 命中规则</b><span>${esc(latest.action || '-')} / ${esc(latest.rule || '-')}</span></div>
    <div class="stat"><b>4. 发送结果</b><span class="${okCls}">${skipped ? '已跳过' : (latest.ok ? '正常' : '失败')}</span><br>${esc(latest.reply || '')}</div>
  `;
}


async function loadKeyLogs(){
  const el = document.getElementById('keyLogs');
  if (!el) return;
  try {
    const r = await api('/api/key-logs?tail=500&limit=120');
    el.textContent = (r.lines || []).join('\n') || '暂无关键事件';
  } catch(e) {
    el.textContent = '加载失败';
  }
}

async function loadLogs(){
  const r = await api('/api/logs?tail=120');
  document.getElementById('logs').textContent = (r.lines||[]).join('\n');
}

loadInstance();
loadAccessLinks();
async function loadPromo(){
  try {
    const r = await api('/api/promo-scheduler');
    document.getElementById('promoEnabled').checked = !!r.enabled;
    document.getElementById('promoStatus').textContent =
      r.enabled ? '当前：已启用' : '当前：未启用（保存后调度器将停止动作）';
    const tbody = document.getElementById('promoWindows');
    tbody.innerHTML = '';
    const wins = (r.windows && r.windows.length) ? r.windows : [];
    if (!wins.length) { addPromoRow(); return; }
    for (const w of wins) addPromoRow(w);
  } catch(e) {
    document.getElementById('promoHint').innerHTML = '<span class="bad">加载失败：'+esc(e.message)+'</span>';
  }
}
function addPromoRow(w){
  w = w || {start:'09:00', end:'12:00'};
  const tbody = document.getElementById('promoWindows');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td class="muted">#${tbody.children.length+1}</td>
    <td><input type="time" data-k="start" value="${esc(w.start||'')}"></td>
    <td><input type="time" data-k="end" value="${esc(w.end||'')}"></td>
    <td><button class="danger" onclick="this.closest('tr').remove()">删除</button></td>
  `;
  tbody.appendChild(tr);
}
async function savePromo(){
  const rows = Array.from(document.querySelectorAll('#promoWindows tr'));
  if (rows.length > 3) { alert('最多 3 段窗口'); return; }
  const windows = [];
  for (const tr of rows) {
    const s = tr.querySelector('[data-k=start]').value;
    const e = tr.querySelector('[data-k=end]').value;
    if (!s || !e) { alert('请填写完整时间'); return; }
    windows.push({start: s, end: e});
  }
  const enabled = document.getElementById('promoEnabled').checked;
  try {
    const r = await api('/api/promo-scheduler', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enabled: enabled, windows: windows})});
    document.getElementById('promoHint').innerHTML =
      '<span class="ok">已保存。下一轮调度（约 '+(r.promotion_scheduler && r.promotion_scheduler.check_interval_sec || 30)+'s 内）即按新时段动作。</span>'
      + (r.backup ? ' 旧配置已备份：'+esc(r.backup.split('/').pop()) : '');
  } catch(e) {
    document.getElementById('promoHint').innerHTML = '<span class="bad">保存失败：'+esc(e.message)+'</span>';
  }
}

loadRules();
loadRuleBackups();
refresh();
loadCookieOps();
loadAlerts();
loadShops();
loadIsolation();
loadEvents();
loadStatePreview();
loadKeyLogs();
loadLogs();
setInterval(loadAccessLinks, 15000);
setInterval(refresh, 5000);
setInterval(loadCookieOps, 5000);
setInterval(loadAlerts, 5000);
setInterval(loadShops, 5000);
setInterval(loadIsolation, 15000);
setInterval(loadEvents, 5000);
setInterval(loadStatePreview, 15000);
setInterval(loadKeyLogs, 5000);
setInterval(loadLogs, 12000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(LANDING_HTML)


@app.get("/shop", response_class=HTMLResponse)
def shop_index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    server = cfg.get("server", {}) or {}
    host = args.host or server.get("admin_host", "0.0.0.0")
    port = int(args.port or server.get("admin_port", 3003))
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
