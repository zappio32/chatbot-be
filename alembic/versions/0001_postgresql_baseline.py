"""PostgreSQL baseline

Revision ID: 0001_postgresql_baseline
Revises:
Create Date: 2026-11-21
"""
from pathlib import Path

from alembic import op


revision = "0001_postgresql_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema_path = Path(__file__).parents[2] / "scripts" / "postgres_schema.sql"
    op.execute(schema_path.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Preserve records. Downgrades must be performed explicitly with backups.
    pass
