"""Reply state and recent reply events."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

_LOCK = threading.Lock()


class ReplyState:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "replies.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = {str(k): v for k, v in data.items()}
        except Exception:
            self._cache = {}

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception:
            pass

    @staticmethod
    def make_key(customer: str, text: str, bubble_index: int, bubble_total: int) -> str:
        return f"{customer}|{text}|{bubble_index}/{bubble_total}"

    def already_replied(self, key: str, ttl_seconds: int = 3600) -> bool:
        with _LOCK:
            ts = self._cache.get(key)
            if ts is None:
                return False
            try:
                age = time.time() - float(ts)
            except (TypeError, ValueError):
                return False
            return age <= ttl_seconds

    def mark_replied(self, key: str) -> None:
        with _LOCK:
            self._cache[key] = time.time()
            self._trim_unlocked()
            self._flush()

    def stats(self) -> Dict[str, Any]:
        with _LOCK:
            return {"records": len(self._cache), "path": str(self.path)}

    def get_number(self, key: str) -> float:
        with _LOCK:
            try:
                return float(self._cache.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

    def set_number(self, key: str, value: float) -> None:
        with _LOCK:
            self._cache[key] = float(value)
            self._trim_unlocked()
            self._flush()

    def set_value(self, key: str, value: Any) -> None:
        with _LOCK:
            self._cache[key] = value
            self._trim_unlocked()
            self._flush()

    def get_value(self, key: str, default: Any = None) -> Any:
        with _LOCK:
            return self._cache.get(key, default)

    def add_event(self, event: Dict[str, Any], limit: int = 80) -> None:
        with _LOCK:
            events = self._cache.get("reply_events", [])
            if not isinstance(events, list):
                events = []
            events.append({"ts": time.time(), **event})
            self._cache["reply_events"] = events[-limit:]
            self._trim_unlocked()
            self._flush()

    def recent_events(self, limit: int = 30) -> List[Dict[str, Any]]:
        with _LOCK:
            events = self._cache.get("reply_events", [])
            if not isinstance(events, list):
                return []
            return list(reversed(events[-limit:]))

    def clear(self) -> None:
        with _LOCK:
            self._cache.clear()
            self._flush()

    def clear_watermarks(self) -> None:
        """Remove all watermark keys (last_peer_fp:*) so bot re-detects pending cards on restart."""
        with _LOCK:
            keys_to_remove = [k for k in self._cache if k.startswith("last_peer_fp:")]
            for k in keys_to_remove:
                del self._cache[k]
            if keys_to_remove:
                self._flush()
                print(f"state: cleared {len(keys_to_remove)} watermark keys on startup")

    def _trim_unlocked(self) -> None:
        if len(self._cache) <= 5000:
            return
        protected = {k: v for k, v in self._cache.items() if k == "reply_events" or k.startswith("last_peer_fp:")}
        numeric_items = []
        for key, value in self._cache.items():
            if key in protected:
                continue
            try:
                numeric_items.append((key, value, float(value)))
            except (TypeError, ValueError):
                continue
        keep = sorted(numeric_items, key=lambda item: item[2])[-4500:]
        self._cache = {key: value for key, value, _ in keep}
        self._cache.update(protected)
