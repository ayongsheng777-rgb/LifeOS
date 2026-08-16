"""新闻摘要技能 handler：汇总飞书已摄入的新闻素材，可选 AI 生成今日摘要。

新闻素材来自 app.feishu.get_news()（飞书 Bot 摄入，内存态，重启即丢是设计内取舍）。
此处只做「读取 + 规整展示」；摄入与 AI 研判在 feishu.py 内完成。

注意：get_news / client 均懒导入（execute 内），避免与 app.feishu / app.agent.router
在 AgentRouter 初始化时形成环形导入。
"""


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        from app.feishu import get_news
        from app.ai import client

        msg = message.lower()
        if not any(k in msg for k in ("新闻", "资讯", "摘要", "素材", "news", "看了什么", "今天有什么")):
            return None

        items = get_news()
        if not items:
            return ("暂时没有已摄入的新闻素材。你可以在飞书里把新闻链接发给我"
                    "（或粘贴正文），我会自动研判并收录。")

        lines = []
        for it in items[-10:]:
            score = it.get("score")
            summary = it.get("summary", "-")
            lines.append(f"· {it.get('time', '')}　评分{score if score is not None else '?'}　{summary}")
        digest = "\n".join(lines)

        if client.available():
            ai = await client.chat(
                "你是资讯摘要助手。基于下面的新闻线索，用 3 句话以内概括今日重点，"
                "不要编造、不要套话、不要重复逐条罗列。",
                f"今日线索：\n{digest}",
                max_tokens=400, temperature=0.4, cache_ttl=600)
            if ai:
                return "【今日资讯摘要】\n" + ai + "\n\n——明细——\n" + digest
        return "【已摄入新闻素材】\n" + digest
