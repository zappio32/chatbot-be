import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import event

from app.core.config import Settings
from app.main import create_app
from app.services.postgres_chat_repository import PostgresChatRepository


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL") or os.getenv("POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="NOT RUN: no real PostgreSQL test server configured (set TEST_POSTGRES_URL)",
)


@pytest.fixture
def postgres_repository():
    schema = f"chatbot_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(POSTGRES_URL)
    admin.autocommit = True
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    repository = PostgresChatRepository(POSTGRES_URL)

    @event.listens_for(repository.engine, "connect")
    def set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}", public')
        cursor.close()

    repository.initialize()
    yield repository
    repository.close()

    admin = psycopg.connect(POSTGRES_URL)
    admin.autocommit = True
    admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.close()


def test_postgres_startup_migrations_crud_and_cascade(postgres_repository):
    repository = postgres_repository

    schema_sql = Path("scripts/postgres_schema.sql").read_text(encoding="utf-8")
    with repository.engine.begin() as connection:
        connection.exec_driver_sql(schema_sql)
        connection.exec_driver_sql(schema_sql)

    user = repository.register_user("pg-user@example.com", "secret-password", "PG User")
    assert repository.authenticate_user("pg-user@example.com", "secret-password")["id"] == user["id"]

    conversation = repository.create_conversation(
        "PG conversation",
        "pg-conversation",
        "pg-session",
        user["id"],
    )
    repository.save_exchange("hello", "world", "demo", conversation["id"])
    assert repository.get_conversation(conversation["id"], user_id=user["id"]) is not None

    with pytest.raises(ValueError):
        repository.register_user("pg-user@example.com", "other-password", "Duplicate")

    with repository._session() as session:
        from app.models import User

        session.add(
            User(
                id="unique-rollback-user",
                email="rollback@example.com",
                password_hash="hash",
                display_name="Rollback",
                role="USER",
                active=True,
                email_verified=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.flush()
        session.rollback()
    assert repository.get_user_by_id("unique-rollback-user") is None

    assert repository.delete_conversation(conversation["id"], user_id=user["id"])
    assert repository.get_conversation(conversation["id"], user_id=user["id"]) is None


def test_postgres_app_startup_selection_and_rate_limit(postgres_repository):
    settings = Settings(
        database_path="must-not-be-used.db",
        database_url=POSTGRES_URL,
    )
    application = create_app(settings)
    assert application.state.chat_repository is not None

    repository = postgres_repository
    assert repository.check_rate_limit("pg-rate:test", 2)
    assert repository.check_rate_limit("pg-rate:test", 2)
    assert not repository.check_rate_limit("pg-rate:test", 2)
