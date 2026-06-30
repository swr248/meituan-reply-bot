#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p logs state

# 1) 杀掉残留进程
pkill -9 -f "browser-profile" 2>/dev/null || true
pkill -9 -f "x11vnc" 2>/dev/null || true
pkill -9 -f "Xvfb :99" 2>/dev/null || true
sleep 1

# 2) 启动 X display :99，并 export 给所有子进程
export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 -ac +extension GLX +render -noreset >> logs/xvfb.log 2>&1 &
XVFB_PID=$!

# 等 Xvfb 完全就绪
for i in 1 2 3 4 5 6 7 8 9 10; do
  if xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "[ok] Xvfb :99 ready (${i}s)"
    break
  fi
  sleep 1
done

# 3) 启动 x11vnc：-display :99 显式指定
#    不使用 -bg，避免 fork 模式丢失 DISPLAY
x11vnc -display :99 -rfbport 5900 -forever -shared -nopw -noxdamage \
  -logfile /home/ubuntu/meituan-reply-bot/logs/x11vnc.log 2>>logs/x11vnc.err &
X11VNC_PID=$!

# 等 5900 端口就绪
for i in 1 2 3 4 5 6 7 8 9 10; do
  if ss -ltn 2>/dev/null | grep -q ':5900 '; then
    echo "[ok] x11vnc 5900 ready (${i}s)"
    break
  fi
  sleep 1
done

# 验证 x11vnc 真的连到 :99（log 第一行）
sleep 1
if grep -E 'Using X display :99' logs/x11vnc.log >/dev/null 2>&1; then
  echo "[ok] x11vnc connected to :99"
else
  echo "[fail] x11vnc did NOT connect to :99, log shows:"
  head -5 logs/x11vnc.log
  kill $X11VNC_PID $XVFB_PID 2>/dev/null || true
  exit 1
fi

# 4) 启动 Python 服务
trap "kill $XVFB_PID $X11VNC_PID 2>/dev/null || true" EXIT
echo "[info] starting python on DISPLAY=$DISPLAY"
exec .venv/bin/python remote_browser.py --config config.yaml
