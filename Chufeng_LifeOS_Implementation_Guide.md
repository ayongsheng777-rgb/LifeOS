# 楚烽 LifeOS V2.0 核心系统实现与部署指南

本指南提供了 LifeOS V2.0 的核心基础架构代码、关键组件实现以及 Docker 容器化部署方案。您可以基于此框架进行二次开发与功能扩充。

---

## 一、 环境依赖与项目初始化 (requirements.txt)

在项目根目录下创建 `requirements.txt`，涵盖核心所需的库：

```text
fastapi==0.104.1
uvicorn==0.24.0
pydantic==2.5.2
redis==5.0.1
asyncpg==0.29.0
qdrant-client==1.6.9
langchain==0.0.344
openai==1.3.7
pyyaml==6.0.1
httpx==0.25.2
```

---

## 二、 核心入口：API 与飞书网关 (app/main.py)

利用 FastAPI 构建统一网关，接收飞书机器人的 Webhook 推送，并转发给 Agent 处理。

```python
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from typing import Dict, Any
from app.agent.router import AgentRouter

app = FastAPI(title="Chufeng LifeOS", version="2.0")
agent_router = AgentRouter()

class MessagePayload(BaseModel):
    user_id: str
    message: str
    source: str = "feishu"
    time: str
    context: Dict[str, Any] = {}

@app.post("/webhook/feishu")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    
    payload = MessagePayload(
        user_id=data.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "default_user"),
        message=data.get("event", {}).get("message", {}).get("content", ""),
        time=data.get("event", {}).get("message", {}).get("create_time", ""),
        source="feishu"
    )
    
    background_tasks.add_task(agent_router.process_message, payload)
    return {"status": "success", "msg": "Message received"}
```

---

## 三、 核心引擎：Agent 路由与隔离机制 (app/agent/router.py)

负责意图识别、上下文控制（确保支持跨领域的 Clean-slate 状态重置）以及技能分发。

```python
from app.skills.loader import SkillRegistry
from app.memory.short_memory import ShortMemory

class AgentRouter:
    def __init__(self):
        self.skill_registry = SkillRegistry()
        self.skill_registry.load_all_skills()
        self.short_memory = ShortMemory()
        
    async def process_message(self, payload):
        user_id = payload.user_id
        message = payload.message
        
        # 1. Clean-Slate 隔离保护
        if message.strip() in ["/reset", "清空上下文", "新对话"]:
            self.short_memory.clear(user_id)
            return "上下文已彻底清空，已为您准备好全新的干净运行环境。"

        # 2. 获取当前短期记忆
        context = self.short_memory.get(user_id)
        
        # 3. 意图识别与 Skill 匹配
        available_skills = self.skill_registry.get_available_skills()
        selected_skill_name = await self._identify_intent(message, available_skills)
        
        # 4. 执行技能
        if selected_skill_name and self.skill_registry.has_skill(selected_skill_name):
            skill = self.skill_registry.get_skill(selected_skill_name)
            response = await skill.execute(message, context)
        else:
            response = await self._default_chat(message, context)
            
        # 5. 更新短期记忆
        self.short_memory.add(user_id, message, response)
        return response

    async def _identify_intent(self, message: str, skills: list) -> str:
        if "不舒服" in message or "药" in message:
            return "health_skill"
        return None

    async def _default_chat(self, message: str, context: list) -> str:
        return f"已收到您的消息：{message}"
```

---

## 四、 自动发现与热插拔：Skill 加载器 (app/skills/loader.py)

实现根据 `skills/` 目录下的 `skill.yaml` 自动扫描和注册技能。

```python
import os
import yaml
import importlib

class SkillRegistry:
    def __init__(self, skills_dir="skills"):
        self.skills_dir = skills_dir
        self.skills = {}

    def load_all_skills(self):
        base_path = os.path.abspath(self.skills_dir)
        if not os.path.exists(base_path):
            return

        for skill_folder in os.listdir(base_path):
            folder_path = os.path.join(base_path, skill_folder)
            if os.path.isdir(folder_path):
                yaml_path = os.path.join(folder_path, "skill.yaml")
                if os.path.exists(yaml_path):
                    self._register_skill(folder_path, yaml_path)

    def _register_skill(self, folder_path, yaml_path):
        with open(yaml_path, 'r', encoding='utf-8') as f:
            metadata = yaml.safe_load(f)
            
        skill_name = metadata.get("name")
        module_name = f"{self.skills_dir}.{os.path.basename(folder_path)}.handler"
        try:
            module = importlib.import_module(module_name)
            handler_instance = module.SkillHandler(metadata)
            self.skills[skill_name] = handler_instance
        except Exception as e:
            print(f"[Skill Error] Failed to load {skill_name}: {e}")

    def get_available_skills(self):
        return [{"name": name, "desc": handler.metadata.get("description")} 
                for name, handler in self.skills.items()]

    def has_skill(self, name):
        return name in self.skills

    def get_skill(self, name):
        return self.skills.get(name)
```

---

## 五、 记忆与经验沉淀 (app/memory/vector_memory.py)

利用 Qdrant 向量数据库处理长期经验记忆的存储与检索。

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import uuid

class VectorMemory:
    def __init__(self, host="qdrant", port=6333):
        self.client = QdrantClient(host=host, port=port)
        self.collection_name = "lifeos_experience"
        self._ensure_collection()

    def _ensure_collection(self):
        collections = self.client.get_collections().collections
        if not any(c.name == self.collection_name for c in collections):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
            )

    def save_experience(self, user_id: str, text: str, vector: list, metadata: dict):
        # 保存提纯后的长期经验
        point_id = str(uuid.uuid4())
        payload = {"user_id": user_id, "text": text}
        payload.update(metadata)
        
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                PointStruct(id=point_id, vector=vector, payload=payload)
            ]
        )
```

---

## 六、 Docker Compose 生产部署配置 (docker-compose.yml)

数据卷（Volumes）设定为本地目录，确保数据库重装或容器销毁时不丢失“人生资产”。

```yaml
version: '3.8'

services:
  lifeos-api:
    build: .
    container_name: lifeos-api
    ports:
      - "8000:8000"
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DB_URL=postgresql://postgres:password@postgres:5432/lifeos
      - QDRANT_URL=http://qdrant:6333
      - LLM_API_KEY=${LLM_API_KEY}
    depends_on:
      - postgres
      - redis
      - qdrant
    volumes:
      - ./app:/app/app
      - ./skills:/app/skills

  postgres:
    image: postgres:15-alpine
    container_name: lifeos-postgres
    environment:
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=password
      - POSTGRES_DB=lifeos
    ports:
      - "5432:5432"
    volumes:
      - /mnt/data/lifeos_pgdata:/var/lib/postgresql/data
    restart: always

  redis:
    image: redis:7-alpine
    container_name: lifeos-redis
    ports:
      - "6379:6379"
    volumes:
      - /mnt/data/lifeos_redis:/data
    restart: always

  qdrant:
    image: qdrant/qdrant:v1.6.1
    container_name: lifeos-qdrant
    ports:
      - "6333:6333"
    volumes:
      - /mnt/data/lifeos_qdrant:/qdrant/storage
    restart: always
```
