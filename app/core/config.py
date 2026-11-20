import os
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_name: str = "A&M FutureTech AI Assistant API"
    use_mock_ai: bool = True
    database_path: str = "data/chatbot.db"
    cors_origins: list[str] = ["http://localhost:5173"]
    ai_provider: str = "openai"
    openai_model: str | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    openai_timeout_seconds: float = Field(default=30, gt=0, le=120)

    @field_validator("cors_origins")
    @classmethod
    def reject_wildcard_origins(cls, origins: list[str]) -> list[str]:
        if "*" in origins:
            raise ValueError("CORS_ORIGINS must not contain a wildcard.")
        return origins

    @field_validator("openai_base_url")
    @classmethod
    def validate_openai_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None

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
                "OPENAI_BASE_URL must be an HTTP(S) URL without credentials or query values."
            )
        return value.rstrip("/")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        return cls(
            app_name=os.getenv("APP_NAME", "A&M FutureTech AI Assistant API"),
            use_mock_ai=os.getenv("USE_MOCK_AI", "true"),
            database_path=os.getenv("DATABASE_PATH", "data/chatbot.db"),
            cors_origins=[
                origin.strip()
                for origin in cors_origins.split(",")
                if origin.strip()
            ],
            ai_provider=os.getenv("AI_PROVIDER", "openai").strip().lower(),
            openai_model=model or None,
            openai_api_key=api_key or None,
            openai_base_url=base_url or None,
            openai_timeout_seconds=os.getenv("OPENAI_TIMEOUT_SECONDS", "30"),
        )
