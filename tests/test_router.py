"""Agent 路由测试：对话兜底、清上下文指令、单用户身份收敛。"""
from app.main import ChatReq, agent_router


def test_chat_returns_string(client, auth_headers):
    r = client.post("/api/agent/chat", json={"message": "你好"}, headers=auth_headers)
    assert r.status_code == 200
    assert isinstance(r.json()["reply"], str)


def test_reset_command(client, auth_headers):
    r = client.post("/api/agent/chat", json={"message": "清空上下文"}, headers=auth_headers)
    assert r.status_code == 200
    assert "清空" in r.json()["reply"]


def test_single_user_identity_enforced():
    # P0-04：外部 REST 不再接受客户端自填 user_id
    assert "user_id" not in ChatReq.model_fields


def test_skill_registry_loaded():
    skills = agent_router.skill_registry.get_available_skills()
    names = {s["name"] for s in skills}
    assert {"todo_skill", "expense_skill", "news_skill", "health_skill"}.issubset(names)
