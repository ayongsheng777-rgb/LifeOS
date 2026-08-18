# 09 BUG 清单 — LifeOS V2.0 验收

> 等级定义：P0 严重（数据丢失/安全漏洞/无法运行）｜P1 重大（核心功能错误/数据错误）｜P2 一般（功能缺陷/体验问题）｜P3 优化建议

## P0：无

本次验收未发现数据丢失、可直接利用的安全漏洞或系统无法运行的问题。

## P1（2 项）

| # | 问题描述 | 影响范围 | 复现方式 | 修复建议 |
|---|---|---|---|---|
| P1-1 | 技能代码包热加载零沙箱：handler_code 直写磁盘并 importlib 执行（api_skill.py L120-144） | 任何有效会话 = 服务器任意代码执行 | POST /api/skills/package 提交任意 Python 代码即被加载 | 自用可接受但文档须显著标注；产品化前加沙箱或代码签名白名单 |
| P1-2 | 全部密钥明文落盘 data/settings_runtime.json；otp_secret/session_secret 无权限控制 | 拿到 data 目录 = 全部凭据泄露；备份打包 data 会连带外泄 | 直接打开该 JSON 可见 AI key/备份密码明文 | 密码字段对称加密（密钥走 env）；备份排除 otp/session 密钥文件 |

## P2（10 项）

| # | 问题描述 | 影响范围 | 修复建议 |
|---|---|---|---|
| P2-1 | SFTP 备份 paramiko AutoAddPolicy 不校验主机密钥 | 备份通道可被中间人劫持 | 固定 host key 或首次确认后落盘 known_hosts |
| P2-2 | PG 5433 绑定 0.0.0.0，局域网可直连 | 数据库暴露面扩大 | 改绑 127.0.0.1 |
| P2-3 | 兜底密码 lifeos_pg_2026 硬编码于 compose 与 backup.py L30 | 源码泄露=密码泄露 | 强制 env 注入，去掉默认兜底 |
| P2-4 | TOTP 同窗口内动态码可重放 | 30 秒内截获的码可复用登录 | 记录已用码时间窗，拒绝重放 |
| P2-5 | 登录限流信任 X-Forwarded-For（main.py L137） | 直连部署可伪造 IP 绕过锁定 | 仅在被信任反代后启用该头 |
| P2-6 | /api/auth/otp-reset 无会话可达（白名单前缀，注释与实现不符） | 攻击面暴露（有 OTP 兜底） | 移出白名单或要求已认证会话+OTP 双因子 |
| P2-7 | 会话纯内存 VALID_TOKENS，重启全失效、无法主动吊销 | 运维重启踢出所有登录 | 会话落 Redis，支持吊销列表 |
| P2-8 | 前端 token localStorage 明文 | XSS 一旦得手即窃取会话 | 改 httpOnly Cookie 或内存态+刷新重登 |
| P2-9 | /api/models/fetch 接受任意 base_url（SSRF 面，有鉴权缓解） | 可探测内网服务 | base_url 白名单或协议/端口限制 |
| P2-10 | ai_usage 无索引 + summary 全表扫描 Python 聚合 | 数据量大后统计接口变慢 | 加 user_id/created_at 索引，聚合下推 SQL |

## P3（7 项）

| # | 问题 | 建议 |
|---|---|---|
| P3-1 | lark-oapi>=、segno>= 未锁上限 | 改精确版本或加 ~= 区间 |
| P3-2 | loader.py/db_store.py 用 print 代替 logging | 统一 logging，接入日志收集 |
| P3-3 | mask_secret/_is_valid_key 三处重复；_default_chat 与流式版约 80 行重复 | 抽公共模块 |
| P3-4 | main.py 1318 行承载全部 60+ 端点 | 按域拆 APIRouter |
| P3-5 | vector_memory 默认 1536 与实际 1024 不一致 | 默认值改为与 manager 一致或强制显式传参 |
| P3-6 | Agent 简单问候走完整管线 23s | 闲聊快路 + 前端默认走 stream 端点 |
| P3-7 | FTP 明文备份协议仍在支持列表 | 文档标注风险或默认禁用 |
| P3-8 | 无 Alembic 迁移管理 | 引入 Alembic |
| P3-9 | 依赖普遍为 2024 年底版本 | 跑 pip-audit 后择期升级 |

## 测试误报澄清（非系统 BUG）

首轮测试中 4 项 FAIL（待办 toggle 405、记账 404、模型激活 404、SQL 注入连接异常）均为**验收脚本路由猜测错误**，按真实路由复测后全部通过，不计入系统缺陷。
