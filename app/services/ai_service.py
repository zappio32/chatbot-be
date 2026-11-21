from dataclasses import dataclass
from collections.abc import Iterator
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

    def __init__(self, public_message: str | None = None) -> None:
        if public_message:
            self.public_message = public_message


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

    def generate_response(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> AIResponse:
        if self._settings.use_mock_ai:
            return AIResponse(
                text=f"DEMO MODE: This is a mock response. You said: {message}",
                mode="demo",
            )

        if self._settings.ai_provider != "groq":
            raise AIConfigurationError(
                "The configured AI provider is not supported."
            )
        if not self._settings.groq_model:
            raise AIConfigurationError("GROQ_MODEL is not configured.")

        provider = self._provider
        if provider is None:
            from app.services.providers.openai_provider import OpenAIProvider

            provider = OpenAIProvider(self._settings)
            self._provider = provider

        response = provider.generate_response(message, history)
        return AIResponse(text=response, mode="live")

    def generate_stream(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[Literal["demo", "live"], Iterator[str]]:
        if self._settings.use_mock_ai:
            text = f"DEMO MODE: This is a mock response. You said: {message}"
            return "demo", iter(text[i:i + 12] for i in range(0, len(text), 12))

        if self._settings.ai_provider != "groq":
            raise AIConfigurationError("The configured AI provider is not supported.")
        if not self._settings.groq_model:
            raise AIConfigurationError("GROQ_MODEL is not configured.")

        provider = self._provider
        if provider is None:
            from app.services.providers.openai_provider import OpenAIProvider

            provider = OpenAIProvider(self._settings)
            self._provider = provider

        generate_stream = getattr(provider, "generate_stream", None)
        if generate_stream is None:
            return "live", iter([provider.generate_response(message, history)])
        return "live", generate_stream(message, history)

    def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if close is not None:
            close()
