import os
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_name: str = "A&M FutureTech AI Assistant API"
    use_mock_ai: bool = True
    database_path: str = "data/chatbot.db"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    ai_provider: str = "groq"
    groq_model: str | None = None
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = Field(default=30, gt=0, le=120)
    chat_context_limit: int = Field(default=20, ge=2, le=100)
    upload_root: str = "data/uploads"
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    document_chunk_chars: int = Field(default=3500, ge=500, le=10000)
    document_retrieval_chunks: int = Field(default=4, ge=1, le=20)
    memory_retrieval_limit: int = Field(default=6, ge=1, le=20)
    session_secret: SecretStr | None = None
    environment: str = "development"
    session_expire_hours: int = Field(default=168, ge=1, le=720)
    reset_token_minutes: int = Field(default=30, ge=5, le=1440)
    initial_admin_email: str | None = None
    initial_admin_password: SecretStr | None = None
    database_url: str = "sqlite:///data/chatbot.db"
    cookie_samesite: str = "lax"
    cookie_secure: bool = False
    max_request_bytes: int = Field(default=12 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    login_rate_limit: int = Field(default=5, ge=1, le=100)
    registration_rate_limit: int = Field(default=3, ge=1, le=100)
    reset_rate_limit: int = Field(default=3, ge=1, le=100)
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True
    tesseract_cmd: str | None = None
    poppler_path: str | None = None
    ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_timeout_seconds: int = Field(default=30, ge=5, le=300)
    ocr_max_image_pixels: int = Field(default=25_000_000, ge=1_000_000, le=100_000_000)
    ocr_max_pdf_pages: int = Field(default=30, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_openai_names(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        if values.get("ai_provider") == "openai":
            values["ai_provider"] = "groq"
        legacy_to_groq = {
            "openai_model": "groq_model",
            "openai_api_key": "groq_api_key",
            "openai_base_url": "groq_base_url",
            "openai_timeout_seconds": "groq_timeout_seconds",
        }
        for legacy_name, groq_name in legacy_to_groq.items():
            if groq_name not in values and legacy_name in values:
                values[groq_name] = values[legacy_name]
        return values

    @property
    def openai_model(self) -> str | None:
        return self.groq_model

    @property
    def openai_api_key(self) -> SecretStr | None:
        return self.groq_api_key

    @property
    def openai_base_url(self) -> str:
        return self.groq_base_url

    @property
    def openai_timeout_seconds(self) -> float:
        return self.groq_timeout_seconds

    @field_validator("cors_origins")
    @classmethod
    def reject_wildcard_origins(cls, origins: list[str]) -> list[str]:
        if "*" in origins:
            raise ValueError("CORS_ORIGINS must not contain a wildcard.")
        return origins

    @field_validator("cookie_samesite")
    @classmethod
    def validate_cookie_samesite(cls, value: str) -> str:
        if value not in {"lax", "strict", "none"}:
            raise ValueError("COOKIE_SAMESITE must be lax, strict, or none.")
        if value == "none":
            raise ValueError("COOKIE_SAMESITE=none is not enabled by this application.")
        return value

    @field_validator("groq_base_url")
    @classmethod
    def validate_groq_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "GROQ_BASE_URL must be an HTTP(S) URL without credentials or query values."
            )
        return value.rstrip("/")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        cors_origins = os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        )
        api_key = os.getenv("GROQ_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip()
        model = os.getenv("GROQ_MODEL", os.getenv("OPENAI_MODEL", "")).strip()
        base_url = os.getenv(
            "GROQ_BASE_URL",
            os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
        ).strip()
        return cls(
            app_name=os.getenv("APP_NAME", "A&M FutureTech AI Assistant API"),
            use_mock_ai=os.getenv("USE_MOCK_AI", "true"),
            database_path=os.getenv("DATABASE_PATH", "data/chatbot.db"),
            cors_origins=[
                origin.strip()
                for origin in cors_origins.split(",")
                if origin.strip()
            ],
            ai_provider=os.getenv("AI_PROVIDER", "groq").strip().lower(),
            groq_model=model or None,
            groq_api_key=api_key or None,
            groq_base_url=base_url or "https://api.groq.com/openai/v1",
            groq_timeout_seconds=os.getenv(
                "GROQ_TIMEOUT_SECONDS",
                os.getenv("OPENAI_TIMEOUT_SECONDS", "30"),
            ),
            chat_context_limit=os.getenv("CHAT_CONTEXT_LIMIT", "20"),
            upload_root=os.getenv("UPLOAD_ROOT", "data/uploads"),
            max_upload_bytes=os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)),
            document_chunk_chars=os.getenv("DOCUMENT_CHUNK_CHARS", "3500"),
            document_retrieval_chunks=os.getenv("DOCUMENT_RETRIEVAL_CHUNKS", "4"),
            memory_retrieval_limit=os.getenv("MEMORY_RETRIEVAL_LIMIT", "6"),
            session_secret=(
                os.getenv("SESSION_SECRET", "").strip() or None
            ),
            environment=os.getenv("ENVIRONMENT", "development").strip().lower(),
            session_expire_hours=os.getenv("SESSION_EXPIRE_HOURS", "168"),
            reset_token_minutes=os.getenv("RESET_TOKEN_MINUTES", "30"),
            initial_admin_email=os.getenv("INITIAL_ADMIN_EMAIL", "").strip() or None,
            initial_admin_password=(
                os.getenv("INITIAL_ADMIN_PASSWORD", "").strip() or None
            ),
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/chatbot.db"),
            cookie_samesite=os.getenv("COOKIE_SAMESITE", "lax").strip().lower(),
            cookie_secure=os.getenv("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"},
            max_request_bytes=os.getenv("MAX_REQUEST_BYTES", str(12 * 1024 * 1024)),
            login_rate_limit=os.getenv("LOGIN_RATE_LIMIT", "5"),
            registration_rate_limit=os.getenv("REGISTRATION_RATE_LIMIT", "3"),
            reset_rate_limit=os.getenv("RESET_RATE_LIMIT", "3"),
            smtp_host=os.getenv("SMTP_HOST", "").strip() or None,
            smtp_port=os.getenv("SMTP_PORT", "587"),
            smtp_user=os.getenv("SMTP_USER", "").strip() or None,
            smtp_password=(os.getenv("SMTP_PASSWORD", "").strip() or None),
            smtp_from=os.getenv("SMTP_FROM", "").strip() or None,
            smtp_use_tls=os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"},
            tesseract_cmd=os.getenv("TESSERACT_CMD", "").strip() or None,
            poppler_path=os.getenv("POPPLER_PATH", "").strip() or None,
            ocr_enabled=os.getenv("OCR_ENABLED", "true").strip().lower() in {"1", "true", "yes"},
            ocr_language=os.getenv("OCR_LANGUAGE", "eng").strip() or "eng",
            ocr_timeout_seconds=os.getenv("OCR_TIMEOUT_SECONDS", "30"),
            ocr_max_image_pixels=os.getenv("OCR_MAX_IMAGE_PIXELS", "25000000"),
            ocr_max_pdf_pages=os.getenv("OCR_MAX_PDF_PAGES", "30"),
        )
