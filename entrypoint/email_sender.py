"""
SMTP Helper for sending token emails.

Der Admin gibt seine ETH-Credentials im Web-UI ein (werden nur für die Dauer des
Sendens gehalten — kein Storage). Das Template enthält [TOKEN] als Platzhalter.

ETH SMTP:
    host: mail.ethz.ch    port: 587 (STARTTLS)
"""
import smtplib
import ssl
from email.message import EmailMessage
from dataclasses import dataclass


DEFAULT_SMTP_HOST = "mail.ethz.ch"
DEFAULT_SMTP_PORT = 587


@dataclass
class SmtpCredentials:
    username: str
    password: str
    host: str = DEFAULT_SMTP_HOST
    port: int = DEFAULT_SMTP_PORT
    sender_email: str | None = None  # defaults to username@ethz.ch if None

    @property
    def effective_sender(self) -> str:
        if self.sender_email:
            return self.sender_email
        if "@" in self.username:
            return self.username
        return f"{self.username}@ethz.ch"


def test_credentials(creds: SmtpCredentials) -> tuple[bool, str]:
    """Try to login to the SMTP server. Returns (ok, message)."""
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(creds.host, creds.port, timeout=10) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(creds.username, creds.password)
        return True, "SMTP login successful"
    except smtplib.SMTPAuthenticationError:
        return False, "Authentication failed — check username/password"
    except Exception as e:
        return False, f"SMTP error: {e}"


def _is_html(body: str) -> bool:
    """Heuristic: body contains an HTML tag like <a>, <p>, <br> or <html>."""
    import re
    return bool(re.search(r"<\s*(html|a|p|br|div|span|strong|em|ul|ol|li|h[1-6])\b", body, re.I))


def send_token_email(
    creds: SmtpCredentials,
    to_email: str,
    subject: str,
    body_template: str,
    token: str,
    first_name: str = "",
    last_name: str = "",
    eth_id: str = "",
) -> tuple[bool, str]:
    """Send a single personalised token email.

    The body template can contain placeholders [TOKEN], [FIRST_NAME],
    [LAST_NAME], [ETH_ID]. If it contains HTML tags (<a>, <p>, <br>, ...) the
    mail is sent as HTML — links with custom text work. Otherwise it's plain
    text; clients still auto-linkify http:// URLs.
    """
    body = (body_template
            .replace("[TOKEN]", token)
            .replace("[FIRST_NAME]", first_name)
            .replace("[LAST_NAME]", last_name)
            .replace("[ETH_ID]", eth_id))

    msg = EmailMessage()
    msg["From"] = creds.effective_sender
    msg["To"] = to_email
    msg["Subject"] = subject

    if _is_html(body):
        # Provide both a plain-text fallback (tags stripped) and the HTML body
        import re
        plain = re.sub(r"<[^>]+>", "", body)
        msg.set_content(plain)
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(creds.host, creds.port, timeout=15) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(creds.username, creds.password)
            s.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, str(e)
