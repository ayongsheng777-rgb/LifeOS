"""Agent V2 测试：关键词快路 / AI 兜底分类 / 多步编排 / 无 AI 降级。

不依赖外部 Redis/Qdrant/AI；AI 相关调用用 monkeypatch 模拟，避免网络与凭证。
"""
import asyncio

import pytest

from app.agent.router import AgentRouter, MessagePayload
from app.ai import client as ai_client


def _router():
    return AgentRouter()


def test_keyword_fastpath_skips_ai(monkeypatch):
    """关键词命中应直接返回 skill，且不触发任何 AI 分类调用。"""
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("关键词命中不应调用 AI 分类")
    monkeypatch.setattr(ai_client, "chat_json", _boom)

    skills = [{"name": "todo_skill", "desc": "待办", "trigger_keywords": ["买菜"]}]
    decision = asyncio.run(router._classify_intent_v2("帮我买菜", skills))
    assert decision == {"type": "skill", "skill": "todo_skill"}


def test_ai_classify_chat(monkeypatch):
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)
    monkeypatch.setattr(ai_client, "chat_json",
                        _async(lambda: {"type": "chat", "reason": "闲聊"}))
    skills = [{"name": "todo_skill", "desc": "待办", "trigger_keywords": ["待办"]}]
    decision = asyncio.run(router._classify_intent_v2("今天心情不错", skills))
    assert decision["type"] == "chat"


def test_ai_classify_skill(monkeypatch):
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)
    monkeypatch.setattr(ai_client, "chat_json",
                        _async(lambda: {"type": "skill", "skill": "todo_skill"}))
    skills = [{"name": "todo_skill", "desc": "待办", "trigger_keywords": ["待办"]}]
    decision = asyncio.run(router._classify_intent_v2("帮我记个待办", skills))
    assert decision == {"type": "skill", "skill": "todo_skill"}


def test_ai_classify_invalid_skill_falls_back(monkeypatch):
    """AI 返回一个不存在的技能名 → 安全回落 chat，不报错。"""
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)
    monkeypatch.setattr(ai_client, "chat_json",
                        _async(lambda: {"type": "skill", "skill": "ghost"}))
    skills = [{"name": "todo_skill", "desc": "待办", "trigger_keywords": ["待办"]}]
    decision = asyncio.run(router._classify_intent_v2("随便说点", skills))
    assert decision["type"] == "chat"


def test_ai_classify_failure_falls_back(monkeypatch):
    """AI 分类抛异常 → 回落 chat，不卡死。"""
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)
    monkeypatch.setattr(ai_client, "chat_json", _async_raise(RuntimeError("模型挂了")))
    skills = [{"name": "todo_skill", "desc": "待办", "trigger_keywords": ["待办"]}]
    decision = asyncio.run(router._classify_intent_v2("随便说点", skills))
    assert decision["type"] == "chat"


def test_multi_step_orchestration(monkeypatch):
    """multi_step：分类→规划→执行（skill + ai 混合步骤）并汇总。"""
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)

    state = {"n": 0}
    async def fake_chat_json(system, user, **k):
        state["n"] += 1
        if state["n"] == 1:
            return {"type": "multi_step"}
        return {"steps": [
            {"action": "skill", "skill": "fake_skill", "arg": "记一下买菜"},
            {"action": "ai", "arg": "总结一下"},
        ]}
    monkeypatch.setattr(ai_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(ai_client, "chat", _async(lambda: "AI回复内容"))
    monkeypatch.setattr(router, "_save_experience", _async(lambda: None))

    class FakeSkill:
        def __init__(self):
            self.metadata = {}
        async def execute(self, message, context, user_id=None):
            return f"已处理：{message}"
    router.skill_registry.has_skill = lambda n: n == "fake_skill"
    router.skill_registry.get_skill = lambda n: FakeSkill()
    # 用受控技能列表，避免真实技能关键词（如「待办」）抢走意图，确保走 AI 分类→多步
    router.skill_registry.get_available_skills = lambda: [
        {"name": "fake_skill", "desc": "d", "trigger_keywords": ["zzz_nomatch"]}
    ]

    reply = asyncio.run(router.process_message(
        MessagePayload(user_id="me", message="帮我记待办并总结", source="debug", time="", context={})
    ))
    assert "已处理：记一下买菜" in reply
    assert "AI回复内容" in reply


def test_multi_step_plan_failure_falls_back(monkeypatch):
    """规划阶段 AI 失败 → 回落默认对话（返回字符串）。"""
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: True)
    state = {"n": 0}
    async def fake_chat_json(system, user, **k):
        state["n"] += 1
        if state["n"] == 1:
            return {"type": "multi_step"}
        raise RuntimeError("规划挂了")
    monkeypatch.setattr(ai_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(router, "_save_experience", _async(lambda: None))

    reply = asyncio.run(router.process_message(
        MessagePayload(user_id="me", message="帮我做点复杂的", source="debug", time="", context={})
    ))
    assert isinstance(reply, str) and reply


def test_no_ai_degrades_to_echo(monkeypatch):
    """无 AI 且关键词不命中 → 默认对话（回声兜底，返回非空字符串）。"""
    router = _router()
    monkeypatch.setattr(ai_client, "available", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("无 AI 不应调用分类")
    monkeypatch.setattr(ai_client, "chat_json", _boom)

    reply = asyncio.run(router.process_message(
        MessagePayload(user_id="me", message="今天天气怎么样", source="debug", time="", context={})
    ))
    assert isinstance(reply, str) and reply


def _async(fn):
    async def _w(*a, **k):
        return fn()
    return _w


def _async_raise(exc):
    async def _w(*a, **k):
        raise exc
    return _w
