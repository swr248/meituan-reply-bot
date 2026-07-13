# 🍜 美团外卖 IM 自动回复机器人

<div align="center">

**多店铺自动化客服解决方案** · 关键词智能回复 · 防重复刷屏 · Cookie 持久化

</div>

## 🎯 这是什么

面向美团外卖 IM 工作台的自动回复系统，支持多店铺独立运行。
核心目标：**让顾客在有新消息时，机器人稳定、快速、可控地回复**。

---

## 🧩 功能速览

- **消息监听**：自动识别“正在接待”中的顾客会话，依据倒计时/超时状态触发回复。
- **回复策略**：
  - 首条消息先发欢迎语（可自定义）。
  - 后续消息按关键词匹配回复；无匹配时走保底回复。
- **去重防刷**：
  - 同一会话最多自动回复 3 次，避免页面状态异常时循环发送。
  - 会话按订单号/门店新客 + 脱敏顾客名隔离，避免 `v**` 这类脱敏名跨会话误伤。
- **店铺隔离**：多店铺独立配置（token、端口、Cookie、规则），互不干扰。
- **统一监控**：总览页展示店铺状态、运行状态、内存占用、日志与告警。
- **Cookie 生命周期管理**：导出/保活/异常提示。
- **自我识别**：过滤含店铺名、机器人标签（`[订单]`、`[机器人...]`）的消息，避免误把店方消息当顾客消息。

---

## 🗂 项目结构

```text
github-release/
├── master/                    # 总控管理台（统一入口）
│   └── master_admin.py
├── shop1/                     # 店铺1（示例）
│   ├── bot.py                 # 核心机器人逻辑
│   ├── admin.py               # 店铺管理页
│   ├── config.yaml.example    # 配置模板（复制为 config.yaml）
│   ├── rules.py               # 关键词规则
│   ├── state.py               # 状态/去重记录持久化
│   ├── cookie_sync.py         # Cookie 导出与同步
│   ├── capture/               # 按需 noVNC、Cookie 与推广浏览器
│   └── ...
├── shop2/                     # 店铺2（结构同 shop1）
├── etc/                       # systemd 单元文件与部署文件
├── .github/workflows/         # CI 配置
├── .gitignore                 # 敏感文件忽略
└── README.md                  # 当前说明文档
```

---

## 🚀 快速开始

### 1）安装依赖

```bash
cd github-release
pip install -r shop1/requirements.txt
```

### 2）配置

复制示例配置，并分别填写两家店的真实值：

```bash
cp shop1/config.yaml.example shop1/config.yaml
cp shop2/config.yaml.example shop2/config.yaml
```

重点字段：

- `token`
- `server_port`
- `service_port`
- `phone`
- `first_message` / `fallback`
- `rules`（关键词与回复）

### 3）启动服务

```bash
# 常驻服务
sudo systemctl enable --now meituan-reply-bot.service
sudo systemctl enable --now meituan-reply-bot-shop2.service
sudo systemctl enable --now meituan-reply-bot-admin.service
sudo systemctl enable --now meituan-reply-bot-admin-shop2.service
sudo systemctl enable --now meituan-master-admin.service
sudo systemctl enable --now meituan-promo-scheduler-shop1.service
sudo systemctl enable --now meituan-promo-scheduler-shop2.service

# 20 小时 Cookie 刷新
sudo systemctl enable --now meituan-cookie-watch@shop1.timer
sudo systemctl enable --now meituan-cookie-watch@shop2.timer

# capture 不启用开机自启，由管理页、Cookie watcher 或推广调度按需启动
```

### 4）访问界面

```text
主控： http://<server-ip>:<master_port>/?token=<MASTER_ADMIN_TOKEN>
店铺1： http://<server-ip>:<shop1_port>/
店铺2： http://<server-ip>:<shop2_port>/
```

主控凭证通过 `/etc/meituan-master-admin.env` 配置。该文件必须为 root 所有并设置 `0600` 权限。

---

## 🔁 回复规则（建议理解）

- 检测到新消息（来自顾客）后即触发一次处理。
- 首条：发送欢迎语。
- 第二条起：先按关键词匹配回复。
- 无命中：发送 fallback。
- 遇到重复消息也会按规则处理，但同一会话受“最多 3 次自动回复”保护。

---

## 🧪 排障与自检

常见排查顺序：

1. 管理页是否显示 `运行中`。
2. 会话列表是否出现倒计时（59s→1s）或超时提示。
3. 最近日志里是否有 `scan`/`reply` 关键字。
4. 若出现 cookie 告警，先到对应店铺浏览器页重新导出。

建议动作：

- 先发一条测试消息验证链路。
- 若无回复，先检查系统时间、Cookie 有效期与页面状态。
- 必要时重启对应店铺服务（或 master）后观察恢复。

---

## 🧰 开发与测试

```bash
cd github-release
python -m pytest -q \
  shop1/test_rules.py shop2/test_rules.py \
  shop1/test_bot_guards.py shop2/test_bot_guards.py \
  shop1/test_promo_scheduler.py shop2/test_promo_scheduler.py
```

项目 CI 会在 `push` / `pull_request` 时执行测试。

---

## 🛠 运维建议

### ⏱ Cookie 自动刷新 + 按需推广

- Cookie 保活默认每 **20 小时** 触发一次（`meituan-cookie-watch@.timer`）；
- 触发时会临时启动对应店铺的 capture 服务，导出后自动停止；
- 推广定时开关由 `meituan-promo-scheduler-shop{1,2}.service` 常驻但轻量；
- 推广调度在期望状态变化时立即启动 capture，成功后默认每 1 小时复核一次；失败按 30 秒主循环重试；
- 推广状态写入 `promo_scheduler_status.json`，管理页会显示“按时间应开启/关闭”和“实际开启/关闭”。

### 🧠 4GB 内存运行

- capture 使用按需启动，并通过 `/run/meituan-capture-global.lock` 串行化，避免两店 capture 同时启动；
- bot 空闲后会完整重建浏览器栈，释放 Chromium/Playwright 长期累积内存；
- systemd 对每个 bot 设置 `MemoryHigh=1200M`、`MemoryMax=1500M`；capture 设置 `MemoryHigh=650M`、`MemoryMax=900M`；
- `meituan.slice` 总预算为 `MemoryHigh=2200M`、`MemoryMax=2700M`，适配 4GB 主机并保留系统余量；
- 旧 `meituan-browser-control*.service` 已退役并应保持 masked，唯一浏览器入口是 capture。
- `/etc/tmpfiles.d/meituan-capture-locks.conf` 统一创建跨服务 capture 锁；admin、推广调度和 Cookie watcher 不会互相接管浏览器会话。
- 内存看门狗默认超过 `1100MB` 会重启对应 bot；
- 建议启用 2GB swap 作为 4GB 机器兜底。

- 日志建议按 7 天轮转（可通过日志策略文件管理）。
- 关键操作优先走管理页按钮，避免直接改配置导致服务不一致。
- 变更前先备份配置/日志目录，方便回滚。

---

## 🔐 安全说明

- `config.yaml` 已在 `.gitignore` 中忽略，避免提交敏感信息。
- 仓库内仅保留示例配置 `config.yaml.example`。
- Cookie、浏览器 profile 与会话状态文件请勿提交。

---

## 🧰 技术栈

- Python 3
- Playwright
- FastAPI
- systemd
- VNC
