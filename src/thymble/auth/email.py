"""Magic-link email delivery over SMTP."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Annotated, Protocol

from fastapi import Depends

from thymble.config import Settings, get_settings

log = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"] = s.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if s.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=context)
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        with smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=context)
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)


def send_quietly(sender: EmailSender, to: str, subject: str, body: str) -> None:
    """Runs as a background task: failures are logged, never shown to the requester."""
    try:
        sender.send(to, subject, body)
    except Exception:
        log.exception("sending sign-in email failed")


def get_email_sender(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmailSender | None:
    """None when SMTP isn't configured (email sign-in off). Tests override this."""
    if not settings.email_sign_in:
        return None
    return SmtpEmailSender(settings)


def magic_link(settings: Settings, token: str) -> str:
    base = settings.magic_link_url or settings.thymble_public_url.rstrip("/") + "/auth/email"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={token}"


def magic_link_body(link: str, ttl_minutes: int) -> str:
    return (
        "Tap the link below on your iPhone to sign in to Thymble.\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {ttl_minutes} minutes.\n"
        "If you didn't ask to sign in, you can ignore this email.\n"
    )
