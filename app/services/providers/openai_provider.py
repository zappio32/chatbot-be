import logging
from collections.abc import Iterator
from threading import Lock

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from app.core.config import Settings
from app.services.ai_service import (
    AIConfigurationError,
    AIProviderRateLimitError,
    AIProviderRejectedError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
)

logger = logging.getLogger(__name__)


class OpenAIProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: OpenAI | None = None
        self._client_lock = Lock()

    def generate_response(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        model = self._settings.groq_model
        if not model:
            raise AIConfigurationError

        client = self._get_client()
        try:
            messages = list(history or [])
            messages.append({"role": "user", "content": message})
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
            )
        except APITimeoutError as exc:
            logger.warning("AI provider request timed out.")
            raise AIProviderTimeoutError from exc
        except RateLimitError as exc:
            logger.warning("AI provider rate limit reached.")
            raise AIProviderRateLimitError from exc
        except APIConnectionError as exc:
            logger.warning("AI provider connection failed.")
            raise AIProviderUnavailableError from exc
        except APIStatusError as exc:
            if exc.status_code in {401, 403}:
                logger.warning("Groq API authentication was rejected.")
                raise AIProviderRejectedError(
                    "Groq API authentication failed. Check GROQ_API_KEY."
                ) from exc
            if exc.status_code >= 500:
                logger.warning("AI provider returned a server error.")
                raise AIProviderUnavailableError from exc
            logger.warning("AI provider rejected a request.")
            raise AIProviderRejectedError from exc
        except OpenAIError as exc:
            logger.error("AI provider request failed (%s).", type(exc).__name__)
            raise AIProviderUnavailableError from exc

        if not completion.choices:
            logger.warning("AI provider returned no response choices.")
            raise AIProviderUnavailableError
        content = completion.choices[0].message.content
        if not content or not content.strip():
            logger.warning("AI provider returned an empty response.")
            raise AIProviderUnavailableError
        return content.strip()

    def generate_stream(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> Iterator[str]:
        model = self._settings.groq_model
        if not model:
            raise AIConfigurationError

        client = self._get_client()
        stream = None
        try:
            messages = list(history or [])
            messages.append({"role": "user", "content": message})
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    yield content
        except APITimeoutError as exc:
            logger.warning("AI provider stream timed out.")
            raise AIProviderTimeoutError from exc
        except RateLimitError as exc:
            logger.warning("AI provider stream rate limit reached.")
            raise AIProviderRateLimitError from exc
        except APIConnectionError as exc:
            logger.warning("AI provider stream connection failed.")
            raise AIProviderUnavailableError from exc
        except APIStatusError as exc:
            if exc.status_code in {401, 403}:
                raise AIProviderRejectedError(
                    "Groq API authentication failed. Check GROQ_API_KEY."
                ) from exc
            if exc.status_code >= 500:
                raise AIProviderUnavailableError from exc
            raise AIProviderRejectedError from exc
        except OpenAIError as exc:
            logger.error("AI provider stream failed (%s).", type(exc).__name__)
            raise AIProviderUnavailableError from exc
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get_client(self) -> OpenAI:
        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is None:
                api_key = self._settings.groq_api_key
                if api_key is None or not api_key.get_secret_value().strip():
                    raise AIConfigurationError("GROQ_API_KEY is not configured.")

                try:
                    self._client = OpenAI(
                        api_key=api_key.get_secret_value(),
                        base_url=self._settings.groq_base_url,
                        timeout=self._settings.groq_timeout_seconds,
                        max_retries=0,
                    )
                except OpenAIError as exc:
                    logger.error("AI provider client configuration failed.")
                    raise AIConfigurationError(
                        "The AI provider client could not be configured."
                    ) from exc

        return self._client
