from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import chat, health
from app.core.config import Settings
from app.services.ai_service import AIService
from app.services.chat_repository import ChatRepository
from app.services.providers.base import ChatProvider


def create_app(
    settings: Settings | None = None,
    *,
    chat_provider: ChatProvider | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    repository = ChatRepository(app_settings.database_path)
    ai_service = AIService(app_settings, provider=chat_provider)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        try:
            yield
        finally:
            ai_service.close()

    application = FastAPI(
        title=app_settings.app_name,
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.ai_service = ai_service
    application.state.chat_repository = repository
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    application.include_router(health.router)
    application.include_router(chat.router)
    return application


app = create_app()
