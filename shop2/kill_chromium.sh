#!/bin/bash
# 杀掉残留 chromium 进程，避免 profile 被锁。
# 注意：pkill 自身命令行不能包含 "browser-profile"，否则会自杀。
set +e
ps -eo pid,cmd | awk '/[c]hrome/ && /[b]rowser-profile/ {print $1}' | xargs -r kill -9 2>/dev/null
exit 0
