import logging
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

    def generate_response(self, message: str) -> str:
        model = self._settings.openai_model
        if not model:
            raise AIConfigurationError

        client = self._get_client()
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": message}],
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

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get_client(self) -> OpenAI:
        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is None:
                api_key = self._settings.openai_api_key
                if api_key is None or not api_key.get_secret_value().strip():
                    raise AIConfigurationError("OPENAI_API_KEY is not configured.")

                try:
                    self._client = OpenAI(
                        api_key=api_key.get_secret_value(),
                        base_url=self._settings.openai_base_url,
                        timeout=self._settings.openai_timeout_seconds,
                        max_retries=0,
                    )
                except OpenAIError as exc:
                    logger.error("AI provider client configuration failed.")
                    raise AIConfigurationError(
                        "The AI provider client could not be configured."
                    ) from exc

        return self._client
