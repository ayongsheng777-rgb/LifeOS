"""飞书通讯模块：智能体 Bot（WebSocket 长连接）。

对齐《复刻指导 02》验收清单：
- daemon 线程内覆盖 lark_oapi.ws.client.loop 为子线程事件循环
- 消息回调用 run_coroutine_threadsafe 派发到主循环，不阻塞 WS 线程
- text/post/interactive 三类消息全收，卡片递归提取含按钮 URL
- message_id 去重；sender_type 同时认 "app" 和 "bot"
- 新闻素材内容级去重（链接去查询串 / 正文 md5），命中回「已摄入过」提示卡
- 抓取失败与 AI 失败分开报因；裸链接抓不到正文要明确提示粘贴正文
- _fetch_url_text 抓网页直连不走 AI 代理
- 机器人默认只当素材不当指令；白名单热生效
- token 用模块级 list 缓存，过期前 60s 刷新
- 扫码二维码指向 accounts.feishu.cn 官方页
- app_secret 脱敏，不落日志
- is_online() 读 client._conn 真实连接态
"""
import os
import re
import json
import time
import asyncio
import logging
import hashlib
import threading

import httpx

import lark_oapi as lark
from app.config import settings
from app.agent.router import agent_router, MessagePayload
from app.ai import news_ai

log = logging.getLogger("lifeos.feishu")

FEISHU_API = "https://open.feishu.cn/open-apis"

# 模块级 token 缓存（不要用函数属性 -> 模块作用域不可见）
_TOKEN_CACHE: list = [None, 0]

# 内存态（重启即丢，设计内取舍）
_RECENT_MSG_IDS = set()
_NEWS_ELEMENTS = []
_NEWS_DEDUP = {}
_NEWS_CAP = 200


class FeishuBotService:
    def __init__(self):
        self._running = False
        self._client = None
        self._loop = None
        self._thread = None

    # ===== 生命周期 =====
    def start(self, loop=None):
        if self._running:
            return
        self._running = True
        self._loop = loop
        self._thread = threading.Thread(target=self._run, daemon=True, name="feishu-bot")
        self._thread.start()
        log.info("飞书 Bot 守护线程已启动")

    def stop(self):
        self._running = False
        try:
            if self._client and hasattr(self._client, "stop"):
                self._client.stop()
        except Exception:
            pass
        log.info("飞书 Bot 已请求停止")

    def is_online(self) -> bool:
        # 读 lark 底层真实连接态（不要用自己的布尔标志）
        return bool(self._running and self._client is not None
                    and getattr(self._client, "_conn", None) is not None)

    def _run(self):
        import lark_oapi.ws.client as _ws_mod
        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        # ★ 关键：lark-oapi 在 import 时捕获事件循环，必须在子线程覆盖
        _ws_mod.loop = thread_loop

        while self._running:
            app_id = settings.feishu_app_id
            app_secret = settings.feishu_app_secret
            if not settings.feishu_enabled or not app_id or not app_secret:
                time.sleep(30)
                continue
            try:
                handler = (lark.EventDispatcherHandler.builder("", "")
                           .register_p2_im_message_receive_v1(self._on_message).build())
                self._client = lark.ws.Client(app_id, app_secret,
                                              event_handler=handler,
                                              log_level=lark.LogLevel.WARNING)
                log.info("飞书 WS 连接建立中…")
                self._client.start()  # 阻塞直到断连
            except Exception as e:
                log.warning("飞书 WS 异常: %s", e)
            if not self._running:
                break
            time.sleep(10)  # 断连后重连

    # ===== 接收（同步回调，WS 线程内）=====
    def _on_message(self, data):
        try:
            event = getattr(data, "event", None)
            if not event:
                return
            message = getattr(event, "message", None)
            sender = getattr(event, "sender", None)
            if not message or not sender:
                return
            msg_id = getattr(message, "message_id", "")
            # 1. 事件级去重
            if msg_id in _RECENT_MSG_IDS:
                return
            _RECENT_MSG_IDS.add(msg_id)
            if len(_RECENT_MSG_IDS) > 500:
                _RECENT_MSG_IDS.clear()

            sender_id_obj = getattr(sender, "sender_id", None)
            open_id = getattr(sender_id_obj, "open_id", "") if sender_id_obj else ""
            sender_type = getattr(sender, "sender_type", "") or ""
            chat_id = getattr(message, "chat_id", open_id) or open_id
            msg_type = getattr(message, "message_type", "text") or "text"
            content = getattr(message, "content", "{}") or "{}"
            text = self._extract_text(msg_type, content)
            if not text:
                log.info("收到非文本/卡片消息(%s)，已忽略", msg_type)
                return

            # 4. 机器人门禁
            is_bot = sender_type in ("app", "bot") and open_id not in settings.feishu_trusted_bots

            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._process_command(open_id, text, chat_id, is_bot), self._loop)
        except Exception as e:
            log.warning("消息解析异常: %s", e)

    # ===== 文本/卡片提取 =====
    def _extract_text(self, msg_type: str, content_str: str) -> str:
        try:
            obj = json.loads(content_str)
        except Exception:
            return content_str or ""
        if msg_type == "text":
            return (obj.get("text") or "").strip()
        if msg_type == "post":
            parts = []
            if obj.get("title"):
                parts.append(obj["title"])
            for line in obj.get("content", []):
                for el in line:
                    tag = el.get("tag")
                    if tag == "text":
                        parts.append(el.get("text", ""))
                    elif tag == "a":
                        parts.append(el.get("href", ""))
            return "\n".join(p for p in parts if p).strip()
        if msg_type == "interactive":
            return self._extract_card_text(obj).strip()
        return str(obj)

    def _extract_card_text(self, obj: dict) -> str:
        """递归提取卡片文本与 URL（兼容卡片 1.0/2.0）。"""
        texts, urls = [], []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("content", "text") and isinstance(v, str):
                        texts.append(v)
                    if k in ("href", "url", "action_url") and isinstance(v, str):
                        urls.append(v)
                    walk(v)
            elif isinstance(node, list):
                for it in node:
                    walk(it)

        walk(obj)
        return "\n".join(texts + urls)

    # ===== 指令分发（async，主事件循环）=====
    async def _process_command(self, open_id: str, text: str, chat_id: str, is_bot: bool):
        raw = text
        # 去 @提及前缀
        text = re.sub(r"^@\S+\s*", "", text).strip()
        text_lower = text.lower()

        # 未加白名单的机器人：只当素材，不执行指令
        if is_bot:
            if re.search(r"https?://\S+", raw):
                await self._cmd_ingest_news(open_id, raw, chat_id)
            return

        # 帮助
        if text in ("帮助", "help", "?"):
            await self._send_card(chat_id, "LifeOS 使用帮助",
                                  ["· 直接发消息：我会用 AI 回复",
                                   "· 附带新闻链接：我会解读并收录为素材",
                                   "· 发送「清空上下文」：重置本次对话记忆",
                                   "· 管理员可配置飞书 / AI 模型"])
            return

        # 新闻素材查询
        if text in ("新闻", "素材", "news"):
            await self._send_news_list(chat_id)
            return

        # 含 URL → 新闻摄入
        if re.search(r"https?://\S+", raw):
            await self._cmd_ingest_news(open_id, raw, chat_id)
            return

        # 兜底：交给 Agent（最终路由到 AI 默认对话 / Skill）
        try:
            reply = await agent_router.process_message(
                MessagePayload(user_id=open_id, message=text, source="feishu", time=str(int(time.time()))))
        except Exception as e:
            reply = f"处理出错：{e}"
        if reply:
            await self._send_text(chat_id, reply)

    # ===== 新闻素材摄入 =====
    def _news_dedup_key(self, urls, text):
        if urls:
            return "u:" + urls[0].split("?")[0].split("#")[0].rstrip("/")
        norm = re.sub(r"\s+", "", text or "")[:500]
        return "t:" + hashlib.md5(norm.encode()).hexdigest()

    async def _cmd_ingest_news(self, open_id: str, raw: str, chat_id: str):
        urls = re.findall(r"https?://\S+", raw)
        dedup_key = self._news_dedup_key(urls, raw)
        if dedup_key in _NEWS_DEDUP:
            first = _NEWS_DEDUP[dedup_key]
            await self._send_card(chat_id, "重复素材提醒",
                                  [f"这条素材我已摄入过（{first['time']}）。",
                                   f"当时研判：{first.get('summary','-')}"])
            return

        # 先发前置回执
        await self._send_text(chat_id, "已收到，正在分析…")

        attachment = raw
        body = ""
        if urls:
            body = await self._fetch_url_text(urls[0])
            if not body:
                # 抓取失败与 AI 失败分开报因
                await self._send_card(chat_id, "链接抓取失败",
                                      ["未能抓取到该链接的正文（超时或反爬）。",
                                       "请直接把新闻正文粘贴给我，我来解读。"])
                return

        # AI 解读
        result = await news_ai.interpret_text(body or attachment, attachment="")
        if result is None:
            # AI 不可用
            element = {"time": time.strftime("%Y-%m-%d %H:%M"), "url": urls[0] if urls else "",
                       "summary": "（AI 未配置，仅存原文）", "raw": (body or attachment)[:500]}
            self._push_news(element)
            await self._send_card(chat_id, "已收录（AI 未配置）",
                                  ["已保存素材原文，但 AI 解读未配置。",
                                   "请在设置中配置 AI_API_KEY 后重新发送以获得研判。"])
            return

        score = news_ai.news_score(result)
        summary_lines = [
            f"情感：{result.get('sentiment')}　影响：{result.get('impact')}/5",
            f"层级：{result.get('level')}　可信度：{result.get('credibility')}",
            f"综合评分：{score}/100",
            f"理由：{result.get('reason','-')}",
        ]
        element = {"time": time.strftime("%Y-%m-%d %H:%M"), "url": urls[0] if urls else "",
                   "summary": result.get("reason", "-"), "result": result, "score": score}
        self._push_news(element)
        await self._send_card(chat_id, "新闻研判", summary_lines)

    def _push_news(self, element: dict):
        _NEWS_ELEMENTS.append(element)
        key = self._news_dedup_key([element.get("url")] if element.get("url") else [], element.get("summary", ""))
        _NEWS_DEDUP[key] = {"time": element["time"], "summary": element.get("summary", "-")}
        if len(_NEWS_ELEMENTS) > _NEWS_CAP:
            _NEWS_ELEMENTS.pop(0)

    async def _send_news_list(self, chat_id: str):
        if not _NEWS_ELEMENTS:
            await self._send_text(chat_id, "暂无已摄入的新闻素材。")
            return
        lines = [f"{e['time']}　{e.get('summary','-')}" for e in _NEWS_ELEMENTS[-10:]]
        await self._send_card(chat_id, f"已摄入素材（最近 {len(lines)} 条）", lines)

    # ===== 网页抓取（直连，不走 AI 代理）=====
    async def _fetch_url_text(self, url: str, max_chars: int = 1500) -> str:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (compatible; LifeOS/2.0)"}) as c:
                r = await c.get(url)
            if r.status_code != 200:
                return ""
            html = r.text
            # 1) 先抽取 <title> 与 meta description 作为兜底素材
            title = ""
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
            desc = ""
            m = re.search(r'(?is)<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])'
                          r'[^>]+content=["\'](.*?)["\']', html)
            if m:
                desc = m.group(1).strip()
            # 2) 剥离非正文块（脚本/样式/导航/页眉页脚等）
            html = re.sub(r"(?is)<(script|style|noscript|svg|head|header|footer|nav|aside|form)\b.*?</\1>", " ", html)
            # 3) 去标签 + 还原常见实体
            text = re.sub(r"(?s)<[^>]+>", " ", html)
            text = re.sub(r"&nbsp;", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            # 4) 兜底：正文过短则用 meta description，再不行用标题
            if len(text) < 60 and desc:
                text = desc
            if not text and title:
                text = title
            return text[:max_chars]
        except Exception as e:
            log.warning("抓取失败 %s: %s", url, e)
            return ""

    # ===== 发送（REST，tenant_access_token 带缓存）=====
    async def _get_token(self) -> str | None:
        now = time.time()
        if _TOKEN_CACHE[0] and _TOKEN_CACHE[1] > now + 60:
            return _TOKEN_CACHE[0]
        aid, sec = settings.feishu_app_id, settings.feishu_app_secret
        if not aid or not sec:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
                                 json={"app_id": aid, "app_secret": sec})
                d = r.json()
                if d.get("code") != 0:
                    log.warning("获取 tenant_access_token 失败: %s", d)
                    return None
                token = d.get("tenant_access_token")
                expire = d.get("expire", 7200)
                _TOKEN_CACHE[0] = token
                _TOKEN_CACHE[1] = now + expire
                return token
        except Exception as e:
            log.warning("获取 token 异常: %s", e)
            return None

    async def _do_send(self, chat_id: str, content: dict, msg_type: str = "text", id_type: str = "chat_id") -> bool:
        token = await self._get_token()
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{FEISHU_API}/im/v1/messages?receive_id_type={id_type}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": chat_id, "msg_type": msg_type,
                          "content": json.dumps(content, ensure_ascii=False)})
                return r.json().get("code") == 0
        except Exception as e:
            log.warning("发送消息异常: %s", e)
            return False

    async def _send_text(self, chat_id: str, text: str, id_type: str = "chat_id") -> bool:
        return await self._do_send(chat_id, {"text": text}, "text", id_type)

    async def _send_card(self, chat_id: str, title: str, lines: list, id_type: str = "chat_id") -> bool:
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": ln}} for ln in lines],
            },
        }
        return await self._do_send(chat_id, card["card"], "interactive", id_type)

    # ===== 出站推送（供 Connector 调用）=====
    async def push_to_users(self, users: list, text: str) -> int:
        """向一组飞书 open_id 推送文本；返回成功发送数。未配置/未上线安全降级返回 0。"""
        if not text:
            return 0
        users = [u for u in (users or []) if u]
        if not users:
            return 0
        if not settings.feishu_enabled or not settings.feishu_app_id:
            return 0
        ok = 0
        for uid in users:
            try:
                if await self._send_text(uid, text, id_type="user_id"):
                    ok += 1
            except Exception as e:
                log.warning("飞书推送失败 %s: %s", uid, e)
        return ok


# ===== 模块级单例与启停（供 main.py lifespan 调用）=====
bot = FeishuBotService()


def start_bot(loop):
    bot.start(loop)


async def stop_bot():
    bot.stop()


def bot_status() -> dict:
    return {
        "bot_online": bot.is_online(),
        "cred_configured": bool(settings.feishu_app_id and settings.feishu_app_secret),
        "feishu_enabled": settings.feishu_enabled,
        "news_elements": len(_NEWS_ELEMENTS),
        "trusted_bots": settings.feishu_trusted_bots,
        "admin_users": settings.feishu_admin_users,
    }


def get_news() -> list:
    return _NEWS_ELEMENTS
