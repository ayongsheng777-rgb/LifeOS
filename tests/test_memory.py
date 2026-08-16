"""记忆层测试：重要性护栏 + 记忆管理端点优雅降级。

不依赖外部 Redis / Qdrant（对应模块失败自动降级），
仅验证：(1) 长期记忆护栏规则；(2) 端点在无外部依赖时返回合规结构。
多轮短期记忆的 Redis 持久化由重启实例后的手动验证覆盖。
"""
import pytest

from app.memory.manager import MemoryManager
from app.main import app


# ===================== 重要性护栏 =====================
def test_guard_rejects_echo_fallback():
    # AI 未配置时的回声兜底，不应沉淀
    msg = "帮我查下明天天气"
    reply = "已收到您的消息：帮我查下明天天气\n（当前未配置 AI 模型或模型不可用，仅作回声；...）"
    assert MemoryManager._is_important(msg, reply) is False


def test_guard_rejects_skill_error():
    assert MemoryManager._is_important("记个待办", "技能[todo_skill]执行出错：xxx") is False


def test_guard_rejects_greeting():
    assert MemoryManager._is_important("你好", "你好！有什么可以帮你？") is False
    assert MemoryManager._is_important("/reset", "上下文已清空") is False


def test_guard_rejects_too_short():
    assert MemoryManager._is_important("a", "b") is False


def test_guard_accepts_substantive():
    msg = "帮我总结一下 LifeOS 的三层记忆架构"
    reply = ("LifeOS 采用工作/短期/长期三层记忆：工作记忆存进程内即时状态，"
             "短期记忆用 Redis 保存多轮对话，长期记忆用 Qdrant 沉淀重要经验。")
    assert MemoryManager._is_important(msg, reply) is True


# ===================== 端点优雅降级 =====================
def test_get_memory_short_structure(client, auth_headers):
    r = client.get("/api/memory/short", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "working" in data and isinstance(data["working"], dict)
    assert "short" in data and isinstance(data["short"], list)


def test_delete_memory_short_ok(client, auth_headers):
    r = client.delete("/api/memory/short", headers=auth_headers)
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_get_memory_long_degrades(client, auth_headers):
    # 测试环境无 Qdrant → 优雅返回 configured:false
    r = client.get("/api/memory/long", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is False
    assert data["items"] == []


def test_delete_memory_long_requires_id(client, auth_headers):
    r = client.delete("/api/memory/long", headers=auth_headers)
    assert r.status_code == 400
    assert "id" in r.json().get("error", "")


def test_delete_memory_long_unavailable(client, auth_headers):
    # 无 Qdrant 时删除应优雅返回 503（不崩溃）
    r = client.delete("/api/memory/long?id=fake-point", headers=auth_headers)
    assert r.status_code == 503
