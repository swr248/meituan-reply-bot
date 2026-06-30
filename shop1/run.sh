#!/usr/bin/env bash
# bot 守护启动器：xvfb-run + bot.py 用 while 循环串起来
# - 如果 bot 进程退出（异常 / OOM / 主动退出），5s 后自动重启
# - 如果 Xvfb 死了但 bot 还活着（卡在 chromium launch 失败），bot 自己检测后会退出
# - 这样 systemd 看 bot 一直 active；但每跑一段时间会重起一次，Xvfb 总是新的
set -u
cd "$(dirname "$0")"
mkdir -p logs state
LOG=logs/bot.log
while true; do
  echo "$(date '+%Y-%m-%d %H:%M:%S') bot-runner: starting xvfb + bot" >> "$LOG"
  xvfb-run -a --server-args='-screen 0 1440x900x24' .venv/bin/python -u bot.py --config config.yaml "$@" >> "$LOG" 2>&1
  rc=$?
  echo "$(date '+%Y-%m-%d %H:%M:%S') bot-runner: bot exited rc=$rc, restarting in 5s" >> "$LOG"
  sleep 5
done