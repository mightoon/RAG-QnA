"""
对象存储适配器（rag/adapters/storage.py）

minio：S3 兼容对象存储（同步 SDK + asyncio.to_thread）
local_fs：本地文件系统（小规模部署 / 开发环境）
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from rag.config.models import StorageConfig

from .base import StorageAdapter
from .registry import AdapterRegistry

LOCAL_SCHEME = "local://"

# ── 「测试连接」探测口径（配置页用表单当前值直连探测时使用）────────────
# minio-py 默认给 urllib3 配的是 Retry(total=5)：地址不可达 / 被防火墙静默丢包
# 时要等满 5 次连接超时才报错（实测 ~18s），配置页点一下不能等十几秒。构造期
# 已用 Retry(total=0) 收敛（见 MinIOStorage.__init__ 的 TS-002 注释），探测与它
# 保持同一口径；再由总预算兜住"TCP 连上了但服务端不回包"。
PROBE_CONNECT_TIMEOUT_SEC = 2
PROBE_READ_TIMEOUT_SEC = 5          # 探测只发一次 HEAD，读超时可以比业务更短
PROBE_BUDGET_SEC = 8.0


@AdapterRegistry.register("storage", "minio")
class MinIOStorage(StorageAdapter):

    def __init__(self, config: StorageConfig):
        import urllib3
        from minio import Minio
        self.config = config
        # 连接超时收紧：SDK 默认 urllib3 重试会导致不可达时阻塞 ~30s
        self._client = Minio(
            config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=2, read=10),
                retries=urllib3.Retry(total=0),   # 构造期探测不重试：失败即降级
            ),
        )
        self._bucket = config.bucket
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    async def put(self, key: str, content: bytes,
                  content_type: str = "application/octet-stream") -> str:
        import io
        from minio.commonconfig import Tags

        def _upload():
            self._client.put_object(
                self._bucket, key, io.BytesIO(content),
                length=len(content), content_type=content_type,
            )
            return f"s3://{self._bucket}/{key}"

        return await asyncio.to_thread(_upload)

    async def get(self, url: str) -> bytes:
        def _download():
            key = url.split(f"{self._bucket}/", 1)[-1]
            resp = self._client.get_object(self._bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        return await asyncio.to_thread(_download)

    async def delete(self, url: str) -> None:
        def _remove():
            key = url.split(f"{self._bucket}/", 1)[-1]
            self._client.remove_object(self._bucket, key)
        await asyncio.to_thread(_remove)

    async def preview_url(self, url: str, ttl: int = 3600) -> str:
        def _presign():
            key = url.split(f"{self._bucket}/", 1)[-1]
            return self._client.presigned_get_object(
                self._bucket, key, expires=ttl if ttl else 3600)
        return await asyncio.to_thread(_presign)

    async def health_check(self) -> bool:
        try:
            return await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        except Exception:
            return False


@AdapterRegistry.register("storage", "local_fs")
class LocalFSStorage(StorageAdapter):

    def __init__(self, config: StorageConfig):
        self.config = config
        self._root = Path(config.local_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        key = url[len(LOCAL_SCHEME):] if url.startswith(LOCAL_SCHEME) else url
        p = (self._root / key).resolve()
        if not str(p).startswith(str(self._root)):
            raise ValueError(f"路径越界: {url}")
        return p

    async def put(self, key: str, content: bytes,
                  content_type: str = "application/octet-stream") -> str:
        p = self._root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return f"{LOCAL_SCHEME}{key}"

    async def get(self, url: str) -> bytes:
        return self._path(url).read_bytes()

    async def delete(self, url: str) -> None:
        p = self._path(url)
        if p.exists():
            p.unlink()

    async def preview_url(self, url: str, ttl: int = 3600) -> str:
        # 本地模式直接返回原始路径（由 FastAPI 静态路由提供预览）
        return url

    async def health_check(self) -> bool:
        return self._root.exists()
