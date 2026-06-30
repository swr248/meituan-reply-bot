#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p logs state

pkill -9 -f "/home/ubuntu/.meituan-reply-bot-shop2/browser-profile" 2>/dev/null || true
pkill -9 -f "Xvfb :97" 2>/dev/null || true
sleep 1

export DISPLAY=:97
Xvfb :97 -screen 0 1440x900x24 -ac +extension GLX +render -noreset >> logs/xvfb.log 2>&1 &
XVFB_PID=$!

for i in 1 2 3 4 5 6 7 8 9 10; do
  if xdpyinfo -display :97 >/dev/null 2>&1; then
    echo "[ok] Xvfb :97 ready (${i}s)"
    break
  fi
  sleep 1
done

x11vnc -display :97 -rfbport 5903 -forever -shared -nopw -noxdamage   -logfile /home/ubuntu/meituan-reply-bot-shop2/logs/x11vnc.log 2>>logs/x11vnc.err &
X11VNC_PID=$!

for i in 1 2 3 4 5 6 7 8 9 10; do
  if ss -ltn 2>/dev/null | grep -q ":5903 "; then
    echo "[ok] x11vnc 5903 ready (${i}s)"
    break
  fi
  sleep 1
done

watch_vnc() {
  while true; do
    sleep 10
    if ! kill -0 "$X11VNC_PID" 2>/dev/null; then
      echo "[fail] x11vnc exited, restarting service" >&2
      kill $$ 2>/dev/null || true
      exit 1
    fi
    if ! ss -ltn 2>/dev/null | grep -q ":5903 "; then
      echo "[fail] x11vnc port 5903 closed, restarting service" >&2
      kill $$ 2>/dev/null || true
      exit 1
    fi
  done
}
watch_vnc &
WATCH_PID=$!
trap "kill $XVFB_PID $X11VNC_PID $WATCH_PID 2>/dev/null || true" EXIT
echo "[info] starting python on DISPLAY=$DISPLAY"
exec .venv/bin/python remote_browser.py --config config.yaml
