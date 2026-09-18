"""批量入库 CLI（scripts/ingest.py）

用法：
    python scripts/ingest.py --dir ./data/manuals \
        --collection default --roles admin,engineer --wait

说明：不经 HTTP API，直接复用 ServiceContainer 与入库协调器，
适合运维侧批量导入。文件任务入队后由协调器消费；
--wait 时轮询任务状态直至全部完成。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag.container import ServiceContainer  # noqa: E402
from rag.ingestion.coordinator import IngestItem  # noqa: E402

SUPPORTED = {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".html",
             ".png", ".jpg", ".jpeg"}


async def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 批量入库工具")
    ap.add_argument("--dir", required=True, help="待入库目录")
    ap.add_argument("--collection", default="default")
    ap.add_argument("--roles", default="", help="可见角色，逗号分隔")
    ap.add_argument("--tenant", default=None, help="覆盖默认租户")
    ap.add_argument("--wait", action="store_true", help="等待全部完成")
    args = ap.parse_args()

    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(f"目录不存在: {root}")
        return 2

    container = ServiceContainer()
    await container.initialize()
    await container.ingest_coordinator.start()

    tenant = args.tenant or container.config.tenant_id
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]

    items = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1].lower() in SUPPORTED:
                items.append(IngestItem(
                    filename=name,
                    file_path=os.path.join(dirpath, name)))
    if not items:
        print("未发现可入库文件")
        return 0

    _batch, tasks = await container.ingest_coordinator.submit(
        items=items, tenant_id=tenant, collection=args.collection,
        user_id="cli", source_type="batch",
        allowed_roles=roles or None)
    print(f"已提交 {len(tasks)} 个任务")

    if args.wait:
        pending = {t.task_id for t in tasks}
        while pending:
            await asyncio.sleep(5)
            done = set()
            for tid in pending:
                t = await container.meta.get_task(tid)
                if t and t.status.value in ("done", "partial", "failed"):
                    print(f"  [{t.status.value}] {t.filename} "
                          f"chunks={t.written_chunks}")
                    done.add(tid)
            pending -= done
        print("全部完成")

    await container.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
