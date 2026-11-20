import sqlite3
from types import SimpleNamespace

import pytest
import httpx
from openai import APITimeoutError
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.main import create_app
from app.services.ai_service import (
    AIProviderRateLimitError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
)
from app.services.chat_repository import ChatRepository
from app.services.providers import openai_provider


class FakeProvider:
    def __init__(
        self,
        response: str = "A mocked AI response.",
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.closed = False
        self.calls = 0

    def generate_response(self, message: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.closed = True


def make_client(
    database_path: str,
    *,
    use_mock_ai: bool = True,
    api_key: str | None = None,
    model: str | None = None,
    ai_provider: str = "openai",
    provider: FakeProvider | None = None,
    cors_origins: list[str] | None = None,
) -> TestClient:
    settings = Settings(
        database_path=database_path,
        use_mock_ai=use_mock_ai,
        openai_api_key=SecretStr(api_key) if api_key else None,
        openai_model=model,
        ai_provider=ai_provider,
        cors_origins=cors_origins or ["http://localhost:5173"],
    )
    return TestClient(create_app(settings, chat_provider=provider))


def test_health_returns_service_status(tmp_path) -> None:
    with make_client(str(tmp_path / "test.db")) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "A&M FutureTech AI Assistant API",
        "ai_mode": "demo",
    }


def test_chat_returns_demo_response_and_persists_exchange(tmp_path) -> None:
    database_path = str(tmp_path / "test.db")
    with make_client(database_path) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "DEMO MODE: This is a mock response. You said: Hello"
    assert body["mode"] == "demo"
    assert isinstance(body["request_id"], int)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT message, response, mode FROM chat_exchanges WHERE id = ?",
            (body["request_id"],),
        ).fetchone()
    assert row == (
        "Hello",
        "DEMO MODE: This is a mock response. You said: Hello",
        "demo",
    )


def test_mock_mode_does_not_call_injected_live_provider(tmp_path) -> None:
    provider = FakeProvider(error=RuntimeError("must not be called"))
    with make_client(
        str(tmp_path / "test.db"),
        provider=provider,
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 200
    assert response.json()["mode"] == "demo"
    assert provider.calls == 0


def test_chat_uses_mocked_live_provider_and_matches_frontend_contract(
    tmp_path,
) -> None:
    provider = FakeProvider("A genuine-provider-shaped mocked response.")
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        api_key="test-key",
        model="test-model",
        provider=provider,
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "response", "mode"}
    assert body["response"] == "A genuine-provider-shaped mocked response."
    assert body["mode"] == "live"
    assert provider.closed


def test_chat_rejects_empty_message(tmp_path) -> None:
    with make_client(str(tmp_path / "test.db")) as client:
        response = client.post("/chat", json={"message": "   "})

    assert response.status_code == 422


def test_chat_rejects_malformed_json_and_unexpected_fields(tmp_path) -> None:
    with make_client(str(tmp_path / "test.db")) as client:
        malformed = client.post(
            "/chat",
            content="{",
            headers={"Content-Type": "application/json"},
        )
        unexpected = client.post(
            "/chat",
            json={"message": "Hello", "api_key": "must-not-be-accepted"},
        )
        oversized = client.post(
            "/chat",
            json={"message": "x" * 4001},
        )

    assert malformed.status_code == 422
    assert unexpected.status_code == 422
    assert oversized.status_code == 422


def test_live_chat_returns_safe_error_when_api_key_is_missing(tmp_path) -> None:
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        model="test-model",
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]
    assert "OPENAI_MODEL" not in response.json()["detail"]


def test_live_chat_returns_safe_error_when_model_is_missing(tmp_path) -> None:
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        api_key="test-key",
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 503
    assert "OPENAI_MODEL" in response.json()["detail"]


def test_live_chat_rejects_unsupported_provider(tmp_path) -> None:
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        api_key="test-key",
        model="test-model",
        ai_provider="unsupported",
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 503
    assert "not supported" in response.json()["detail"]


def test_live_chat_sanitizes_provider_failures(tmp_path, caplog) -> None:
    provider = FakeProvider(error=AIProviderUnavailableError("secret-test-token"))
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        api_key="test-key",
        model="test-model",
        provider=provider,
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 502
    assert "secret-test-token" not in response.text
    assert "secret-test-token" not in caplog.text
    assert "test-key" not in caplog.text


@pytest.mark.parametrize(
    ("provider_error", "status_code"),
    [
        (AIProviderRateLimitError("provider detail"), 429),
        (AIProviderTimeoutError("provider detail"), 504),
    ],
)
def test_live_chat_maps_rate_limit_and_timeout_safely(
    tmp_path,
    provider_error: Exception,
    status_code: int,
) -> None:
    provider = FakeProvider(error=provider_error)
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        api_key="test-key",
        model="test-model",
        provider=provider,
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == status_code
    assert "provider detail" not in response.text


def test_chat_configures_local_and_production_cors_origins(tmp_path) -> None:
    origins = [
        "http://localhost:5173",
        "https://assistant.example.com",
    ]
    with make_client(
        str(tmp_path / "test.db"),
        cors_origins=origins,
    ) as client:
        for origin in origins:
            response = client.options(
                "/chat",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin


def test_cors_rejects_wildcard_origins() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_origins=["*"])


def test_openai_provider_uses_configured_settings_without_network(
    monkeypatch,
) -> None:
    received: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            received["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="  Provider response  ")
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            received["client"] = kwargs
            self.chat = SimpleNamespace(
                completions=FakeCompletions(),
            )

        def close(self) -> None:
            received["closed"] = True

    monkeypatch.setattr(openai_provider, "OpenAI", FakeClient)
    provider = openai_provider.OpenAIProvider(
        Settings(
            use_mock_ai=False,
            openai_api_key="test-key",
            openai_model="configured-test-model",
            openai_base_url="https://api.example.com/v1",
            openai_timeout_seconds=12,
        )
    )

    assert provider.generate_response("Hello") == "Provider response"
    provider.close()

    assert received["client"] == {
        "api_key": "test-key",
        "base_url": "https://api.example.com/v1",
        "timeout": 12.0,
        "max_retries": 0,
    }
    assert received["request"] == {
        "model": "configured-test-model",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    assert received["closed"] is True


def test_openai_provider_maps_sdk_timeout_without_leaking_details(
    monkeypatch,
    caplog,
) -> None:
    provider = openai_provider.OpenAIProvider(
        Settings(
            use_mock_ai=False,
            openai_api_key="test-key",
            openai_model="configured-test-model",
        )
    )

    class FakeCompletions:
        def create(self, **kwargs):
            raise APITimeoutError(
                request=httpx.Request(
                    "POST",
                    "https://api.example.com/v1/chat/completions",
                )
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    with pytest.raises(AIProviderTimeoutError):
        provider.generate_response("Hello")

    assert "test-key" not in caplog.text


def test_repository_migrates_existing_demo_only_constraint(tmp_path) -> None:
    database_path = str(tmp_path / "existing.db")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE chat_exchanges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                response TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode = 'demo'),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO chat_exchanges (message, response, mode)
            VALUES ('old message', 'old response', 'demo')
            """
        )

    repository = ChatRepository(database_path)
    repository.initialize()
    live_id = repository.save_exchange("new message", "live response", "live")

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT message, response, mode FROM chat_exchanges ORDER BY id"
        ).fetchall()
    assert live_id == 2
    assert rows == [
        ("old message", "old response", "demo"),
        ("new message", "live response", "live"),
    ]
