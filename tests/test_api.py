import sqlite3
from types import SimpleNamespace

import pytest
import httpx
import io
import subprocess
import sys
from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter
from PIL import Image
from app.services.document_service import DocumentProcessingError, extract_text
from openai import APITimeoutError
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.main import create_app
from app.services.ai_service import (
    AIProviderRateLimitError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
)
from app.services.chat_repository import ChatRepository
from app.services.providers import openai_provider
from app.db import is_postgres_url, normalize_database_url
from app.services.postgres_chat_repository import PostgresChatRepository


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

    def generate_response(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        self.calls += 1
        self.history = history
        if self.error is not None:
            raise self.error
        return self.response

    def generate_stream(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ):
        self.calls += 1
        self.history = history
        if self.error is not None:
            raise self.error
        midpoint = max(1, len(self.response) // 2)
        yield self.response[:midpoint]
        yield self.response[midpoint:]

    def close(self) -> None:
        self.closed = True


def make_client(
    database_path: str,
    *,
    use_mock_ai: bool = True,
    api_key: str | None = None,
    model: str | None = None,
    ai_provider: str = "groq",
    provider: FakeProvider | None = None,
    cors_origins: list[str] | None = None,
) -> TestClient:
    settings = Settings(
        database_path=database_path,
        use_mock_ai=use_mock_ai,
        groq_api_key=SecretStr(api_key) if api_key else None,
        groq_model=model,
        ai_provider=ai_provider,
        cors_origins=cors_origins or ["http://localhost:5173"],
    )
    return TestClient(create_app(settings, chat_provider=provider))


def test_postgres_url_detection_and_engine_configuration() -> None:
    assert is_postgres_url("postgresql://user:pass@host/db")
    assert is_postgres_url("postgres://user:pass@host/db")
    assert is_postgres_url("postgresql+psycopg://user:pass@host/db")
    assert normalize_database_url("postgres://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )

    repository = PostgresChatRepository(
        "postgresql+psycopg://user:pass@127.0.0.1:1/chatbot"
    )
    assert repository.engine.pool is not None
    assert repository.engine.url.drivername == "postgresql+psycopg"
    repository.close()


def test_postgres_rate_limit_upsert_compiles_with_postgresql_dialect(monkeypatch) -> None:
    class SessionStub:
        statement = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, statement):
            self.statement = statement
            statement.compile(dialect=postgresql.dialect())
            return 1

        def commit(self):
            pass

    repository = object.__new__(PostgresChatRepository)
    session = SessionStub()
    monkeypatch.setattr(repository, "_session", lambda: session)

    assert repository.check_rate_limit("login:test", limit=2)
    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert "ON CONFLICT" in compiled.string
    assert "DO UPDATE" in compiled.string
    assert "RETURNING" in compiled.string


def test_database_url_normalization_preserves_encoded_credentials_and_query() -> None:
    legacy_url = (
        "postgres://user%40domain:p%40ss%3Aword@db.example:5432/"
        "chat%2Fbot?sslmode=require&options=-c%20search_path%3Dchatbot"
    )
    normalized = normalize_database_url(legacy_url)

    assert normalized == (
        "postgresql+psycopg://user%40domain:p%40ss%3Aword@db.example:5432/"
        "chat%2Fbot?sslmode=require&options=-c%20search_path%3Dchatbot"
    )
    assert normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    driver_url = "postgresql+psycopg://user:pass@host/db?sslmode=require"
    assert normalize_database_url(driver_url) == driver_url
    sqlite_url = "sqlite:///data/chatbot.db"
    assert normalize_database_url(sqlite_url) == sqlite_url


def test_app_selects_postgres_repository_without_sqlite_fallback(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "must-not-be-used.db"),
        database_url="postgresql+psycopg://user:pass@127.0.0.1:1/chatbot",
    )
    application = create_app(settings)
    assert isinstance(
        application.state.chat_repository,
        PostgresChatRepository,
    )


def test_app_accepts_legacy_postgres_url_and_keeps_sqlite_selection(tmp_path) -> None:
    postgres_app = create_app(Settings(
        database_path=str(tmp_path / "must-not-be-used.db"),
        database_url="postgres://user:pass@127.0.0.1:1/chatbot",
    ))
    postgres_repository = postgres_app.state.chat_repository
    assert isinstance(postgres_repository, PostgresChatRepository)
    assert postgres_repository.engine.url.drivername == "postgresql+psycopg"
    postgres_repository.close()

    sqlite_app = create_app(Settings(
        database_path=str(tmp_path / "sqlite.db"),
        database_url="sqlite:///ignored-path.db",
    ))
    assert isinstance(sqlite_app.state.chat_repository, ChatRepository)


def test_app_rejects_unsupported_database_url(tmp_path) -> None:
    unsupported_url = "mysql://user:secret-password@host/db"
    settings = Settings(
        database_path=str(tmp_path / "test.db"),
        database_url=unsupported_url,
    )
    with pytest.raises(RuntimeError, match="Unsupported DATABASE_URL") as exc_info:
        create_app(settings)
    assert "secret-password" not in str(exc_info.value)


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
    assert {"request_id", "response", "mode", "conversation_id"} == set(body)
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
    assert "GROQ_API_KEY" in response.json()["detail"]
    assert "GROQ_MODEL" not in response.json()["detail"]


def test_live_chat_returns_safe_error_when_model_is_missing(tmp_path) -> None:
    with make_client(
        str(tmp_path / "test.db"),
        use_mock_ai=False,
        api_key="test-key",
    ) as client:
        response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 503
    assert "GROQ_MODEL" in response.json()["detail"]


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
            groq_api_key="test-key",
            groq_model="configured-test-model",
            groq_base_url="https://api.example.com/v1",
            groq_timeout_seconds=12,
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
            groq_api_key="test-key",
            groq_model="configured-test-model",
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


def test_document_upload_txt_and_retrieval(tmp_path) -> None:
    database_path = str(tmp_path / "documents.db")
    with make_client(database_path) as client:
        created = client.post(
            "/conversations",
            json={"title": "Documents", "conversation_id": "doc-1"},
        )
        assert created.status_code == 201
        uploaded = client.post(
            "/conversations/doc-1/documents",
            files={"files": ("notes.txt", b"Revenue was $125,000", "text/plain")},
        )
        assert uploaded.status_code == 200
        document = uploaded.json()[0]
        assert document["status"] == "ready"
        assert document["extension"] == ".txt"

        listed = client.get("/conversations/doc-1/documents")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == document["id"]

        chunks = client.app.state.chat_repository.retrieve_document_chunks(
            "doc-1",
            "Revenue amount",
            4,
        )
        assert any("125,000" in chunk["text"] for chunk in chunks)

        deleted = client.delete(
            f"/conversations/doc-1/documents/{document['id']}"
        )
        assert deleted.status_code == 204


def test_document_upload_rejects_invalid_type_and_image(tmp_path) -> None:
    database_path = str(tmp_path / "documents-invalid.db")
    with make_client(database_path) as client:
        client.post(
            "/conversations",
            json={"title": "Documents", "conversation_id": "doc-invalid"},
        )
        executable = client.post(
            "/conversations/doc-invalid/documents",
            files={"files": ("malware.exe", b"MZ", "application/octet-stream")},
        )
        assert executable.status_code == 415

        invalid_image = client.post(
            "/conversations/doc-invalid/documents",
            files={"files": ("fake.png", b"not an image", "image/png")},
        )
        assert invalid_image.status_code == 422


def test_image_ocr_is_safe_when_engine_is_unavailable(tmp_path) -> None:
    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color="white").save(image_buffer, format="PNG")
    with pytest.raises(DocumentProcessingError, match="Tesseract OCR"):
        extract_text(
            "empty.png",
            image_buffer.getvalue(),
            ocr_enabled=True,
            tesseract_cmd=None,
        )

    with pytest.raises(DocumentProcessingError, match="invalid"):
        extract_text(
            "corrupt.png",
            b"not a png",
            ocr_enabled=True,
        )


def test_pdf_csv_and_image_uploads(tmp_path) -> None:
    database_path = str(tmp_path / "formats.db")
    pdf_buffer = io.BytesIO()
    pdf_writer = PdfWriter()
    pdf_writer.add_blank_page(width=612, height=792)
    pdf_writer.write(pdf_buffer)

    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color="red").save(image_buffer, format="PNG")

    with make_client(database_path) as client:
        client.post(
            "/conversations",
            json={"title": "Formats", "conversation_id": "formats-1"},
        )
        pdf_upload = client.post(
            "/conversations/formats-1/documents",
            files={"files": ("report.pdf", pdf_buffer.getvalue(), "application/pdf")},
        )
        csv_upload = client.post(
            "/conversations/formats-1/documents",
            files={"files": ("sales.csv", b"Region,Revenue\nNorth,125000\n", "text/csv")},
        )
        image_upload = client.post(
            "/conversations/formats-1/documents",
            files={"files": ("chart.png", image_buffer.getvalue(), "image/png")},
        )

        assert pdf_upload.status_code == 200
        assert pdf_upload.json()[0]["status"] == "limited"
        assert csv_upload.status_code == 200
        assert csv_upload.json()[0]["status"] == "ready"
        assert image_upload.status_code == 200
        assert image_upload.json()[0]["status"] == "limited"


def test_memory_crud_duplicate_sensitive_filter_and_disable(tmp_path) -> None:
    database_path = str(tmp_path / "memory.db")
    with make_client(database_path) as client:
        created = client.post(
            "/memory",
            json={
                "category": "work_and_projects",
                "project": "POS software",
                "content": "Use Laravel for the POS project",
            },
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]

        duplicate = client.post(
            "/memory",
            json={
                "category": "work_and_projects",
                "project": "POS software",
                "content": "Use Laravel for the POS project",
            },
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["id"] == memory_id

        sensitive = client.post(
            "/memory",
            json={
                "category": "important_facts",
                "project": "",
                "content": "My password is secret123",
            },
        )
        assert sensitive.status_code == 400

        disabled = client.patch(
            "/memory/settings",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json() == {"enabled": False}

        enabled = client.patch(
            "/memory/settings",
            json={"enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json() == {"enabled": True}

        updated = client.patch(
            f"/memory/{memory_id}",
            json={
                "category": "work_and_projects",
                "project": "POS software",
                "content": "Use PHP and Laravel for the POS project",
                "enabled": True,
            },
        )
        assert updated.status_code == 200
        assert "PHP" in updated.json()["content"]

        deleted = client.delete(f"/memory/{memory_id}")
        assert deleted.status_code == 204


def test_security_rate_limit_uses_database(tmp_path) -> None:
    database_path = str(tmp_path / "rate.db")
    with make_client(database_path) as client:
        repository = client.app.state.chat_repository
        assert repository.check_rate_limit("login:test", 2)
        assert repository.check_rate_limit("login:test", 2)
        assert not repository.check_rate_limit("login:test", 2)


def test_sqlite_postgres_migration_dry_run(tmp_path) -> None:
    database_path = tmp_path / "migration.db"
    with make_client(str(database_path)) as client:
        client.post(
            "/conversations",
            json={"title": "Migration", "conversation_id": "migration-1"},
        )
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_sqlite_to_postgres.py",
            "--sqlite",
            str(database_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "Source SQLite record counts:" in completed.stdout
    assert "conversations:" in completed.stdout
    assert "No destination changes were made." in completed.stdout


def test_email_verification_and_reset_lifecycle(tmp_path) -> None:
    database_path = str(tmp_path / "verification.db")
    with make_client(database_path) as client:
        registered = client.post(
            "/auth/register",
            json={
                "email": "verify@example.com",
                "password": "StrongPassword123",
            },
        )
        assert registered.status_code == 201
        user_id = registered.json()["user"]["id"]
        verification = client.app.state.chat_repository.create_email_verification(
            user_id,
            60,
        )
        verified = client.post(
            "/auth/verify-email",
            headers={"X-Verification-Token": verification},
        )
        assert verified.status_code == 204
        reused = client.post(
            "/auth/verify-email",
            headers={"X-Verification-Token": verification},
        )
        assert reused.status_code == 400


def test_production_settings_reject_insecure_configuration(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                database_path=str(tmp_path / "production.db"),
                environment="production",
                session_secret=None,
                cors_origins=["https://assistant.example.com"],
                cookie_secure=True,
            )
        )

    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                database_path=str(tmp_path / "production.db"),
                environment="production",
                session_secret=SecretStr("x" * 32),
                cors_origins=["http://localhost:5173"],
                cookie_secure=True,
            )
        )


def test_registration_login_logout_and_password_hash(tmp_path) -> None:
    database_path = str(tmp_path / "auth.db")
    with make_client(database_path) as client:
        registered = client.post(
            "/auth/register",
            json={
                "email": "User@Example.com",
                "password": "StrongPassword123",
                "display_name": "Test User",
            },
        )
        assert registered.status_code == 201
        assert registered.json()["user"]["email"] == "user@example.com"
        csrf = registered.json()["csrf_token"]

        me = client.get("/auth/me")
        assert me.status_code == 200

        logout = client.post(
            "/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert logout.status_code == 204
        assert client.get("/auth/me").status_code == 401

        login = client.post(
            "/auth/login",
            json={
                "email": "USER@example.com",
                "password": "StrongPassword123",
            },
        )
        assert login.status_code == 200

        wrong = client.post(
            "/auth/login",
            json={
                "email": "user@example.com",
                "password": "wrong-password",
            },
        )
        assert wrong.status_code == 401

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE email = 'user@example.com'"
        ).fetchone()
    assert row
    assert "StrongPassword123" not in row[0]
    assert row[0].startswith("$2")


def test_duplicate_email_and_cross_user_record_isolation(tmp_path) -> None:
    database_path = str(tmp_path / "auth-isolation.db")
    with make_client(database_path) as first:
        first_register = first.post(
            "/auth/register",
            json={
                "email": "first@example.com",
                "password": "StrongPassword123",
                "display_name": "First",
            },
        )
        assert first_register.status_code == 201
        first_csrf = first_register.json()["csrf_token"]
        created = first.post(
            "/conversations",
            json={"title": "Private", "conversation_id": "private-1"},
            headers={"X-CSRF-Token": first_csrf},
        )
        assert created.status_code == 201
        memory = first.post(
            "/memory",
            json={
                "category": "important_facts",
                "project": "Private",
                "content": "Private first user fact",
            },
            headers={"X-CSRF-Token": first_csrf},
        )
        assert memory.status_code == 201

    with make_client(database_path) as second:
        second_register = second.post(
            "/auth/register",
            json={
                "email": "second@example.com",
                "password": "StrongPassword123",
                "display_name": "Second",
            },
        )
        assert second_register.status_code == 201
        second_csrf = second_register.json()["csrf_token"]

        duplicate = second.post(
            "/auth/register",
            json={
                "email": "SECOND@example.com",
                "password": "DifferentPassword123",
            },
        )
        assert duplicate.status_code == 409

        assert second.get("/conversations").json() == []
        assert second.get("/conversations/private-1").status_code == 404
        assert second.get("/memory").json() == []
        delete = second.delete(
            "/conversations/private-1",
            headers={"X-CSRF-Token": second_csrf},
        )
        assert delete.status_code == 404


def test_password_reset_token_is_single_use(tmp_path) -> None:
    database_path = str(tmp_path / "reset.db")
    with make_client(database_path) as client:
        registered = client.post(
            "/auth/register",
            json={
                "email": "reset@example.com",
                "password": "OriginalPassword123",
            },
        )
        assert registered.status_code == 201

        forgot = client.post(
            "/auth/forgot-password",
            json={"email": "reset@example.com"},
        )
        assert forgot.status_code == 202

        token = client.app.state.chat_repository.create_password_reset(
            "reset@example.com",
            30,
        )
        assert token
        reset = client.post(
            "/auth/reset-password",
            json={
                "token": token,
                "new_password": "ReplacementPassword123",
            },
        )
        assert reset.status_code == 204
        reused = client.post(
            "/auth/reset-password",
            json={
                "token": token,
                "new_password": "AnotherPassword123",
            },
        )
        assert reused.status_code == 400


def test_memory_sessions_are_isolated(tmp_path) -> None:
    database_path = str(tmp_path / "memory-isolation.db")
    with make_client(database_path) as first:
        created = first.post(
            "/memory",
            json={
                "category": "important_facts",
                "project": "Private",
                "content": "First session personal fact",
            },
        )
        assert created.status_code == 201

    with make_client(database_path) as second:
        memories = second.get("/memory")
        assert memories.status_code == 200
        assert memories.json() == []


def test_memory_suggestions_require_review(tmp_path) -> None:
    database_path = str(tmp_path / "memory-suggestions.db")
    with make_client(database_path) as client:
        created = client.post(
            "/conversations",
            json={"title": "Memory", "conversation_id": "memory-1"},
        )
        assert created.status_code == 201

        chat = client.post(
            "/chat",
            json={
                "message": "Remember: I prefer concise answers",
                "conversation_id": "memory-1",
            },
        )
        assert chat.status_code == 200

        suggestions = client.get("/memory/suggestions")
        assert suggestions.status_code == 200
        assert suggestions.json()

        suggestion_id = suggestions.json()[0]["id"]
        accepted = client.post(
            f"/memory/suggestions/{suggestion_id}/accept"
        )
        assert accepted.status_code == 200


def test_docx_and_xlsx_extraction(tmp_path) -> None:
    database_path = str(tmp_path / "office.db")
    docx_buffer = io.BytesIO()
    document = Document()
    document.add_heading("Sales Report", level=1)
    document.add_paragraph("Total revenue was 125000.")
    document.save(docx_buffer)

    xlsx_buffer = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Revenue"])
    sheet.append(["North", 125000])
    workbook.save(xlsx_buffer)

    with make_client(database_path) as client:
        client.post(
            "/conversations",
            json={"title": "Office", "conversation_id": "office-1"},
        )
        docx_upload = client.post(
            "/conversations/office-1/documents",
            files={
                "files": (
                    "report.docx",
                    docx_buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        xlsx_upload = client.post(
            "/conversations/office-1/documents",
            files={
                "files": (
                    "sales.xlsx",
                    xlsx_buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert docx_upload.status_code == 200
        assert xlsx_upload.status_code == 200
        assert docx_upload.json()[0]["status"] == "ready"
        assert xlsx_upload.json()[0]["source_summary"] == "Sheet: Sales"

        chunks = client.app.state.chat_repository.retrieve_document_chunks(
            "office-1",
            "sales revenue",
            4,
        )
        assert chunks
        assert any("125000" in chunk["text"] or "Sales" in chunk["text"] for chunk in chunks)


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


def test_conversation_crud_and_history(tmp_path) -> None:
    database_path = str(tmp_path / "conversations.db")
    with make_client(database_path) as client:
        created = client.post(
            "/conversations",
            json={"title": "Project discussion", "conversation_id": "project-1"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]

        first = client.post(
            "/chat",
            json={"message": "Remember the codename Atlas", "conversation_id": conversation_id},
        )
        second = client.post(
            "/chat",
            json={"message": "What codename?", "conversation_id": conversation_id},
        )
        assert first.status_code == 200
        assert second.status_code == 200

        detail = client.get(f"/conversations/{conversation_id}")
        assert detail.status_code == 200
        messages = detail.json()["messages"]
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

        renamed = client.patch(
            f"/conversations/{conversation_id}",
            json={"title": "Atlas project"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Atlas project"

        history = client.get("/conversations")
        assert history.status_code == 200
        assert history.json()[0]["title"] == "Atlas project"

        deleted = client.delete(f"/conversations/{conversation_id}")
        assert deleted.status_code == 204
        assert client.get(f"/conversations/{conversation_id}").status_code == 404


def test_conversations_are_isolated_for_provider_context(tmp_path) -> None:
    database_path = str(tmp_path / "isolation.db")
    provider = FakeProvider("Context-aware response")
    with make_client(
        database_path,
        use_mock_ai=False,
        api_key="test-key",
        model="test-model",
        provider=provider,
    ) as client:
        first = client.post("/conversations", json={"title": "First", "conversation_id": "first"})
        second = client.post("/conversations", json={"title": "Second", "conversation_id": "second"})
        assert first.status_code == 201
        assert second.status_code == 201

        client.post(
            "/chat",
            json={"message": "Secret from first conversation", "conversation_id": "first"},
        )
        client.post(
            "/chat",
            json={"message": "Question in second conversation", "conversation_id": "second"},
        )

    assert provider.history == []


def test_stream_chat_persists_completed_response(tmp_path) -> None:
    database_path = str(tmp_path / "stream.db")
    provider = FakeProvider("Streaming complete response")
    with make_client(
        database_path,
        use_mock_ai=False,
        api_key="test-key",
        model="test-model",
        provider=provider,
    ) as client:
        created = client.post(
            "/conversations",
            json={"title": "Stream", "conversation_id": "stream-1"},
        )
        assert created.status_code == 201

        with client.stream(
            "POST",
            "/chat/stream",
            json={"message": "Stream this", "conversation_id": "stream-1"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in response.iter_text())

    assert '"type": "token"' in events
    assert '"type": "done"' in events

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT conversation_id, message, response, mode
            FROM chat_exchanges
            WHERE conversation_id = 'stream-1'
            """
        ).fetchone()
    assert row == (
        "stream-1",
        "Stream this",
        "Streaming complete response",
        "live",
    )
