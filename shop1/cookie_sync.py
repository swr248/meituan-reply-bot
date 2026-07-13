"""Cookie sync: export/import cookies between browser-control and bot.

browser-control exports cookies to JSON after login.
bot loads cookies from JSON and injects via context.add_cookies().
This way bot does not need persistent context and won't lock the profile.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from browser_common import log


_WRITE_LOCKS: Dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _cookie_write_lock(path: Path):
    lock = _thread_lock(path)
    with lock:
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except ImportError:
                fcntl = None
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_cookie_state(path: Path, data: Dict[str, Any]) -> bool:
    path = Path(path)
    export_time = float(data.get("export_time", 0) or 0)
    cookies = data.get("cookies")
    if export_time <= 0 or not isinstance(cookies, list):
        raise ValueError("invalid cookie state")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _cookie_write_lock(path):
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                current_time = float(current.get("export_time", 0) or 0)
                if current_time > export_time:
                    return False
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as temp_file:
                temp_name = temp_file.name
                json.dump(data, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            os.chmod(path, 0o600)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return True
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)


def cookie_file_path(cfg: Dict[str, Any]) -> Path:
    profile_dir = cfg.get("browser", {}).get("profile_dir", "")
    if profile_dir:
        return Path(profile_dir).parent / "state" / "cookies.json"
    return Path("state") / "cookies.json"


def export_cookies(context_or_page, cfg: Dict[str, Any]) -> int:
    try:
        if hasattr(context_or_page, "cookies"):
            ctx = context_or_page
        elif hasattr(context_or_page, "context"):
            ctx = context_or_page.context
        else:
            log.error("cookie_sync: cannot get context from object")
            return 0

        raw_cookies = ctx.cookies()
        if not raw_cookies:
            log.warning("cookie_sync: no cookies found in context")
            return 0

        meituan_domains = [
            ".meituan.com", ".waimai.meituan.com",
            ".sankuai.com", ".meituan.cn",
        ]
        filtered = []
        for c in raw_cookies:
            domain = c.get("domain", "")
            if any(d in domain for d in meituan_domains):
                filtered.append(c)

        if not filtered:
            log.warning("cookie_sync: no meituan cookies found (total=%d)", len(raw_cookies))
            filtered = raw_cookies

        # Dedupe by (name, domain, path): keep the entry with max expires (last login wins).
        dedup = {}
        for c in filtered:
            key = (c.get("name", ""), c.get("domain", ""), c.get("path", "/"))
            prev = dedup.get(key)
            if prev is None or float(c.get("expires", -1) or -1) >= float(prev.get("expires", -1) or -1):
                dedup[key] = c
        before = len(filtered)
        filtered = list(dedup.values())
        if before != len(filtered):
            log.info("cookie_sync: dedupe %d -> %d", before, len(filtered))

        out_path = cookie_file_path(cfg)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "export_time": time.time(),
            "export_time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cookie_count": len(filtered),
            "cookies": filtered,
        }

        write_cookie_state(out_path, data)

        log.info("cookie_sync: exported %d cookies to %s", len(filtered), out_path)
        return len(filtered)

    except Exception as e:
        log.error("cookie_sync: export failed: %s", e)
        return 0


def load_cookies(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = cookie_file_path(cfg)
    if not path.exists():
        log.warning("cookie_sync: cookie file not found: %s", path)
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        cookies = data.get("cookies", [])
        export_time = data.get("export_time", 0)
        age_hours = (time.time() - export_time) / 3600 if export_time else 0

        log.info(
            "cookie_sync: loaded %d cookies from %s (age=%.1fh, exported %s)",
            len(cookies), path, age_hours,
            data.get("export_time_str", "?"),
        )

        if age_hours > 24:
            log.warning("cookie_sync: cookies are %.1f hours old, may be expired!", age_hours)

        return cookies

    except Exception as e:
        log.error("cookie_sync: load failed: %s", e)
        return []


def cookie_file_exists(cfg: Dict[str, Any]) -> bool:
    return cookie_file_path(cfg).exists()


def cookie_file_age_seconds(cfg: Dict[str, Any]) -> Optional[float]:
    path = cookie_file_path(cfg)
    if not path.exists():
        return None
    try:
        data = json.load(path.open("r", encoding="utf-8"))
        export_time = data.get("export_time", 0)
        if export_time:
            return time.time() - export_time
    except Exception:
        pass
    return time.time() - path.stat().st_mtime
