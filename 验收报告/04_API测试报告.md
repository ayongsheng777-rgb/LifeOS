# 04 API 测试报告 — LifeOS V2.0

## 一、接口规范

- 风格：REST JSON，统一 `/api` 前缀，共 60+ 端点（main.py 集中注册）
- 鉴权：中间件 `auth_guard`（main.py L190）统一实施 Bearer 校验；白名单仅 `/api/health`、`/api/connector/webhook` 与 `/api/auth/` 前缀
- 状态码使用规范：200 成功 / 401 未授权 / 404 资源不存在 / 422 参数校验失败（Pydantic）/ 429 限流（登录与 Agent 对话）
- 错误体格式统一：`{"error": "...", "code": "..."}` 或 FastAPI 422 detail

## 二、实测记录

### 鉴权矩阵（实测）

| 用例 | 结果 | 状态码 |
|---|---|---|
| 无 token 访问 /api/status | 拒绝 ✅ | 401 |
| 无 token 访问 /api/todos | 拒绝 ✅ | 401 |
| 伪造 token（fake.token.sig） | 拒绝 ✅ | 401 |
| 篡改真实 token 尾部 3 字符 | 拒绝 ✅ | 401 |
| 错误 OTP 登录 | 拒绝 ✅ | 401 `OTP_INVALID` |
| 正确 TOTP 登录 | 通过 ✅ | 200 返回 token+ttl |
| /api/health 无 token | 放行（设计如此）✅ | 200 |

### 参数校验（实测）

| 用例 | 结果 |
|---|---|
| amount="abc"（类型错误） | 422 拒绝 ✅ |
| amount=-100（负数） | 422 拒绝 ✅ |
| 缺少必填字段 type / key | 422 并指明缺失字段 ✅ |

### 性能抽测（每接口 3 次，本机回环）

| 端点 | 耗时 ms |
|---|---|
| /api/health | 14 / 16 / 15 |
| /api/status | 15 / 14 / 3 |
| /api/todos | 32 / 30 / 29 |
| /api/models | 25 / 14 / 14 |
| /api/ai/usage | 19 / 30 / 6 |
| 20 并发 /api/status | 20/20 成功，总 164ms，单次 max 162ms |

### 限流（代码审查 + 实测印证）

- 登录：IP 维度失败计数锁定，触发返回 429 + `Retry-After: 30` ✅
- Agent 对话：进程内滑动窗口 30 次/分（env `AI_RATE_LIMIT` 可调）✅

## 三、发现问题

| 编号 | 问题 | 等级 |
|---|---|---|
| API-1 | `/api/auth/otp-reset` 位于 `/api/auth/` 白名单前缀下，**无会话即可达**（注释声称"已过 auth_guard"与实现不符）。虽有 OTP 动态码兜底、实测无 otp 返回 401/422，但攻击面暴露 | P2 |
| API-2 | `/api/models/fetch` 接受任意外部 base_url，构成 SSRF 探测面（有鉴权缓解） | P2 |
| API-3 | 会话令牌为「签名+内存集合」双校验，安全性好；但集合纯内存，重启全部失效，无服务端主动吊销能力 | P2 |
| API-4 | 登录限流信任 `X-Forwarded-For`（main.py L137），直连部署时可伪造 IP 绕过锁定 | P2 |

**API 评分：85/100** —— 规范、鉴权扎实、响应快；扣分在 otp-reset 白名单、SSRF 面、内存态会话。
