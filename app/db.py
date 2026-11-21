from collections.abc import Iterator
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def is_postgres_url(database_url: str) -> bool:
    parsed = urlparse(database_url)
    return parsed.scheme in {"postgres", "postgresql", "postgresql+psycopg"}


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


def create_database_engine(database_url: str) -> Engine:
    normalized = normalize_database_url(database_url)
    if not is_postgres_url(normalized):
        raise ValueError("PostgreSQL runtime requires a PostgreSQL DATABASE_URL.")
    return create_engine(
        normalized,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
    )
