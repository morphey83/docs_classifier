"""Outbound email — currently just account-verification links.

A no-op (logs and returns) unless ``SMTP_HOST`` is configured, so dev and
tests never touch the network. ``aiosmtplib`` is imported lazily for the
same reason.
"""

from __future__ import annotations

import logging
import secrets

from app.config import settings

log = logging.getLogger("app.email")


def new_verify_code() -> str:
    """A six-digit confirmation code the user types back on the site."""
    return f"{secrets.randbelow(1_000_000):06d}"


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


async def send_verification_email(email: str, username: str, code: str) -> None:
    body = (
        f"Здравствуйте, {username}!\n\n"
        f"Код для подтверждения адреса в DocsClassifier:\n\n    {code}\n\n"
        f"Введите его на странице подтверждения. Код действует "
        f"{settings.email_verify_ttl_hours} ч. "
        f"Если вы не регистрировались, просто проигнорируйте это письмо."
    )
    await send_email(email, "DocsClassifier — код подтверждения", body)
