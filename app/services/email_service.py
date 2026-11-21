import smtplib
from email.message import EmailMessage

from app.core.config import Settings


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(
            self._settings.smtp_host
            and self._settings.smtp_from
        )

    def send(self, recipient: str, subject: str, body: str) -> bool:
        if not self.is_configured():
            return False
        message = EmailMessage()
        message["To"] = recipient
        message["From"] = self._settings.smtp_from
        message["Subject"] = subject
        message.set_content(body)

        if self._settings.smtp_use_tls:
            server = smtplib.SMTP(
                self._settings.smtp_host,
                self._settings.smtp_port,
                timeout=10,
            )
            server.starttls()
        else:
            server = smtplib.SMTP(
                self._settings.smtp_host,
                self._settings.smtp_port,
                timeout=10,
            )

        try:
            if self._settings.smtp_user:
                password = (
                    self._settings.smtp_password.get_secret_value()
                    if self._settings.smtp_password
                    else ""
                )
                server.login(self._settings.smtp_user, password)
            server.send_message(message)
            return True
        finally:
            server.quit()
