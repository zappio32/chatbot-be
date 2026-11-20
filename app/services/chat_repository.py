import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal


ChatMode = Literal["demo", "live"]


class ChatRepository:
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        if self._database_path != ":memory:":
            Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)

        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute("BEGIN")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_exchanges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message TEXT NOT NULL,
                        response TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK (mode IN ('demo', 'live')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                self._migrate_mode_constraint(connection)

    def save_exchange(self, message: str, response: str, mode: ChatMode) -> int:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO chat_exchanges (message, response, mode)
                    VALUES (?, ?, ?)
                    """,
                    (message, response, mode),
                )
                if cursor.lastrowid is None:
                    raise sqlite3.DatabaseError("Chat exchange was not saved.")
                return cursor.lastrowid

    @staticmethod
    def _migrate_mode_constraint(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'chat_exchanges'
            """
        ).fetchone()
        if row is None or "check (mode = 'demo')" not in row[0].lower():
            return

        connection.execute(
            """
            CREATE TABLE chat_exchanges_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                response TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('demo', 'live')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO chat_exchanges_new (id, message, response, mode, created_at)
            SELECT id, message, response, mode, created_at FROM chat_exchanges
            """
        )
        connection.execute("DROP TABLE chat_exchanges")
        connection.execute(
            "ALTER TABLE chat_exchanges_new RENAME TO chat_exchanges"
        )
