# 楚烽 LifeOS V2.0

个人 AI 生活/生产力中枢：飞书作为统一入口，Agent 路由 + 可插拔 Skill，AI 多模型层，长期记忆（Qdrant 向量库，可选，需配置 `EMBEDDING_MODEL`）。

## 架构

```
飞书 App（WS 长连接）
   │  im.message.receive_v1
   ▼
FeishuBotService（daemon 线程收消息 → 主循环派发）
   │  MessagePayload
   ▼
AgentRouter（Clean-Slate 隔离 / 意图识别 / Skill 匹配）
   │  未命中 Skill → AI 默认对话
   ▼
AI 统一客户端（OpenAI 兼容多模型，缓存+限流+推理兼容）
   ▲
OTP 中间件（Bearer）守护 /api/*（除 /api/health、/api/auth/*）
```

## 目录

```
app/
  main.py              FastAPI 网关 + 鉴权中间件 + 端点 + lifespan
  config.py            中央配置（AI 多模型 / 飞书运行态 / 代理）
  auth.py              OTP(TOTP)+会话令牌（纯标准库）
  backup.py            数据突变备份（本地 + NAS：SMB/SFTP/FTP/WebDAV 多目标）
  feishu.py            飞书 WS 长连接 Bot
  feishu_deviceflow.py 飞书扫码授权流（RFC 8628）
  agent/router.py      Agent 路由 + 隔离
  skills/loader.py     Skill 自动发现；skills/*/skill.yaml+handler.py
  memory/              短期(Redis) / 长期(Qdrant) 记忆
  ai/                  统一模型客户端 + prompt + analyzer + registry + news_ai
```

## 快速开始（本地开发）

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 按需填写（程序启动时会自动加载 .env）
# 首次启动：访问 /api/auth/setup 拿到 otpauth URI + 二维码，用验证器绑定
# 公网经 Cloudflare Tunnel（lifeos.yshost.de5.net → 192.168.59.56:7208）访问，故监听 7208
uvicorn app.main:app --reload --host 0.0.0.0 --port 7208
```

## 飞书接入

- 方式 A（推荐）：前端调 `POST /api/feishu/qrcode` 拿 `scan_url`（指向 `accounts.feishu.cn` 官方页），用户扫码授权 → 轮询 `GET /api/feishu/qrcode/status` 拿到 `app_id/secret` 自动落库并热启动 Bot。
- 方式 B：飞书开放平台手动建自建应用 → 开「机器人」→ 订阅 `im.message.receive_v1` → 填 `FEISHU_APP_ID/SECRET`。

## 安全红线

- 密钥文件（`data/`）已 `.gitignore`，绝不提交。
- 公网部署用固定 `OTP_SECRET` + 强 `SESSION_SECRET`，避免绑定页暴露。
- 飞书管理端点走与其他 API 相同的 Bearer 鉴权，不开白名单。

## 数据突变备份（本地磁盘 + NAS）

控制台「💾 备份」面板（前端 `Backup.vue`）提供在线配置、手动触发、实时日志、状态查看与还原。

- **备份目标（多目标，任一失败互不影响）**：本地磁盘、`SMB/CIFS`、`SFTP`、`FTP`、`WebDAV`。
  - 每个目标可配：传输方式、主机、端口、共享名（SMB）、远程目录、用户名、密码（UI 密码框 + 接口 `****` 遮罩）。
  - 凭证存 `data/settings_runtime.json`（已 `.gitignore`，绝不提交）；`GET` 接口返回 `****`，保存时填 `****` 表示保留原密码。
- **备份对象**：PostgreSQL（`pg_dump`）、Redis（RDB 快照）、Qdrant（向量快照，仅容器运行才备）、配置目录 `data/`（OTP 密钥、运行时设置等）。
- **策略**：每日 `backup_schedule_hour`（默认 03:00）定时；保留最近 `backup_retention_days`（默认 7 天）；手动 `POST /api/backup/run` 即时触发。
- **还原（危险，二次确认）**：选目标 → 加载备份时间点 → 勾选组件（PostgreSQL/Redis/Qdrant/配置目录）→ 确认覆盖线上。配置目录还原会保留当前面板/调度设置。
- **后端接口**：`/api/backup/{config,status,run,points,restore,log}`。状态缓存写入 `data/backup_status/<目标>.json`（不再在目标根写隐藏文件）。

## 公网访问（Cloudflare Tunnel）

本项目经同机 cloudflared（隧道 `yshost.de5.net`）对外暴露：

```
lifeos.yshost.de5.net  ──▶  cloudflared  ──▶  http://192.168.59.56:7208  ──▶  LifeOS(uvicorn 0.0.0.0:7208)
```

- **本机服务必须监听 `0.0.0.0:7208`**（Docker 已映射 `7208:8000`；host 直跑加 `--host 0.0.0.0 --port 7208`）。
- 隧道 ingress 规则（`cloudflared/update-ingress.py` 维护）须含：
  `{"hostname": "lifeos.yshost.de5.net", "service": "http://192.168.59.56:7208"}`
- 应用层已自保：`/api/*` 除 `/api/health`、`/api/auth/*` 外均须 Bearer 鉴权，公开面仅 OTP 登录与状态。
- ⚠️ 该隧道未叠加 Cloudflare Access 登录墙，第一道防线是 LifeOS 自身的 OTP；公网部署务必用固定 `OTP_SECRET` + 强 `SESSION_SECRET`。
