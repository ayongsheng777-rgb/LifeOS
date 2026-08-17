"""Agent 路由与隔离机制（对齐《实现指南》第三节，V2 增强）。

职责：
- Clean-Slate 隔离保护（/reset、清空上下文、新对话）
- 意图识别：关键词快路（零开销、与旧版一致）+ AI 兜底分类
- Skill 分发 / 多步任务编排（ai + skill 混合步骤，有界且安全降级）
- 未命中走 AI 默认对话（AI 不可用时给友好回声兜底）

设计约束（不破坏运行实例）：
- 关键词命中直接走原路径，不调用大模型；
- AI 不可用 / 分类失败 / 规划失败 一律回落到默认对话，绝不卡死；
- 多步编排步数封顶（MAX_PLAN_STEPS），单步异常不影响其它步。
"""
import os
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.skills.loader import SkillRegistry
from app.memory import MemoryManager
from app.ai import client
from app.skills import db_store as db
from app.ai.prompt import CHAT_SYSTEM, CLASSIFY_SYSTEM, PLAN_SYSTEM
from app.config import settings

log = logging.getLogger("lifeos.agent")

# 技能目录（可通过环境变量覆盖；容器内为 /app/skills）
SKILLS_DIR = os.environ.get("SKILLS_DIR", "skills")
# 多步编排最大步数（防失控）
MAX_PLAN_STEPS = 4


@dataclass
class MessagePayload:
    user_id: str
    message: str
    source: str = "feishu"
    time: str = ""
    context: Dict[str, Any] = field(default_factory=dict)


def _format_skills(available_skills: List[dict]) -> str:
    """把可用技能渲染成「- 名称（关键词：…）：描述」列表，供分类/编排 prompt 使用。"""
    lines = []
    for s in available_skills:
        kw = "、".join(s.get("trigger_keywords") or [])
        desc = s.get("desc") or ""
        lines.append(f"- {s['name']}（关键词：{kw}）：{desc}")
    return "\n".join(lines) if lines else "（无可用技能）"


class AgentRouter:
    def __init__(self):
        self.skill_registry = SkillRegistry(SKILLS_DIR)
        self.skill_registry.load_all_skills()
        # 配置驱动的 API 技能（设置页新增，无需写代码）注入路由表
        from app.skills.api_skill import build_api_skills_into
        build_api_skills_into(self.skill_registry)
        self.memory = MemoryManager()  # 统一编排 工作/短期/长期 三层记忆

    async def process_message(self, payload: MessagePayload) -> str:
        user_id = payload.user_id
        message = payload.message

        # 1. Clean-Slate 隔离保护（同时清空 短期 + 工作 两层）
        if message.strip() in ["/reset", "清空上下文", "新对话"]:
            self.memory.clear_short(user_id)
            self.memory.clear_working(user_id)
            return "上下文已彻底清空，已为您准备好全新的干净运行环境。"

        # 1.5 个人事实库写入（"记住 …" 指令，优先级高于意图分类）
        fact_reply = await self._maybe_store_fact(user_id, message)
        if fact_reply is not None:
            self.memory.add_short(user_id, message, fact_reply)
            return fact_reply

        # 2. 获取当前短期记忆（Redis 多轮历史）
        context = self.memory.get_short(user_id)
        # 个人事实库（永久记忆 A）上下文块，供技能/编排分发时一并带上
        fact_block = await self._build_fact_block(user_id)

        # 3. 意图识别（关键词快路 + AI 兜底）
        available_skills = self.skill_registry.get_available_skills()
        decision = await self._classify_intent_v2(message, available_skills)

        # 4. 工作记忆：记录本轮意图/命中技能，供后续与调试查看（不阻断主流程）
        try:
            self.memory.set_working(user_id, "last_intent", decision["type"])
            if decision.get("skill"):
                self.memory.set_working(user_id, "last_skill", decision["skill"])
        except Exception:
            pass

        # 5. 分发执行
        if decision["type"] == "skill" and self.skill_registry.has_skill(decision["skill"]):
            skill_name = decision["skill"]
            self.memory.bump_skill(skill_name)
            skill = self.skill_registry.get_skill(skill_name)
            enriched = self._enrich_context(context, fact_block)
            try:
                response = await skill.execute(message, enriched, user_id=user_id)
            except Exception as e:
                response = f"技能[{skill_name}]执行出错：{e}"
            # 技能返回 None → 交给 AI 默认对话（不卡死）
            if response is None:
                response = await self._default_chat(message, context, user_id=user_id)
        elif decision["type"] == "multi_step":
            response = await self._execute_multi_step(message, context, fact_block, user_id)
        else:
            response = await self._default_chat(message, context, user_id=user_id)

        # 6. 更新短期记忆
        self.memory.add_short(user_id, message, response)
        return response

    # ===================== 个人事实库（永久记忆 A）=====================
    async def _build_fact_block(self, user_id: str) -> str:
        """读取个人事实库，拼成可读上下文块；失败返回空串（不阻断主流程）。"""
        try:
            facts = await db.list_facts(user_id, limit=20)
            if not facts:
                return ""
            return "\n".join(f"- [{f['category']}] {f['key']}：{f['value']}" for f in facts)
        except Exception as e:
            log.warning("个人事实库读取失败（忽略）: %s", e)
            return ""

    @staticmethod
    def _enrich_context(context: list, fact_block: str) -> list:
        """把个人事实块作为 system 条目并入分发给技能/编排的上下文。"""
        if not fact_block:
            return context
        return list(context) + [{"role": "system", "content": "[个人长期记忆]\n" + fact_block}]

    async def _maybe_store_fact(self, user_id: str, message: str):
        """识别『记住 …』指令，写入个人长期事实库。非此类指令返回 None。"""
        m = message.strip()
        for kw in ("记住了", "记住", "记一下", "记着", "记笔记", "备忘"):
            if m.startswith(kw):
                content = m[len(kw):].strip()
                content = re.sub(r"^[:：\s]+", "", content)
                if not content:
                    return "你想让我记住什么？请说『记住：<内容>』，可选加分类如『健康|我今年用XX药好用』。"
                # 可选分类：支持 "分类|内容" 或 "分类：内容"
                category = "通用"
                if "|" in content:
                    cat, _, body = content.partition("|")
                    category, content = (cat.strip() or "通用"), body.strip()
                elif "：" in content:
                    cat, _, body = content.partition("：")
                    if body.strip():
                        category, content = (cat.strip() or "通用"), body.strip()
                try:
                    fact = await db.remember_fact(user_id, content[:24], content,
                                                 category=category, source="chat")
                    return f"✅ 已记住（分类：{fact['category']}）：{fact['value']}"
                except Exception as e:
                    return f"⚠️ 记忆保存失败：{e}"
        return None

    # ===================== V2：意图分类 =====================
    async def _classify_intent_v2(self, message: str, skills: list) -> Dict[str, Any]:
        """关键词快路 + AI 兜底分类。

        返回 {"type": "skill"|"multi_step"|"chat", "skill": str|None}。
        任何异常 / AI 不可用 → 回落 chat（等价于原默认对话）。
        """
        # ① 关键词快路：与旧版行为一致，零网络开销，先拦明显意图
        kw_hit = self._keyword_match(message, skills)
        if kw_hit:
            return {"type": "skill", "skill": kw_hit}

        # ② AI 兜底分类（需 AI 可用）
        if not client.available():
            return {"type": "chat", "skill": None}
        user_text = f"用户消息：{message}\n\n可用技能：\n{_format_skills(skills)}"
        try:
            res = await client.chat_json(CLASSIFY_SYSTEM, user_text, cache_ttl=60, scenario="classify")
        except Exception as e:
            log.warning("意图分类失败（降级 chat）: %s", e)
            return {"type": "chat", "skill": None}
        if not res or not isinstance(res, dict):
            return {"type": "chat", "skill": None}
        t = (res.get("type") or "chat")
        if t == "skill":
            name = res.get("skill")
            if name and self.skill_registry.has_skill(name):
                return {"type": "skill", "skill": name}
            return {"type": "chat", "skill": None}
        if t == "multi_step":
            return {"type": "multi_step", "skill": None}
        return {"type": "chat", "skill": None}

    @staticmethod
    def _keyword_match(message: str, skills: list) -> Optional[str]:
        """原 _identify_intent 逻辑：遍历技能 trigger_keywords，命中即返回名称。"""
        msg = message.lower()
        for sk in skills:
            for kw in (sk.get("trigger_keywords") or []):
                if kw and kw.lower() in msg:
                    return sk["name"]
        return None

    # ===================== V2：多步编排 =====================
    async def _execute_multi_step(self, message: str, context: list, fact_block: str, user_id: str) -> str:
        """把请求拆成有序步骤（ai / skill 混合），逐一执行并汇总结果。

        规划失败 / AI 不可用 / 无步骤 → 回落默认对话；单步异常不影响其它步。
        """
        if not client.available():
            return await self._default_chat(message, context)
        skills = self.skill_registry.get_available_skills()
        user_text = f"用户请求：{message}\n\n可用技能：\n{_format_skills(skills)}"
        try:
            plan = await client.chat_json(PLAN_SYSTEM, user_text, cache_ttl=60, scenario="plan")
        except Exception as e:
            log.warning("多步规划失败（降级默认对话）: %s", e)
            return await self._default_chat(message, context)
        if not plan or not isinstance(plan, dict):
            return await self._default_chat(message, context)
        steps = plan.get("steps") or []
        if not steps:
            return await self._default_chat(message, context)

        results: List[str] = []
        for i, step in enumerate(steps[:MAX_PLAN_STEPS]):
            action = (step.get("action") or "").lower()
            try:
                if action == "skill":
                    name = step.get("skill")
                    if name and self.skill_registry.has_skill(name):
                        skill = self.skill_registry.get_skill(name)
                        arg = step.get("arg") or message
                        out = await skill.execute(arg, self._enrich_context(context, fact_block), user_id=user_id)
                        if out is None:
                            out = f"（{name} 未返回结果）"
                        results.append(f"【步骤{i + 1}·{name}】{out}")
                    else:
                        results.append(f"【步骤{i + 1}】未知技能：{name}")
                elif action == "ai":
                    arg = step.get("arg") or message
                    # 多步内的 AI 子步不单独沉淀长期记忆，最终汇总时统一沉淀一次
                    reply = await self._default_chat(arg, context, save_exp=False, user_id=user_id)
                    results.append(f"【步骤{i + 1}·AI】{reply}")
                else:
                    results.append(f"【步骤{i + 1}】未识别动作：{action}")
            except Exception as e:
                results.append(f"【步骤{i + 1}】执行出错：{e}")

        final = "\n".join(results)
        # 汇总结果统一沉淀一次长期记忆（可选；失败忽略）
        try:
            await self._save_experience(message, final)
        except Exception:
            pass
        return final

    # ===================== 默认对话（保留原逻辑）=====================
    async def _default_chat(self, message: str, context: list, save_exp: bool = True, user_id: str = "me") -> str:
        """未命中 Skill → AI 默认对话（AI 不可用时给友好兜底）。"""
        if not client.available():
            return (f"已收到您的消息：{message}\n"
                    f"（当前未配置 AI 模型或模型不可用，仅作回声；"
                    f"请在设置中配置 AI_API_KEY 后获得智能回复。）")
        ctx_text = ""
        if context:
            ctx_text = "\n".join(
                [f"{'用户' if c['role'] == 'user' else '助手'}: {c['content']}" for c in context[-6:]]
            )
        # 个人事实库（永久记忆 A）注入：让 AI 真正"记住"用户稳定信息
        try:
            facts = await db.list_facts(user_id, limit=20)
            if facts:
                fact_block = "\n".join(f"- [{f['category']}] {f['key']}：{f['value']}" for f in facts)
                ctx_text = (ctx_text + "\n[个人长期记忆]\n" + fact_block) if ctx_text else ("[个人长期记忆]\n" + fact_block)
        except Exception as e:
            log.warning("个人事实库读取失败（忽略）: %s", e)
        # 长期记忆检索（可选：需 Qdrant 可用 + 已配置 EMBEDDING_MODEL；失败不影响主流程）
        lm = self.memory._ensure_long()
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
        reply = await client.chat(CHAT_SYSTEM, user_msg, temperature=0.7, max_tokens=1024,
                                  cache_ttl=300, user_id=user_id, scenario="chat")
        # 沉淀长期记忆（可选；需 embedding 可用）
        if reply and save_exp:
            await self._save_experience(message, reply)
        return reply or "AI 暂时无回复，请稍后再试。"

    # ===================== 流式默认对话（Phase 4）=====================
    async def _default_chat_stream(self, message: str, context: list, user_id: str):
        """流式版本：逐块 yield 文本；结束沉淀短期/长期记忆。AI 不可用 → 整段回声兜底。"""
        if not client.available():
            yield (f"已收到您的消息：{message}\n"
                   f"（当前未配置 AI 模型或模型不可用，仅作回声；"
                   f"请在设置中配置 AI_API_KEY 后获得智能回复。）")
            return
        ctx_text = ""
        if context:
            ctx_text = "\n".join(
                [f"{'用户' if c['role'] == 'user' else '助手'}: {c['content']}" for c in context[-6:]]
            )
        # 个人事实库（永久记忆 A）注入
        try:
            facts = await db.list_facts(user_id, limit=20)
            if facts:
                fact_block = "\n".join(f"- [{f['category']}] {f['key']}：{f['value']}" for f in facts)
                ctx_text = (ctx_text + "\n[个人长期记忆]\n" + fact_block) if ctx_text else ("[个人长期记忆]\n" + fact_block)
        except Exception as e:
            log.warning("个人事实库读取失败（忽略）: %s", e)
        lm = self.memory._ensure_long()
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
        full: list = []
        async for piece in client.chat_stream(CHAT_SYSTEM, user_msg, temperature=0.7,
                                              max_tokens=1024, user_id=user_id, scenario="chat"):
            full.append(piece)
            yield piece
        reply = "".join(full)
        if reply:
            await self._save_experience(message, reply)
            self.memory.add_short(user_id, message, reply)

    async def stream_message(self, payload: MessagePayload):
        """流式入口：意图判定后分流。

        - skill / multi_step → 非流式整段返回（yield 一段）；
        - chat → 走 _default_chat_stream 逐块流式。
        任何分支结束都确保短期记忆落盘。
        """
        user_id = payload.user_id
        message = payload.message

        # 1. Clean-Slate
        if message.strip() in ["/reset", "清空上下文", "新对话"]:
            self.memory.clear_short(user_id)
            self.memory.clear_working(user_id)
            yield "上下文已彻底清空，已为您准备好全新的干净运行环境。"
            return

        # 2. 短期记忆 + 意图判定
        context = self.memory.get_short(user_id)
        fact_block = await self._build_fact_block(user_id)
        available_skills = self.skill_registry.get_available_skills()
        decision = await self._classify_intent_v2(message, available_skills)
        try:
            self.memory.set_working(user_id, "last_intent", decision["type"])
            if decision.get("skill"):
                self.memory.set_working(user_id, "last_skill", decision["skill"])
        except Exception:
            pass

        # 3. 分发
        if decision["type"] == "skill" and self.skill_registry.has_skill(decision["skill"]):
            skill_name = decision["skill"]
            self.memory.bump_skill(skill_name)
            skill = self.skill_registry.get_skill(skill_name)
            enriched = self._enrich_context(context, fact_block)
            try:
                response = await skill.execute(message, enriched, user_id=user_id)
            except Exception as e:
                response = f"技能[{skill_name}]执行出错：{e}"
            if response is None:
                response = await self._default_chat(message, context, user_id=user_id)
            yield response
            self.memory.add_short(user_id, message, response)
        elif decision["type"] == "multi_step":
            response = await self._execute_multi_step(message, context, fact_block, user_id)
            yield response
            self.memory.add_short(user_id, message, response)
        else:
            async for piece in self._default_chat_stream(message, context, user_id):
                yield piece

    def _ensure_long_memory(self):
        """懒加载长期记忆（委托 MemoryManager，失败置哨兵）。"""
        return self.memory._ensure_long()

    async def _save_experience(self, user_msg: str, assistant_msg: str) -> None:
        """沉淀长期记忆（委托 MemoryManager，带重要性护栏，失败忽略）。"""
        await self.memory.save_long_if_important("me", user_msg, assistant_msg)


# 单例
agent_router = AgentRouter()
