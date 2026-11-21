from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import auth, chat, documents, health, memory
from app.core.config import Settings
from app.core.session import SessionMiddleware
from app.services.ai_service import AIService
from app.services.chat_repository import ChatRepository
from app.services.providers.base import ChatProvider
from app.services.email_service import EmailService
from app.db import is_postgres_url, normalize_database_url
from app.services.postgres_chat_repository import PostgresChatRepository


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_request_bytes: int) -> None:
        super().__init__(app)
        self.max_request_bytes = max_request_bytes

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds the configured size limit."},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid request size."},
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app(
    settings: Settings | None = None,
    *,
    chat_provider: ChatProvider | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    if app_settings.environment in {"production", "prod"}:
        if app_settings.session_secret is None:
            raise RuntimeError("SESSION_SECRET is required in production.")
        if len(app_settings.session_secret.get_secret_value()) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
        if not app_settings.cookie_secure:
            raise RuntimeError("COOKIE_SECURE=true is required in production.")
        if not all(
            origin.startswith("https://")
            for origin in app_settings.cors_origins
        ):
            raise RuntimeError("Production CORS origins must use HTTPS.")
    database_url = app_settings.database_url.strip()
    database_url = normalize_database_url(database_url)
    if is_postgres_url(database_url):
        repository = PostgresChatRepository(database_url)
    elif database_url.startswith("sqlite:"):
        repository = ChatRepository(app_settings.database_path)
    else:
        raise RuntimeError(
            "Unsupported DATABASE_URL. Use SQLite or PostgreSQL URL format."
        )
    ai_service = AIService(app_settings, provider=chat_provider)
    email_service = EmailService(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            repository.initialize()
        except (sqlite3.Error, SQLAlchemyError, OSError) as exc:
            raise RuntimeError(
                "Database initialization failed. Verify DATABASE_URL and PostgreSQL availability."
            ) from exc
        if app_settings.initial_admin_email and app_settings.initial_admin_password:
            repository.set_initial_admin(
                app_settings.initial_admin_email,
                app_settings.initial_admin_password.get_secret_value(),
            )
        try:
            yield
        finally:
            ai_service.close()
            close_repository = getattr(repository, "close", None)
            if close_repository is not None:
                close_repository()

    application = FastAPI(
        title=app_settings.app_name,
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.ai_service = ai_service
    application.state.chat_repository = repository
    application.state.email_service = email_service
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
    application.add_middleware(
        SecurityMiddleware,
        max_request_bytes=app_settings.max_request_bytes,
    )
    application.include_router(health.router)
    application.include_router(chat.router)
    application.include_router(documents.router)
    application.include_router(memory.router)
    application.include_router(auth.router)
    session_secret = (
        app_settings.session_secret.get_secret_value()
        if app_settings.session_secret
        else "development-only-session-secret-change-me"
    )
    application.add_middleware(
        SessionMiddleware,
        secret=session_secret,
        secure=app_settings.cookie_secure or app_settings.environment in {"production", "prod"},
    )
    return application


app = create_app()
