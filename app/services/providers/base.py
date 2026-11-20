from typing import Protocol


class ChatProvider(Protocol):
    def generate_response(self, message: str) -> str:
        """Return a response from the configured AI provider."""

