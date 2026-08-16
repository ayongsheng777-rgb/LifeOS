"""Agent 路由与隔离机制（对齐《实现指南》第三节）。

职责：Clean-Slate 隔离保护 / 意图识别 / Skill 分发 / 未命中走 AI 默认对话。
"""
import os
import logging
from dataclasses import dataclass, field
from typing import Any, Dict

from app.skills.loader import SkillRegistry
from app.memory.short_memory import ShortMemory
from app.memory.vector_memory import VectorMemory
from app.ai import client
from app.ai.prompt import CHAT_SYSTEM
from app.config import settings

log = logging.getLogger("lifeos.agent")

# 技能目录（可通过环境变量覆盖；容器内为 /app/skills）
SKILLS_DIR = os.environ.get("SKILLS_DIR", "skills")


@dataclass
class MessagePayload:
    user_id: str
    message: str
    source: str = "feishu"
    time: str = ""
    context: Dict[str, Any] = field(default_factory=dict)


class AgentRouter:
    def __init__(self):
        self.skill_registry = SkillRegistry(SKILLS_DIR)
        self.skill_registry.load_all_skills()
        self.short_memory = ShortMemory()
        self.long_memory = None  # 长期记忆懒加载；不可用时置 False（哨兵），不再重试

    async def process_message(self, payload: MessagePayload) -> str:
        user_id = payload.user_id
        message = payload.message

        # 1. Clean-Slate 隔离保护
        if message.strip() in ["/reset", "清空上下文", "新对话"]:
            self.short_memory.clear(user_id)
            return "上下文已彻底清空，已为您准备好全新的干净运行环境。"

        # 2. 获取当前短期记忆
        context = self.short_memory.get(user_id)

        # 3. 意图识别与 Skill 匹配（按 skill.yaml 的 trigger_keywords 自动分发）
        available_skills = self.skill_registry.get_available_skills()
        selected_skill_name = await self._identify_intent(message, available_skills)

        # 4. 执行技能
        if selected_skill_name and self.skill_registry.has_skill(selected_skill_name):
            skill = self.skill_registry.get_skill(selected_skill_name)
            try:
                response = await skill.execute(message, context, user_id=user_id)
            except Exception as e:
                response = f"技能[{selected_skill_name}]执行出错：{e}"
        else:
            response = await self._default_chat(message, context)

        # 5. 更新短期记忆
        self.short_memory.add(user_id, message, response)
        return response

    async def _identify_intent(self, message: str, skills: list) -> str:
        """自动意图识别：遍历已注册技能的 trigger_keywords，命中即分发该技能。

        每个技能在自己的 execute() 里做二次判断——若它发现这条消息其实处理不了，
        返回 None，router 会落到默认 AI 对话（不会卡死）。
        """
        msg = message.lower()
        for sk in skills:
            keywords = sk.get("trigger_keywords") or []
            for kw in keywords:
                if kw and kw.lower() in msg:
                    return sk["name"]
        return None

    async def _default_chat(self, message: str, context: list) -> str:
        """未命中 Skill → AI 默认对话（AI 不可用时给友好兜底）。"""
        if not client.available():
            return (f"已收到您的消息：{message}\n"
                    f"（当前未配置 AI 模型或模型不可用，仅作回声；"
                    f"请在设置中配置 AI_API_KEY 后获得智能回复。）")
        ctx_text = ""
        if context:
            ctx_text = "\n".join(
                [f"{'用户' if c['role']=='user' else '助手'}: {c['content']}" for c in context[-6:]]
            )
        # 长期记忆检索（可选：需 Qdrant 可用 + 已配置 EMBEDDING_MODEL；失败不影响主流程）
        lm = self._ensure_long_memory()
        if lm is not None:
            try:
                vec = await client.embed(message)
                if vec:
                    hits = lm.search_similar(vec, limit=3, user_id=None)
                    if hits:
                        refs = "\n".join(f"- {h['payload'].get('text', '')}" for h in hits)
                        ctx_text = (ctx_text + "\n[长期经验参考]\n" + refs) if ctx_text else ("[长期经验参考]\n" + refs)
            except Exception as e:
                log.warning("长期记忆检索失败（忽略）: %s", e)
        user_msg = f"{ctx_text}\n用户：{message}" if ctx_text else message
        reply = await client.chat(CHAT_SYSTEM, user_msg, temperature=0.7, max_tokens=1024, cache_ttl=300)
        # 沉淀长期记忆（可选；需 embedding 可用）
        if reply:
            await self._save_experience(message, reply)
        return reply or "AI 暂时无回复，请稍后再试。"

    def _ensure_long_memory(self):
        """懒加载长期记忆；失败置 False 哨兵，避免每次对话都重试连接。"""
        if self.long_memory is False:
            return None
        if self.long_memory is None:
            try:
                self.long_memory = VectorMemory()
            except Exception as e:
                log.warning("长期记忆（Qdrant）不可用，已降级关闭: %s", e)
                self.long_memory = False
        return self.long_memory if self.long_memory else None

    async def _save_experience(self, user_msg: str, assistant_msg: str) -> None:
        """把本轮对话（用户+助手）向量化后存入长期记忆；任一环节失败均忽略。"""
        lm = self._ensure_long_memory()
        if lm is None:
            return
        text = f"用户：{user_msg}\n助手：{assistant_msg}"
        try:
            vec = await client.embed(text)
            if vec:
                lm.save_experience("me", text, vec)
        except Exception as e:
            log.warning("长期经验保存失败（忽略）: %s", e)


# 单例
agent_router = AgentRouter()
