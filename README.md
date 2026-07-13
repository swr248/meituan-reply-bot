# 🍜 美团外卖商家 IM 自动回复机器人

<div align="center">

**多店铺 · 一键 noVNC 登录 · 自动回复 · 推广定时开关 · Cookie 自动续期**

面向美团商家 IM 工作台的开源客服自动化方案。一台 4GB 内存的 VPS 就能托管两家店，浏览器按需启动，电费低到可以忽略。

[主页占位截图：管理台首页](#) · [快速开始](#-快速开始) · [部署指南](#-部署指南) · [常见问题](#-常见问题)

</div>

## 🎬 这是什么、解决什么问题

你开了一家或多家美团外卖店，每天要在 IM 工作台回复新顾客、点击一站式推广开关、定期手动给浏览器重新登录。机器人会处理其中 90% 的重复工作：

- 顾客一发消息，**秒级自动回复**：首条欢迎语、关键词命中回复、保底回复、识别机器人标签会话避免自回复。
- **推广定时开关**：在管理台配时间窗口（如 `11:00–13:30`、`17:00–20:00`），到点自动开、到点自动关，支持跨午夜。
- **一键 noVNC 登录**：账号掉登录就开浏览器自己点几下，cookie 自动写回 bot 可用目录，全程不需要 SSH。
- **Cookie 自动续期**：默认每 20 小时跑一次，按需启动 capture，导出后自动停止。
- **多店铺隔离**：每家店独立 token、cookie、端口、规则、配置；任一家出事不会影响另一家。
- **统一主管理台**：一个页面看两家店 bot/admin/capture/推广/内存/日志/告警。

## ✨ 核心特性

| 模块 | 解决的问题 | 关键能力 |
| --- | --- | --- |
| 自动回复 bot | 顾客发消息要秒回、又不能陷入循环 | 倒计时 / 超时 / 机器人标签三重判据；按消息实例指纹熔断（默认 3 次/条） |
| 推广定时器 | 每天人工开关推广太烦、易忘 | 30 秒主循环 + 默认 1 小时复核 + 跨午夜窗口；状态写文件可追溯 |
| noVNC capture | 账号过期需要手动重新登录 | 管理页“一键启动”，按需拉起 Chromium；启动互斥避免两店同时占用 |
| Cookie 同步 | 多浏览器/Cookie 串台、易丢 | 跨线程锁 + Linux flock + 原子写 + fsync + 0600 权限；新覆盖旧 |
| 主管理台 | 多店运维要登录 N 个面板 | 一个页面看 N 家店的服务/推广/内存/告警；短票据链接给浏览器 |
| 资源治理 | 4GB 机器常驻浏览机会 OOM | systemd slice + MemoryHigh/Max；按需 capture；mem-watch 看门狗 |
| 安全 | token 出现在 URL/日志会造成接管 | master → 店铺 60 秒 HMAC 票据；admin/capture 进程内 10 分钟会话 |

## 🖼 截图占位

> 上线后替换真实截图。建议三张：主管理台、单店管理页、noVNC 浏览器。

| 主管理台 | 单店管理页 | noVNC 浏览器 |
| --- | --- | --- |
| _（待插入：店铺状态、推广状态、内存、告警）_ | _（待插入：Cookie/规则/noVNC 按钮）_ | _（待插入：人工登录或检查推广页面）_ |

## 🧱 架构

```text
                +-----------------------------+
                |  master_admin (FastAPI)     |
                |  <server-ip>:<master_port>  |
                +-------------+---------------+
                              | 60s HMAC ticket
                              v
        +---------------------+---------------------+
        |                                           |
++-------+----------+                       +--------+--------+
+| shop1 admin       |                       | shop2 admin      |
+| :43579            |                       | :30097           |
++-------+----------+                       +--------+--------+
+        | flock /tmp/meituan-capture-global.lock     |
+        v                                           v
+    capture (按需)  <----- Cookie watcher (20h) -----> capture (按需)
+        |                                           |
+        v                                           v
+   Xvfb + Chromium + x11vnc + websockify + noVNC    (同一时刻最多 1 个)
+```

常驻进程只有 bot、admin、master、scheduler。capture 仅在登录、推广定时器触发、cookie 续期时启动，并在任务完成后立即停止。

## 🚀 快速开始

### 1）部署环境

适用 Ubuntu 22.04 LTS 及以上，Python 3.10+，4GB 内存起步（8GB 更舒服）。

```bash
git clone https://github.com/swr248/meituan-reply-bot.git
cd meituan-reply-bot/github-release

python3 -m venv .venv
source .venv/bin/activate
pip install -r shop1/requirements.txt
pip install -r shop2/requirements.txt
```

### 2）配置示例

```bash
cp shop1/config.yaml.example shop1/config.yaml
cp shop2/config.yaml.example shop2/config.yaml
cp master/master.env.example /etc/meituan-master-admin.env
chmod 0600 /etc/meituan-master-admin.env
```

按需修改：

- `auth_token`：单店管理/cookie 同步用的共享密钥
- `promotion_scheduler.windows`：推广时间窗口，列表里可以多个时段，支持跨午夜
- `rules`：关键词与回复
- `phone`：仅用于在 IM 工作台定位会话，不会上传

### 3）启用 systemd 服务

```bash
sudo install -m 0644 etc/meituan-master-admin.service              /etc/systemd/system/
sudo install -m 0644 etc/meituan-reply-bot.service                  /etc/systemd/system/
sudo install -m 0644 etc/meituan-reply-bot-shop2.service            /etc/systemd/system/
sudo install -m 0644 etc/meituan-reply-bot-admin.service            /etc/systemd/system/
sudo install -m 0644 etc/meituan-reply-bot-admin-shop2.service      /etc/systemd/system/
sudo install -m 0644 etc/meituan-promo-scheduler-shop1.service      /etc/systemd/system/
sudo install -m 0644 etc/meituan-promo-scheduler-shop2.service      /etc/systemd/system/
sudo install -m 0644 etc/meituan.slice                              /etc/systemd/system/
sudo install -m 0644 etc/meituan-capture-locks.conf                 /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/meituan-capture-locks.conf
sudo install -m 0755 etc/meituan-cookie-watch.sh                    /usr/local/sbin/meituan-cookie-watch
sudo install -m 0644 etc/meituan-mem-watch.sh                       /usr/local/sbin/meituan-mem-watch.sh
sudo install -m 0644 etc/meituan-mem-watch.timer                    /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now \
  meituan-reply-bot.service meituan-reply-bot-shop2.service \
  meituan-reply-bot-admin.service meituan-reply-bot-admin-shop2.service \
  meituan-master-admin.service \
  meituan-promo-scheduler-shop1.service meituan-promo-scheduler-shop2.service
sudo systemctl enable --now meituan-cookie-watch@shop1.timer meituan-cookie-watch@shop2.timer
sudo systemctl enable --now meituan-mem-watch.timer
```

### 4）首次登录与 Cookie 写入

1. 打开主管理台：`http://<server-ip>:<master_port>/?token=<MASTER_ADMIN_TOKEN>`
2. 进入单店管理页 → 点击“一键启动 noVNC”
3. 在 noVNC 浏览器里手动登录（首次可能有滑块），确认进入 IM 工作台
4. 系统自动把 cookie 导出到 `~/.meituan-reply-bot/state/cookies.json`，bot 立刻接管
5. 之后默认每 20 小时自动续期一次

### 5）配置推广定时开关

管理台 → 单店 → “推广定时”页 → 启用并填入时间窗口，例如：

```yaml
promotion_scheduler:
  enabled: true
  check_interval_sec: 30        # 主循环检测频率（30 秒）
+  reconcile_interval_sec: 3600  # 默认每 1 小时复核一次（900-21600）
  windows:
    - { start: "11:00", end: "13:30" }  # 午餐高峰
    - { start: "17:00", end: "20:00" }  # 晚餐高峰
```

到点自动开启、到点自动关闭；中途关闭后下次重新进入窗口会自动再开。

## 🧪 测试

```bash
cd github-release
python -m pytest -q \
  shop1/test_bot_guards.py shop2/test_bot_guards.py \
  shop1/test_promo_scheduler.py shop2/test_promo_scheduler.py \
  shop1/test_admin_promo.py shop2/test_admin_promo.py \
  shop1/test_cookie_sync_atomic.py shop2/test_cookie_sync_atomic.py \
  shop1/test_cookie_reload.py shop2/test_cookie_reload.py \
  shop1/test_cookie_capture_auth.py shop2/test_cookie_capture_auth.py \
  master/test_auth_ticket.py
```

CI 会在 push 与 PR 时自动执行。

## 🛠 运维建议

### 资源预算（4GB 主机）

| 服务 | MemoryHigh | MemoryMax | 说明 |
| --- | --- | --- | --- |
| meituan.slice | 2.2G | 2.7G | 总预算 |
| bot ×2 | 1.2G | 1.5G | headed Chromium + Xvfb |
| capture | 650M | 900M | 按需，按时停止 |
| admin / scheduler / master | 64–256M | – | 不设硬上限，靠 slice 兜底 |

实测常驻约 1.1GB（两店 bot + admin ×2 + master + scheduler ×2），capture 仅在需要时拉起。

### Cookie 自动续期

- `meituan-cookie-watch@.timer` 每 20 小时触发一次；运行时按需拉起 capture，导出后立即停止。
- 写入采用跨线程锁 + Linux flock + 唯一临时文件 + fsync + 0600 权限 + 版本号拒绝旧覆盖新。

### 推广定时

- 30 秒主循环判断是否进入/离开窗口；进入即启动 capture 打开，离开即关闭。
- 每 1 小时（或可配）重新核验真实开关，避免被外部改动蒙蔽；失败下一 tick 重试。

### 内存看门狗

- `meituan-mem-watch.timer` 5 分钟一次；bot 超过阈值或 capture 在低内存下自动停止。

## 🔐 安全说明

- `config.yaml`、`/etc/meituan-master-admin.env` 等敏感文件均不进仓库。
- master 不下发店铺长期 token；用 60 秒 HMAC 票据 + 店铺/目标/jti 强校验，过期或重复使用一律 401。
- admin/capture 使用进程内 10 分钟短会话，单店 `/api/shops` 只返回当前店。
- 推广 URL 在日志中已脱敏（去除 token、acctId、wmPoiId、bsid、device_uuid）。

## 🧰 技术栈

- Python 3.10+，FastAPI、Playwright、systemd、noVNC、Chromium
- 仅依赖 Python 标准库 + 上述库，不引入额外大型框架

## 📜 License

MIT