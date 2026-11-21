import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg


TABLES = [
    "users",
    "conversations",
    "chat_exchanges",
    "auth_sessions",
    "password_reset_tokens",
    "email_verification_tokens",
    "security_rate_limits",
    "uploaded_documents",
    "document_chunks",
    "memory_entries",
    "memory_settings",
    "memory_suggestions",
    "memory_audit",
]

SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")


def table_count_sqlite(connection: sqlite3.Connection, table: str) -> int:
    try:
        return connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
    except sqlite3.Error:
        return 0


def table_count_postgres(connection: psycopg.Connection, table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        )
        return cursor.fetchone()[0]


def is_expired(value: str) -> bool:
    try:
        expires = datetime.fromisoformat(value)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def fetch_rows(connection: sqlite3.Connection, table: str) -> list[tuple]:
    try:
        return [
            tuple(row)
            for row in connection.execute(
            f'SELECT * FROM "{table}"'
            ).fetchall()
        ]
    except sqlite3.Error:
        return []


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
        ]
    except sqlite3.Error:
        return []


def insert_rows(
    destination,
    table: str,
    column_names: list[str],
    rows: list[tuple],
    replace_existing: bool,
) -> int:
    if not rows:
        return 0
    column_sql = ", ".join(f'"{column}"' for column in column_names)
    placeholders = ", ".join(["%s"] * len(column_names))
    conflict = ""
    if replace_existing:
        conflict = (
            f' ON CONFLICT ("{column_names[0]}") DO UPDATE SET '
            + ", ".join(
                f'"{column}" = EXCLUDED."{column}"'
                for column in column_names[1:]
            )
        )
    else:
        conflict = f' ON CONFLICT ("{column_names[0]}") DO NOTHING'

    inserted = 0
    with destination.cursor() as cursor:
        for row in rows:
            normalized = list(row)
            boolean_columns = {
                "active",
                "email_verified",
                "revoked",
                "used",
                "enabled",
            }
            datetime_columns = {
                "created_at",
                "updated_at",
                "expires_at",
                "window_started",
            }
            for index, column_name in enumerate(column_names):
                if column_name in boolean_columns:
                    normalized[index] = bool(normalized[index])
                elif column_name in datetime_columns and normalized[index]:
                    value = normalized[index]
                    if isinstance(value, str):
                        try:
                            normalized[index] = datetime.fromisoformat(value)
                        except ValueError:
                            pass
            cursor.execute(
                f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'
                + conflict,
                tuple(normalized),
            )
            inserted += cursor.rowcount
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely migrate existing SQLite data to PostgreSQL."
    )
    parser.add_argument("--sqlite", default="data/chatbot.db")
    parser.add_argument("--postgres-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.is_file():
        print(f"Source database not found: {sqlite_path}", file=sys.stderr)
        return 2

    source = sqlite3.connect(str(sqlite_path))
    source.row_factory = sqlite3.Row

    print("Source SQLite record counts:")
    source_counts = {}
    for table in TABLES:
        count = table_count_sqlite(source, table)
        source_counts[table] = count
        print(f"{table}: {count}")

    if args.dry_run:
        source.close()
        print("Dry run complete. No destination changes were made.")
        return 0
    if not args.postgres_url:
        print("PostgreSQL URL is required for a real migration.", file=sys.stderr)
        source.close()
        return 2

    try:
        destination = psycopg.connect(args.postgres_url)
        destination.autocommit = False
        with destination:
            with destination.cursor() as cursor:
                cursor.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

            inserted_counts = {}
            for table in TABLES:
                column_names = columns(source, table)
                if not column_names:
                    continue
                rows = fetch_rows(source, table)

                if table in {"auth_sessions", "password_reset_tokens", "email_verification_tokens"}:
                    expires_index = column_names.index("expires_at") if "expires_at" in column_names else None
                    if expires_index is not None:
                        rows = [
                            row
                            for row in rows
                            if not is_expired(row[expires_index])
                        ]

                inserted = insert_rows(
                    destination,
                    table,
                    column_names,
                    rows,
                    args.replace_existing,
                )
                inserted_counts[table] = inserted

        print("Destination PostgreSQL record counts:")
        destination_counts = {}
        for table in TABLES:
            count = table_count_postgres(destination, table)
            destination_counts[table] = count
            print(f"{table}: {count}")

        print("Migrated rows:")
        for table, count in inserted_counts.items():
            print(f"{table}: {count}")

        mismatches = [
            table
            for table in TABLES
            if table in destination_counts
            and destination_counts[table] < source_counts.get(table, 0)
        ]
        if mismatches:
            print(
                "Warning: destination counts are below source counts for: "
                + ", ".join(mismatches),
                file=sys.stderr,
            )
            return 3
        print("Migration validation passed.")
    except Exception as exc:
        print(f"Migration failed safely: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        source.close()
        try:
            destination.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
