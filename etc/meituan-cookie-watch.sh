#!/usr/bin/env bash
set -euo pipefail

NAME="${1:-shop1}"
case "$NAME" in
  shop1)
    ROOT="/home/ubuntu/meituan-reply-bot"
    PORT=5901
    SERVICE="meituan-capture-meituan-reply-bot.service"
    MAIN_COOKIE="/home/ubuntu/.meituan-reply-bot/state/cookies.json"
    ;;
  shop2)
    ROOT="/home/ubuntu/meituan-reply-bot-shop2"
    PORT=5902
    SERVICE="meituan-capture-meituan-reply-bot-shop2.service"
    MAIN_COOKIE="/home/ubuntu/.meituan-reply-bot-shop2/state/cookies.json"
    ;;
  *)
    echo "unknown shop: $NAME" >&2
    exit 1
    ;;
esac

exec 8>"/run/meituan-capture-global.lock"
flock -w 900 8 || { echo "[$NAME] global capture lock timeout" >&2; exit 1; }
exec 9>"/run/meituan-capture-${NAME}.lock"
flock -w 600 9 || { echo "[$NAME] capture lock timeout" >&2; exit 1; }

TOKEN=$(python3 - "$ROOT/capture/config.yaml" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print((cfg.get("server") or {}).get("auth_token", ""))
PY
)
[ -n "$TOKEN" ] || { echo "[$NAME] capture auth_token missing" >&2; exit 1; }

BASE="http://127.0.0.1:${PORT}"
CAPTURE_COOKIE="$ROOT/capture/profiles/im/state/cookies.json"
STARTED=0
trap 'if [ "$STARTED" = "1" ]; then systemctl stop "$SERVICE" >/dev/null 2>&1 || true; fi' EXIT

wait_health() {
  local i
  for i in $(seq 1 90); do
    if HEALTH=$(curl -fsS --max-time 60 "$BASE/api/health?token=$TOKEN&role=im" 2>/dev/null); then
      if python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d.get("ok") and d.get("roles",{}).get("im")' "$HEALTH" 2>/dev/null; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

if systemctl is-active --quiet "$SERVICE"; then
  echo "[$NAME] replacing ownerless active capture before cookie refresh"
  systemctl stop "$SERVICE"
fi
echo "[$NAME] starting capture service for cookie refresh"
systemctl start "$SERVICE"
STARTED=1
wait_health || { echo "[$NAME] capture did not become healthy" >&2; exit 1; }

KEEPALIVE_URL="https://shangoue.meituan.com/"
curl -fsS -X POST --get \
  --data-urlencode "token=$TOKEN" \
  --data-urlencode "role=im" \
  --data-urlencode "wait_ms=5000" \
  --data-urlencode "url=$KEEPALIVE_URL" \
  "$BASE/api/goto" >/dev/null

RESP=$(curl -fsS -X POST "$BASE/api/export-cookies?token=$TOKEN&role=im")
SUMMARY=$(PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" - "$RESP" "$CAPTURE_COOKIE" "$MAIN_COOKIE" <<'PY'
import json, sys
from pathlib import Path

from cookie_sync import write_cookie_state

response = json.loads(sys.argv[1])
source = sys.argv[2]
target = sys.argv[3]
if not response.get("ok"):
    raise SystemExit("capture export failed")
data = json.load(open(source, encoding="utf-8"))
cookies = data.get("cookies", [])
auth_names = {"token", "accessToken", "acctId", "wmPoiId", "JSESSIONID"}
auth = [c for c in cookies if c.get("name") in auth_names and c.get("value")]
if not any(c.get("name") in {"token", "accessToken"} for c in auth):
    raise SystemExit(f"refusing anonymous cookie export: count={len(cookies)} auth={len(auth)}")
updated = write_cookie_state(Path(target), data)
print(json.dumps({"cookie_count": len(cookies), "auth_count": len(auth), "source": source, "updated": updated}, ensure_ascii=False))
PY
)

chown ubuntu:ubuntu "$MAIN_COOKIE"
trap - EXIT
if [ "$STARTED" = "1" ]; then
  systemctl stop "$SERVICE" >/dev/null 2>&1 || true
fi
echo "[$NAME] capture IM cookie synced to $MAIN_COOKIE; $SUMMARY"
