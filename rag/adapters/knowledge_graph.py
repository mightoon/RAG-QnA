"""
知识图谱适配器（rag/adapters/knowledge_graph.py）

neo4j：主实现（同步驱动 + asyncio.to_thread）
nebula：骨架（现场按需实现，协议差异由适配器屏蔽）
"""
from __future__ import annotations

import asyncio

from rag.config.models import KnowledgeGraphConfig

from .base import KnowledgeGraphAdapter
from .registry import AdapterRegistry


@AdapterRegistry.register("knowledge_graph", "neo4j")
class Neo4jKG(KnowledgeGraphAdapter):

    def __init__(self, config: KnowledgeGraphConfig):
        from neo4j import GraphDatabase
        self.config = config
        # 连接超时收紧：driver 默认 30s，服务不可达时阻塞启动
        self._driver = GraphDatabase.driver(
            config.uri, auth=(config.user, config.password),
            connection_timeout=2, connection_acquisition_timeout=3)

    def _run(self, cypher: str, **params):
        with self._driver.session() as session:
            return list(session.run(cypher, **params))

    async def ensure_schema(self) -> None:
        def _create():
            self._run("CREATE CONSTRAINT IF NOT EXISTS "
                      "FOR (e:Entity) REQUIRE e.uid IS UNIQUE")
        await asyncio.to_thread(_create)

    async def upsert_entities(self, tenant_id: str, doc_id: str,
                              entities: list[dict]) -> None:
        def _upsert():
            for e in entities:
                self._run(
                    "MERGE (ent:Entity {uid: $uid}) "
                    "SET ent.name = $name, ent.type = $etype, "
                    "ent.tenant_id = $tenant",
                    uid=f"{tenant_id}:{e['name']}", name=e["name"],
                    etype=e.get("entity_type", "generic"), tenant=tenant_id)
                if doc_id:
                    self._run(
                        "MATCH (ent:Entity {uid: $uid}) "
                        "MERGE (d:Document {doc_id: $doc}) "
                        "MERGE (ent)-[:MENTIONED_IN]->(d)",
                        uid=f"{tenant_id}:{e['name']}", doc=doc_id)
        await asyncio.to_thread(_upsert)

    async def upsert_relations(self, tenant_id: str, doc_id: str,
                               relations: list[dict]) -> None:
        def _upsert():
            for r in relations:
                rel_type = (r.get("relation") or "RELATED_TO").upper()
                rel_type = "".join(c for c in rel_type if c.isalnum() or c == "_")
                self._run(
                    f"MATCH (a:Entity {{uid: $ua}}), (b:Entity {{uid: $ub}}) "
                    f"MERGE (a)-[rel:{rel_type}]->(b) "
                    f"SET rel.doc_id = $doc, rel.tenant_id = $tenant",
                    ua=f"{tenant_id}:{r['source']}", ub=f"{tenant_id}:{r['target']}",
                    doc=doc_id, tenant=tenant_id)
        await asyncio.to_thread(_upsert)

    async def traverse(self, tenant_id: str, entity: str, hops: int = 2,
                       limit: int = 50) -> str:
        """多跳遍历 → 自然语言序列化"""
        hops = min(hops, self.config.max_hops)

        def _traverse():
            records = self._run(
                "MATCH path = (a:Entity {uid: $uid})-[*1.." + str(hops) + "]-(b:Entity) "
                "WHERE a.tenant_id = $tenant "
                "RETURN [n IN nodes(path) | coalesce(n.name, '')] AS names, "
                "[r IN relationships(path) | type(r)] AS rels "
                f"LIMIT {limit}",
                uid=f"{tenant_id}:{entity}", tenant=tenant_id)
            lines = []
            for rec in records:
                names, rels = rec["names"], rec["rels"]
                if len(names) >= 2:
                    parts = [names[0]]
                    for i, rel in enumerate(rels):
                        parts.append(f"—{rel}→")
                        parts.append(names[i + 1])
                    lines.append("".join(parts))
            return lines
        try:
            lines = await asyncio.to_thread(_traverse)
            return "\n".join(lines) if lines else ""
        except Exception:
            return ""

    async def delete_by_doc(self, tenant_id: str, doc_id: str) -> None:
        def _delete():
            self._run(
                "MATCH (e:Entity)-[r:MENTIONED_IN]->(d:Document {doc_id: $doc}) "
                "DELETE r", doc=doc_id)
            self._run(
                "MATCH ()-[r]->() WHERE r.doc_id = $doc DELETE r", doc=doc_id)
        await asyncio.to_thread(_delete)

    async def health_check(self) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._driver.verify_connectivity), timeout=4)
            return True
        except Exception:
            return False


@AdapterRegistry.register("knowledge_graph", "nebula")
class NebulaKG(KnowledgeGraphAdapter):
    """NebulaGraph 骨架实现：nGQL 语法与 Cypher 不同，现场按需补全"""

    def __init__(self, config: KnowledgeGraphConfig):
        self.config = config
        raise NotImplementedError(
            "NebulaGraph 适配器为骨架实现，请按现场 Nebula 版本补全 "
            "（连接协议 Thrift，查询语言 nGQL）")

    async def ensure_schema(self) -> None: ...
    async def upsert_entities(self, tenant_id, doc_id, entities): ...
    async def upsert_relations(self, tenant_id, doc_id, relations): ...
    async def traverse(self, tenant_id, entity, hops=2, limit=50) -> str:
        return ""
    async def delete_by_doc(self, tenant_id, doc_id) -> None: ...
    async def health_check(self) -> bool:
        return False
