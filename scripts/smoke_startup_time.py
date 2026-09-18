"""启动速度验证：所有外部依赖不可达时，容器构建+初始化总耗时。
优化前基线：~74 秒（MinIO 构造 30s + Redis 健康检查 36.5s + MySQL 5s + Milvus 3s，串行叠加）
优化后：~8.5 秒（并行化 + 收紧各依赖默认超时 + 关闭启动期重试）
排查记录见 doc/trouble-shooting.md
"""
import time

t0 = time.perf_counter()

from fastapi.testclient import TestClient
from rag.api.app import create_app_from_env

t_import = time.perf_counter() - t0

t1 = time.perf_counter()
app = create_app_from_env()
with TestClient(app) as c:
    t_ready = time.perf_counter() - t1
    r = c.get("/api/health")
    deg = r.json().get("degraded") or {}
    print(f"import+app-factory: {t_import:.1f}s")
    print(f"startup(lifespan):  {t_ready:.1f}s")
    print(f"total:             {t_import + t_ready:.1f}s")
    print(f"degraded({len(deg)}): {sorted(deg.keys())}")
    ok = (t_import + t_ready) < 15
    print("RESULT:", "PASS" if ok else "FAIL", "- 总耗时 < 15s" if ok else "- 超时！")
