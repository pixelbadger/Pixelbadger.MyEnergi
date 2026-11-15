"""
Email alerts over SMTP (e.g. Gmail with an app password).

With SMTP_HOST or ALERT_EMAIL_TO unset, alerts are only logged.
Run `python notify.py` to send a test email.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_alert(subject: str, body: str) -> bool:
    """Email an alert. Returns False only if sending was attempted and failed."""
    host = os.environ.get("SMTP_HOST", "").strip()
    to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    if not host or not to:
        logger.warning("alert (email not configured): %s — %s", subject, body)
        return True
    user = os.environ.get("SMTP_USER", "").strip()
    msg = EmailMessage()
    msg["Subject"] = f"[MyEnergi] {subject}"
    msg["From"] = os.environ.get("ALERT_EMAIL_FROM", "").strip() or user or to
    msg["To"] = to
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        logger.info("alert emailed to %s: %s", to, subject)
        return True
    except Exception:
        logger.warning("alert email failed: %s", subject, exc_info=True)
        return False


if __name__ == "__main__":
    import myenergi_client
    logging.basicConfig(level=logging.INFO)
    myenergi_client.load_env()
    ok = send_alert("Test alert", "Email alerts from the MyEnergi service are working.")
    raise SystemExit(0 if ok else 1)
