"""
通知服务（rag/services/notifications.py）

入库批次完成/失败 → 邮件（SMTP）+ Webhook。
由 IngestionCoordinator 在批次状态落定后调用。
"""
from __future__ import annotations

import hmac
import hashlib
import json
import smtplib
from email.mime.text import MIMEText

from rag.models import IngestBatch
from rag.observability.logging import get_logger

log = get_logger("rag.notify")


class NotificationService:

    def __init__(self, container):
        self.s = container
        self.cfg = container.config.notification

    async def notify_ingest_result(self, batch: IngestBatch) -> None:
        """批次结果通知（done / partial_failed）"""
        if batch.status not in ("done", "partial_failed"):
            return
        subject = (f"[{self.s.config.app_name}] 入库批次{'完成' if batch.status == 'done' else '部分失败'}"
                   f" - {batch.batch_id}")
        body = (f"批次 {batch.batch_id}\n"
                f"知识库: {batch.collection}\n"
                f"总数: {batch.total} / 成功: {batch.succeeded} / "
                f"失败: {batch.failed}\n来源: {batch.source_type}")
        if self.cfg.email_enabled:
            await self._send_email(subject, body)
        if self.cfg.webhook_enabled:
            await self._send_webhook({
                "event": "ingest_batch_finished", "batch_id": batch.batch_id,
                "collection": batch.collection, "status": batch.status,
                "total": batch.total, "succeeded": batch.succeeded,
                "failed": batch.failed})

    async def _send_email(self, subject: str, body: str) -> None:
        """SMTP 纯文本邮件（同步库放线程池）"""
        import asyncio
        cfg = self.cfg
        if not (cfg.smtp_host and cfg.smtp_from):
            return

        def _send():
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = cfg.smtp_from
            msg["To"] = cfg.smtp_user
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                  timeout=10) as server:
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg)
        try:
            await asyncio.to_thread(_send)
        except Exception as e:
            log.warning("email_send_failed", error=str(e))

    async def _send_webhook(self, payload: dict) -> None:
        """Webhook POST（HMAC-SHA256 签名头）"""
        import httpx
        cfg = self.cfg
        try:
            body = json.dumps(payload, ensure_ascii=False)
            headers = {"Content-Type": "application/json"}
            if cfg.webhook_secret:
                sig = hmac.new(cfg.webhook_secret.encode(), body.encode(),
                               hashlib.sha256).hexdigest()
                headers["X-Signature"] = sig
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(cfg.webhook_url, content=body,
                                         headers=headers)
                if resp.status_code >= 300:
                    log.warning("webhook_non_2xx", status=resp.status_code)
        except Exception as e:
            log.warning("webhook_send_failed", error=str(e))
