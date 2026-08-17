"""长期经验记忆（Qdrant 向量库）。

对齐《实现指南》第五节：存储提纯后的长期经验，向量维度 1536（OpenAI text-embedding-ada-002 等）。
注意：embedding 向量由调用方生成后传入（本模块不负责向量化）。
"""
import uuid

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct, PointIdsList
    _QDRANT_AVAILABLE = True
except ImportError:
    QdrantClient = Distance = VectorParams = PointStruct = None
    _QDRANT_AVAILABLE = False

from app.config import settings


class VectorMemory:
    def __init__(self, collection_name: str = "lifeos_experience", vector_size: int = 1536):
        if not _QDRANT_AVAILABLE:
            raise RuntimeError("qdrant_client 未安装，无法使用长期记忆（pip install qdrant-client）")
        self.collection_name = collection_name
        self.vector_size = vector_size
        # 统一解析 QDRANT_URL（支持 http:// https:// 以及裸 host:port 形式）
        from urllib.parse import urlparse
        raw = (settings.qdrant_url or "http://localhost:6333").strip().rstrip("/")
        if not raw.startswith(("http://", "https://")):
            raw = "//" + raw  # 无 scheme 时让 urlparse 把 host:port 解析出来
        parsed = urlparse(raw)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6333
        if parsed.scheme == "https":
            self.client = QdrantClient(url=f"https://{host}:{port}")
        elif parsed.scheme == "http":
            self.client = QdrantClient(url=f"http://{host}:{port}")
        else:
            self.client = QdrantClient(host=host, port=port)
        self._ensure_collection()

    def _ensure_collection(self):
        collections = self.client.get_collections().collections
        if not any(c.name == self.collection_name for c in collections):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
            )

    def save_experience(self, user_id: str, text: str, vector: list, metadata: dict = None) -> str:
        point_id = str(uuid.uuid4())
        payload = {"user_id": user_id, "text": text}
        if metadata:
            payload.update(metadata)
        self.client.upsert(
            collection_name=self.collection_name,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return point_id

    def search_similar(self, vector: list, limit: int = 5, user_id: str = None) -> list:
        scroll_filter = None
        if user_id:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            scroll_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])
        hits = self.client.search(
            collection_name=self.collection_name, query_vector=vector,
            limit=limit, query_filter=scroll_filter,
        )
        return [{"id": h.id, "score": h.score, "payload": h.payload} for h in hits]

    def list_experiences(self, user_id: str = None, limit: int = 50) -> list:
        """按 user_id（可选）滚动列出长期经验；用于记忆管理端点查看。"""
        scroll_filter = None
        if user_id:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            scroll_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])
        points, _ = self.client.scroll(
            collection_name=self.collection_name,
            scroll_filter=scroll_filter, limit=limit,
            with_payload=True, with_vectors=False,
        )
        return [{"id": p.id, "payload": p.payload} for p in points]

    def delete_experience(self, point_id) -> bool:
        """按点 ID 删除一条长期经验；用于记忆管理端点。"""
        self.client.delete(collection_name=self.collection_name, points_selector=PointIdsList(points=[point_id]))
        return True
