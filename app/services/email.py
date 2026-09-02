"""Outbound email — currently just account-verification links.

A no-op (logs and returns) unless ``SMTP_HOST`` is configured, so dev and
tests never touch the network. ``aiosmtplib`` is imported lazily for the
same reason.
"""

from __future__ import annotations

import logging
import uuid

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings
from app.util.urls import absolute_url

log = logging.getLogger("app.email")

_signer = URLSafeTimedSerializer(settings.secret_key, salt="dc-email-verify")


def make_verify_token(user_id: uuid.UUID) -> str:
    return _signer.dumps(str(user_id))


def read_verify_token(token: str) -> uuid.UUID | None:
    try:
        raw = _signer.loads(token, max_age=settings.email_verify_ttl_hours * 3600)
        return uuid.UUID(raw)
    except (BadSignature, ValueError):
        return None


async def send_email(to: str, subject: str, body: str) -> None:
    if not settings.smtp_host:
        log.info("email suppressed (SMTP not configured): to=%s subject=%r", to, subject)
        return
    from email.message import EmailMessage

    import aiosmtplib

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_starttls,
        )
    except Exception:
        log.exception("failed to send email to %s", to)


async def send_verification_email(email: str, username: str, token: str) -> None:
    link = absolute_url(f"/verify/{token}")
    body = (
        f"Здравствуйте, {username}!\n\n"
        f"Чтобы завершить регистрацию в DocsClassifier, подтвердите адрес — "
        f"перейдите по ссылке:\n\n{link}\n\n"
        f"Ссылка действует {settings.email_verify_ttl_hours} ч. "
        f"Если вы не регистрировались, просто проигнорируйте это письмо."
    )
    await send_email(email, "DocsClassifier — подтверждение адреса", body)
