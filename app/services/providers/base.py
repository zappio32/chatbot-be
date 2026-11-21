from collections.abc import Iterator
from typing import Protocol


class ChatProvider(Protocol):
    def generate_response(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Return a response from the configured AI provider."""

    def generate_stream(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> Iterator[str]:
        """Yield response text chunks from the configured AI provider."""

