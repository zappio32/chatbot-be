from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.services.providers.base import ChatProvider


class AIServiceError(Exception):
    status_code = 503
    public_message = "The AI service is unavailable. Please try again later."


class AIConfigurationError(AIServiceError):
    def __init__(
        self,
        public_message: str = (
            "AI provider is not configured. Check the backend provider settings."
        ),
    ) -> None:
        self.public_message = public_message


class AIProviderUnavailableError(AIServiceError):
    status_code = 502


class AIProviderRejectedError(AIServiceError):
    status_code = 502
    public_message = (
        "The AI provider rejected the request. Check backend provider configuration."
    )


class AIProviderTimeoutError(AIServiceError):
    status_code = 504
    public_message = "The AI provider request timed out. Please try again."


class AIProviderRateLimitError(AIServiceError):
    status_code = 429
    public_message = "The AI provider rate limit was reached. Please try again later."


@dataclass(frozen=True)
class AIResponse:
    text: str
    mode: Literal["demo", "live"]


class AIService:
    def __init__(
        self,
        settings: Settings,
        provider: ChatProvider | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider

    def generate_response(self, message: str) -> AIResponse:
        if self._settings.use_mock_ai:
            return AIResponse(
                text=f"DEMO MODE: This is a mock response. You said: {message}",
                mode="demo",
            )

        if self._settings.ai_provider != "openai":
            raise AIConfigurationError(
                "The configured AI provider is not supported."
            )
        if not self._settings.openai_model:
            raise AIConfigurationError("OPENAI_MODEL is not configured.")

        provider = self._provider
        if provider is None:
            from app.services.providers.openai_provider import OpenAIProvider

            provider = OpenAIProvider(self._settings)
            self._provider = provider

        response = provider.generate_response(message)
        return AIResponse(text=response, mode="live")

    def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if close is not None:
            close()
