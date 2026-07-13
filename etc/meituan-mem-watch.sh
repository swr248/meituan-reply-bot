#!/usr/bin/env bash
# Memory threshold auto-restart for meituan bots.
set -u

THRESHOLD_MB=${THRESHOLD_MB:-1300}
MIN_GAP_MIN=${MIN_GAP_MIN:-30}
LOG=/home/ubuntu/meituan-reply-bot/logs/mem-restart.log
mkdir -p "$(dirname "$LOG")"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "[$(ts)] memory-watch tick threshold=${THRESHOLD_MB}MB" >> "$LOG"

SLICE_MB=$(($(systemctl show meituan.slice -p MemoryCurrent --value 2>/dev/null || echo 0) / 1024 / 1024))
AVAILABLE_MB=$(awk '/MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
echo "[$(ts)] slice=${SLICE_MB}MB available=${AVAILABLE_MB}MB" >> "$LOG"
if [ "$AVAILABLE_MB" -lt 512 ]; then
  systemctl stop meituan-capture-meituan-reply-bot.service meituan-capture-meituan-reply-bot-shop2.service >/dev/null 2>&1 || true
  echo "[$(ts)] low memory: stopped on-demand capture services" >> "$LOG"
fi

restart_if_over() {
  local svc="$1" marker="$2"
  local cur
  cur=$(systemctl show "$svc" -p MemoryCurrent --value 2>/dev/null)
  if [ -z "$cur" ] || [ "$cur" = "[not set]" ]; then return; fi
  local mb=$((cur / 1024 / 1024))
  if [ "$mb" -lt "$THRESHOLD_MB" ]; then return; fi
  if [ -f "$marker" ]; then
    local last
    last=$(($(date +%s) - $(stat -c %Y "$marker")))
    if [ "$last" -lt $((MIN_GAP_MIN * 60)) ]; then
      echo "[$(ts)] $svc ${mb}MB over threshold but gap=${last}s < ${MIN_GAP_MIN}m, skip" >> "$LOG"
      return
    fi
  fi
  echo "[$(ts)] $svc ${mb}MB over ${THRESHOLD_MB}MB, restarting" >> "$LOG"
  touch "$marker"
  systemctl restart "$svc"
}

restart_if_over "meituan-reply-bot.service" /tmp/.mem-restart-shop1
restart_if_over "meituan-reply-bot-shop2.service" /tmp/.mem-restart-shop2
