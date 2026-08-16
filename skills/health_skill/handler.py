"""健康技能 handler：仅信息整理与提醒，不做医学诊断。"""
from app.ai import analyzer


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        result = await analyzer.analyze_health(message)
        if not result:
            return ("收到您的健康描述。当前未配置 AI 模型，无法做进一步整理；"
                    "建议您记录症状与用药时间，必要时及时就医。")
        urgency = result.get("urgency", "低")
        action = result.get("action", "")
        note = result.get("note", "")
        meds = result.get("medication", "")
        lines = [f"【健康记录】紧急度：{urgency}"]
        if meds:
            lines.append(f"用药/症状：{meds}")
        if action:
            lines.append(f"建议行动：{action}")
        if note:
            lines.append(f"注意：{note}")
        lines.append("（提示：本整理不替代医生诊断）")
        return "\n".join(lines)
