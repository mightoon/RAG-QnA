"""
业务数据适配器（rag/adapters/business_data.py）

NL2SQL：LLM 生成 SQL（schema 描述注入 prompt）
→ 白名单校验（仅 SELECT、仅允许配置声明的表）
→ 执行 → 敏感字段脱敏。
"""
from __future__ import annotations

import re
from typing import Any

import yaml
from sqlalchemy import create_engine, text

from rag.config.models import BusinessDataConfig
from rag.models import QueryConstraint

from .base import BusinessDataAdapter, LLMAdapter
from .registry import AdapterRegistry


@AdapterRegistry.register("business_data", "sqlalchemy")
class SQLAlchemyBusinessData(BusinessDataAdapter):

    def __init__(self, config: BusinessDataConfig, llm: LLMAdapter | None = None):
        self.config = config
        self._llm = llm
        self._engine = create_engine(config.dsn, pool_pre_ping=True)
        self._schema = self._load_schema()

    def _load_schema(self) -> dict:
        from pathlib import Path
        p = Path(self.config.schema_file)
        if not p.exists():
            return {"tables": {}}
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"tables": {}}

    def set_llm(self, llm: LLMAdapter) -> None:
        self._llm = llm

    def describe_schema(self) -> str:
        parts = []
        for tname, tinfo in self._schema.get("tables", {}).items():
            if self.config.allowed_tables and tname not in self.config.allowed_tables:
                continue
            cols = tinfo.get("columns", {})
            col_desc = ", ".join(f"{c}({ct})" for c, ct in cols.items())
            desc = tinfo.get("description", "")
            parts.append(f"表 {tname}（{desc}）：{col_desc}")
        return "\n".join(parts)

    async def nl2sql(self, question: str,
                     constraints: QueryConstraint | None = None,
                     plan_context: dict | None = None) -> str | None:
        if not self._llm:
            return None
        constraint_text = ""
        if constraints:
            if constraints.date_from or constraints.date_to:
                constraint_text += (f"时间范围：{constraints.date_from or '不限'}"
                                    f" 至 {constraints.date_to or '不限'}；")
            if constraints.departments:
                constraint_text += f"部门：{constraints.departments}；"
        prompt = f"""你是 SQL 生成器。根据以下数据库 Schema 和问题生成一条 MySQL 查询语句。

Schema：
{self.describe_schema()}

问题：{question}
{constraint_text}

要求：
1. 只输出一条 SELECT 语句，不要解释，不要 markdown 代码块
2. 只查询上述 Schema 中的表
3. LIMIT 最大 {self.config.max_rows}
4. 如果问题无法用这些表回答，只输出：CANNOT_ANSWER"""
        try:
            result = await self._llm.generate(
                [{"role": "user", "content": prompt}],
                task="rewrite", temperature=0.0, max_tokens=300)
            result = result.strip()
            if "CANNOT_ANSWER" in result or not result:
                return None
            return result
        except Exception:
            return None

    def validate_sql(self, sql: str) -> bool:
        sql_clean = sql.strip().rstrip(";").strip()
        # 仅单条 SELECT
        if not re.match(r"^SELECT\s", sql_clean, re.I):
            return False
        forbidden = re.findall(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|"
                               r"TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|UNION)\b",
                               sql_clean, re.I)
        if forbidden:
            return False
        # 表白名单
        tables = re.findall(r"\bFROM\s+([a-zA-Z_][\w]*)", sql_clean, re.I)
        tables += re.findall(r"\bJOIN\s+([a-zA-Z_][\w]*)", sql_clean, re.I)
        allowed = set(self.config.allowed_tables) or set(
            self._schema.get("tables", {}).keys())
        for t in tables:
            if t.lower() not in {a.lower() for a in allowed}:
                return False
        return True

    async def execute(self, sql: str) -> list[dict]:
        def _run():
            with self._engine.connect() as conn:
                result = conn.execute(text(sql))
                cols = list(result.keys())
                return [dict(zip(cols, row)) for row in
                        result.fetchmany(self.config.max_rows)]
        import asyncio
        rows = await asyncio.to_thread(_run)
        return self._mask_sensitive(rows)

    def _mask_sensitive(self, rows: list[dict]) -> list[dict]:
        sensitive = {s.lower() for s in self.config.sensitive_fields}
        masked = []
        for row in rows:
            new_row = {}
            for k, v in row.items():
                if k.lower() in sensitive and v is not None:
                    s = str(v)
                    new_row[k] = s[:3] + "***" + s[-2:] if len(s) > 5 else "***"
                else:
                    new_row[k] = v
            masked.append(new_row)
        return masked

    async def health_check(self) -> bool:
        try:
            import asyncio
            def _ping():
                with self._engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            await asyncio.to_thread(_ping)
            return True
        except Exception:
            return False
