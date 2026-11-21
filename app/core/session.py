import hmac
import hashlib
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send, Message


COOKIE_NAME = "am_chat_session"
AUTH_COOKIE_NAME = "am_auth_session"


class SessionMiddleware:
    def __init__(self, app: ASGIApp, secret: str, secure: bool = False) -> None:
        self.app = app
        self.secret = secret
        self.secure = secure

    def sign(self, session_id: str) -> str:
        signature = hmac.new(
            self.secret.encode("utf-8"),
            session_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{session_id}.{signature}"

    def verify(self, value: str | None) -> str | None:
        if not value or "." not in value:
            return None
        session_id, signature = value.rsplit(".", 1)
        expected = hmac.new(
            self.secret.encode("utf-8"),
            session_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        return session_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cookie_header = ""
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie_header = value.decode("latin-1")
                break

        session_cookie = None
        for part in cookie_header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == COOKIE_NAME:
                session_cookie = value
                break

        session_id = self.verify(session_cookie)
        if session_id is None:
            session_id = str(uuid4())

        scope["state"] = scope.setdefault("state", {})
        scope["state"]["session_id"] = session_id

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append(
                    "set-cookie",
                    f"{COOKIE_NAME}={self.sign(session_id)}; "
                    f"Path=/; HttpOnly; SameSite=Lax"
                    f"{'; Secure' if self.secure else ''}",
                )
            await send(message)

        await self.app(scope, receive, send_with_cookie)
