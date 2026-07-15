"""Cookie sync: export/import cookies between browser-control and bot.

browser-control exports cookies to JSON after login.
bot loads cookies from JSON and injects via context.add_cookies().
This way bot does not need persistent context and won't lock the profile.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from browser_common import log


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

        tmp = out_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(out_path)
        try:
            os.chmod(out_path, 0o600)
        except Exception:
            pass

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
