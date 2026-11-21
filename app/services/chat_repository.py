import sqlite3
import re
import bcrypt
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4


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
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT 'legacy',
                        title TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conversation_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(conversations)")
                }
                if "session_id" not in conversation_columns:
                    connection.execute(
                        "ALTER TABLE conversations "
                        "ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'"
                    )
                conversation_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(conversations)")
                }
                if "user_id" not in conversation_columns:
                    connection.execute(
                        "ALTER TABLE conversations ADD COLUMN user_id TEXT"
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_exchanges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT,
                        message TEXT NOT NULL,
                        response TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK (mode IN ('demo', 'live')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                    )
                    """
                )
                self._migrate_mode_constraint(connection)
                self._migrate_conversation_column(connection)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_updated_at "
                    "ON conversations(updated_at DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_exchanges_conversation_created "
                    "ON chat_exchanges(conversation_id, created_at, id)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS uploaded_documents (
                        id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        stored_name TEXT NOT NULL,
                        extension TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('ready', 'error', 'limited')),
                        source_summary TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS document_chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_id TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        text TEXT NOT NULL,
                        FOREIGN KEY (document_id) REFERENCES uploaded_documents(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_uploaded_documents_conversation "
                    "ON uploaded_documents(conversation_id, created_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_document_chunks_document "
                    "ON document_chunks(document_id, chunk_index)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        display_name TEXT NOT NULL DEFAULT '',
                        role TEXT NOT NULL CHECK (role IN ('USER', 'ADMIN')),
                        active INTEGER NOT NULL DEFAULT 1,
                        email_verified INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth_sessions (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        csrf_token TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        revoked INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                user_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(users)")
                }
                if "email_verified" not in user_columns:
                    connection.execute(
                        "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS email_verification_tokens (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        expires_at TEXT NOT NULL,
                        used INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS security_rate_limits (
                        key TEXT PRIMARY KEY,
                        window_started TEXT NOT NULL,
                        count INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        expires_at TEXT NOT NULL,
                        used INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user "
                    "ON auth_sessions(user_id, expires_at, revoked)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_reset_tokens_user "
                    "ON password_reset_tokens(user_id, expires_at, used)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_entries (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        category TEXT NOT NULL,
                        project TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        source_conversation_id TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(session_id, project, content)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_settings (
                        session_id TEXT PRIMARY KEY,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_suggestions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        category TEXT NOT NULL,
                        project TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'accepted', 'dismissed')),
                        source_conversation_id TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(session_id, content)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        memory_id TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                for table_name in (
                    "memory_entries",
                    "memory_suggestions",
                    "memory_settings",
                    "memory_audit",
                    "uploaded_documents",
                ):
                    columns = {
                        row[1]
                        for row in connection.execute(
                            f"PRAGMA table_info({table_name})"
                        )
                    }
                    if "user_id" not in columns:
                        connection.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN user_id TEXT"
                        )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_entries_session "
                    "ON memory_entries(session_id, enabled, project, category)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_suggestions_session "
                    "ON memory_suggestions(session_id, status)"
                )

    def create_conversation(
        self,
        title: str = "New conversation",
        conversation_id: str | None = None,
        session_id: str = "legacy",
        user_id: str | None = None,
    ) -> dict:
        conversation_id = conversation_id or str(uuid4())
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO conversations (id, session_id, user_id, title) VALUES (?, ?, ?, ?)",
                    (conversation_id, session_id, user_id, title),
                )
                row = connection.execute(
                    "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("Conversation was not created.")
        return self._conversation_dict(row)

    def list_conversations(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        with closing(sqlite3.connect(self._database_path)) as connection:
            if user_id:
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM conversations
                    WHERE user_id = ?
                    ORDER BY updated_at DESC, created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            elif session_id:
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM conversations
                    WHERE session_id = ?
                    ORDER BY updated_at DESC, created_at DESC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM conversations
                    ORDER BY updated_at DESC, created_at DESC
                    """
                ).fetchall()
        return [self._conversation_dict(row) for row in rows]

    def get_conversation(
        self,
        conversation_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            if user_id:
                row = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM conversations WHERE id = ? AND user_id = ?
                    """,
                    (conversation_id, user_id),
                ).fetchone()
            elif session_id:
                row = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM conversations WHERE id = ? AND session_id = ?
                    """,
                    (conversation_id, session_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
            if row is None:
                return None
            messages = connection.execute(
                """
                SELECT id, message, response, mode, created_at
                FROM chat_exchanges
                WHERE conversation_id = ?
                ORDER BY id ASC
                """,
                (conversation_id,),
            ).fetchall()
        conversation = self._conversation_dict(row)
        conversation["messages"] = [
            item
            for exchange_id, message, response, mode, created_at in messages
            for item in (
                {"id": exchange_id * 2 - 1, "role": "user", "content": message, "mode": None, "created_at": created_at},
                {"id": exchange_id * 2, "role": "assistant", "content": response, "mode": mode, "created_at": created_at},
            )
        ]
        return conversation

    def rename_conversation(
        self,
        conversation_id: str,
        title: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                if user_id:
                    cursor = connection.execute(
                        """
                        UPDATE conversations
                        SET title = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND user_id = ?
                        """,
                        (title, conversation_id, user_id),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE conversations
                        SET title = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND session_id = ?
                        """,
                        (title, conversation_id, session_id),
                    )
                if cursor.rowcount == 0:
                    return None
                row = connection.execute(
                    "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
        return self._conversation_dict(row) if row else None

    def delete_conversation(
        self,
        conversation_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute("PRAGMA foreign_keys = ON")
                if user_id:
                    owner_row = connection.execute(
                        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                        (conversation_id, user_id),
                    ).fetchone()
                else:
                    owner_row = connection.execute(
                        "SELECT id FROM conversations WHERE id = ? AND session_id = ?",
                        (conversation_id, session_id),
                    ).fetchone()
                if owner_row is None:
                    return False
                else:
                    documents = connection.execute(
                        "SELECT stored_name FROM uploaded_documents WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchall()
                    cursor = connection.execute(
                        "DELETE FROM conversations WHERE id = ?",
                        (conversation_id,),
                    )
        if cursor.rowcount > 0:
            setattr(self, "_deleted_document_names", [row[0] for row in documents])
        return cursor.rowcount > 0

    def pop_deleted_document_names(self) -> list[str]:
        names = getattr(self, "_deleted_document_names", [])
        if hasattr(self, "_deleted_document_names"):
            delattr(self, "_deleted_document_names")
        return names

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    def check_rate_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int = 900,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                row = connection.execute(
                    "SELECT window_started, count FROM security_rate_limits WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO security_rate_limits (key, window_started, count) VALUES (?, ?, 1)",
                        (key, now.isoformat()),
                    )
                    return True
                started = datetime.fromisoformat(row[0])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if now - started >= timedelta(seconds=window_seconds):
                    connection.execute(
                        "UPDATE security_rate_limits SET window_started = ?, count = 1 WHERE key = ?",
                        (now.isoformat(), key),
                    )
                    return True
                if row[1] >= limit:
                    return False
                connection.execute(
                    "UPDATE security_rate_limits SET count = count + 1 WHERE key = ?",
                    (key,),
                )
                return True

    def register_user(
        self,
        email: str,
        password: str,
        display_name: str,
        role: str = "USER",
    ) -> dict:
        normalized_email = self.normalize_email(email)
        user_id = str(uuid4())
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO users
                            (id, email, password_hash, display_name, role)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            normalized_email,
                            self.hash_password(password),
                            display_name.strip(),
                            role,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("An account with this email already exists.") from exc
        return self.get_user_by_id(user_id)

    def get_user_by_email(self, email: str) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT id, email, password_hash, display_name, role, active,
                       email_verified, created_at, updated_at
                FROM users WHERE email = ?
                """,
                (self.normalize_email(email),),
            ).fetchone()
        return self._auth_user_dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT id, email, password_hash, display_name, role, active,
                       email_verified, created_at, updated_at
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        return self._auth_user_dict(row) if row else None

    def authenticate_user(self, email: str, password: str) -> dict | None:
        user = self.get_user_by_email(email)
        if user is None or not user["active"]:
            return None
        if not self.verify_password(password, user["password_hash"]):
            return None
        return user

    def create_auth_session(
        self,
        user_id: str,
        expires_hours: int,
    ) -> dict:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        ).isoformat()
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO auth_sessions
                        (id, user_id, csrf_token, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (session_id, user_id, csrf_token, expires_at),
                )
        return {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
        }

    def get_auth_session(self, session_id: str) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT s.id, s.user_id, s.csrf_token, s.expires_at, s.revoked,
                       u.email, u.display_name, u.role, u.active
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row[3])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if row[4] or not row[8] or expires_at <= datetime.now(timezone.utc):
            return None
        return {
            "session_id": row[0],
            "user_id": row[1],
            "csrf_token": row[2],
            "expires_at": row[3],
            "email": row[5],
            "display_name": row[6],
            "role": row[7],
        }

    def revoke_auth_session(self, session_id: str) -> bool:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE auth_sessions SET revoked = 1 WHERE id = ?",
                    (session_id,),
                )
        return cursor.rowcount > 0

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> bool:
        user = self.get_user_by_id(user_id)
        if user is None:
            return False
        if not self.verify_password(current_password, user["password_hash"]):
            return False
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (self.hash_password(new_password), user_id),
                )
                connection.execute(
                    "UPDATE auth_sessions SET revoked = 1 WHERE user_id = ?",
                    (user_id,),
                )
        return True

    def create_password_reset(
        self,
        email: str,
        minutes: int,
    ) -> str | None:
        user = self.get_user_by_email(email)
        if user is None:
            return None
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ).isoformat()
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO password_reset_tokens
                        (id, user_id, token_hash, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid4()), user["id"], token_hash, expires_at),
                )
        return token

    def create_email_verification(
        self,
        user_id: str,
        minutes: int = 60,
    ) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ).isoformat()
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO email_verification_tokens
                        (id, user_id, token_hash, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid4()), user_id, token_hash, expires_at),
                )
        return token

    def verify_email_token(self, token: str) -> bool:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT id, user_id, expires_at
                FROM email_verification_tokens
                WHERE token_hash = ? AND used = 0
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            expires_at = datetime.fromisoformat(row[2])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return False
            with connection:
                connection.execute(
                    "UPDATE users SET email_verified = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row[1],),
                )
                connection.execute(
                    "UPDATE email_verification_tokens SET used = 1 WHERE id = ?",
                    (row[0],),
                )
        return True

    def reset_password(self, token: str, new_password: str) -> bool:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT id, user_id, expires_at
                FROM password_reset_tokens
                WHERE token_hash = ? AND used = 0
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            expires_at = datetime.fromisoformat(row[2])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return False
            with connection:
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (self.hash_password(new_password), row[1]),
                )
                connection.execute(
                    "UPDATE password_reset_tokens SET used = 1 WHERE id = ?",
                    (row[0],),
                )
                connection.execute(
                    "UPDATE auth_sessions SET revoked = 1 WHERE user_id = ?",
                    (row[1],),
                )
        return True

    def set_user_active(self, user_id: str, active: bool) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE users
                    SET active = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (int(active), user_id),
                )
                if cursor.rowcount:
                    if not active:
                        connection.execute(
                            "UPDATE auth_sessions SET revoked = 1 WHERE user_id = ?",
                            (user_id,),
                        )
        return self.get_user_by_id(user_id)

    def set_initial_admin(self, email: str, password: str) -> dict:
        user = self.get_user_by_email(email)
        if user:
            if user["role"] != "ADMIN":
                with closing(sqlite3.connect(self._database_path)) as connection:
                    with connection:
                        connection.execute(
                            "UPDATE users SET role = 'ADMIN', active = 1 WHERE id = ?",
                            (user["id"],),
                        )
            return self.get_user_by_id(user["id"])
        return self.register_user(email, password, "Administrator", role="ADMIN")

    def claim_legacy_session(self, session_id: str, user_id: str) -> int:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE conversations
                    SET user_id = ?, session_id = ?
                    WHERE session_id = ? AND user_id IS NULL
                    """,
                    (user_id, user_id, session_id),
                )
                connection.execute(
                    """
                    UPDATE memory_entries
                    SET user_id = ?, session_id = ?
                    WHERE session_id = ? AND user_id IS NULL
                    """,
                    (user_id, user_id, session_id),
                )
                connection.execute(
                    """
                    UPDATE memory_suggestions
                    SET user_id = ?, session_id = ?
                    WHERE session_id = ? AND user_id IS NULL
                    """,
                    (user_id, user_id, session_id),
                )
                connection.execute(
                    """
                    UPDATE memory_settings
                    SET user_id = ?, session_id = ?
                    WHERE session_id = ?
                    """,
                    (user_id, user_id, session_id),
                )
        return cursor.rowcount

    @staticmethod
    def _auth_user_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "display_name": row[3],
            "role": row[4],
            "active": bool(row[5]),
            "email_verified": bool(row[6]),
            "created_at": row[7],
            "updated_at": row[8],
        }

    def is_memory_enabled(self, session_id: str) -> bool:
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                "SELECT enabled FROM memory_settings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return True if row is None else bool(row[0])

    def set_memory_enabled(self, session_id: str, enabled: bool) -> dict:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO memory_settings (session_id, enabled)
                    VALUES (?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (session_id, int(enabled)),
                )
        self._audit_memory(session_id, "memory_disabled" if not enabled else "memory_enabled")
        return {"enabled": enabled}

    def list_memories(
        self,
        session_id: str,
        project: str | None = None,
    ) -> list[dict]:
        with closing(sqlite3.connect(self._database_path)) as connection:
            if project:
                rows = connection.execute(
                    """
                    SELECT id, category, project, content, enabled,
                           source_conversation_id, created_at, updated_at
                    FROM memory_entries
                    WHERE session_id = ? AND project = ?
                    ORDER BY updated_at DESC
                    """,
                    (session_id, project),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, category, project, content, enabled,
                           source_conversation_id, created_at, updated_at
                    FROM memory_entries
                    WHERE session_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (session_id,),
                ).fetchall()
        return [self._memory_dict(row) for row in rows]

    def create_memory(
        self,
        session_id: str,
        category: str,
        project: str,
        content: str,
        source_conversation_id: str | None = None,
    ) -> dict:
        memory_id = str(uuid4())
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO memory_entries
                            (id, session_id, category, project, content, source_conversation_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (memory_id, session_id, category, project, content, source_conversation_id),
                    )
                except sqlite3.IntegrityError:
                    row = connection.execute(
                        """
                        SELECT id, category, project, content, enabled,
                               source_conversation_id, created_at, updated_at
                        FROM memory_entries
                        WHERE session_id = ? AND project = ? AND content = ?
                        """,
                        (session_id, project, content),
                    ).fetchone()
                    if row:
                        return self._memory_dict(row)
                    raise
                row = connection.execute(
                    """
                    SELECT id, category, project, content, enabled,
                           source_conversation_id, created_at, updated_at
                    FROM memory_entries WHERE id = ?
                    """,
                    (memory_id,),
                ).fetchone()
        self._audit_memory(session_id, "memory_created", memory_id)
        return self._memory_dict(row)

    def update_memory(
        self,
        session_id: str,
        memory_id: str,
        category: str,
        project: str,
        content: str,
        enabled: bool,
    ) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE memory_entries
                    SET category = ?, project = ?, content = ?, enabled = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND session_id = ?
                    """,
                    (category, project, content, int(enabled), memory_id, session_id),
                )
                if cursor.rowcount == 0:
                    return None
                row = connection.execute(
                    """
                    SELECT id, category, project, content, enabled,
                           source_conversation_id, created_at, updated_at
                    FROM memory_entries WHERE id = ? AND session_id = ?
                    """,
                    (memory_id, session_id),
                ).fetchone()
        self._audit_memory(session_id, "memory_updated", memory_id)
        return self._memory_dict(row)

    def delete_memory(self, session_id: str, memory_id: str) -> bool:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM memory_entries WHERE id = ? AND session_id = ?",
                    (memory_id, session_id),
                )
        if cursor.rowcount:
            self._audit_memory(session_id, "memory_deleted", memory_id)
        return cursor.rowcount > 0

    def clear_memories(self, session_id: str) -> int:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM memory_entries WHERE session_id = ?",
                    (session_id,),
                )
        self._audit_memory(session_id, "memories_cleared")
        return cursor.rowcount

    def retrieve_memories(
        self,
        session_id: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        if not self.is_memory_enabled(session_id):
            return []
        terms = [
            term.lower()
            for term in re.findall(r"[A-Za-z0-9_]+", query)
            if len(term) > 1
        ]
        with closing(sqlite3.connect(self._database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, category, project, content, enabled,
                       source_conversation_id, created_at, updated_at
                FROM memory_entries
                WHERE session_id = ? AND enabled = 1
                """,
                (session_id,),
            ).fetchall()
        memories = [self._memory_dict(row) for row in rows]
        if not terms:
            return memories[:limit]
        ranked = []
        for memory in memories:
            haystack = f"{memory['project']} {memory['category']} {memory['content']}".lower()
            score = sum(haystack.count(term) for term in terms)
            ranked.append((score, memory))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[:limit] if item[0] > 0]

    def add_memory_suggestion(
        self,
        session_id: str,
        category: str,
        project: str,
        content: str,
        source_conversation_id: str | None,
    ) -> dict | None:
        suggestion_id = str(uuid4())
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO memory_suggestions
                            (id, session_id, category, project, content,
                             source_conversation_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            suggestion_id,
                            session_id,
                            category,
                            project,
                            content,
                            source_conversation_id,
                        ),
                    )
                except sqlite3.IntegrityError:
                    return None
                row = connection.execute(
                    """
                    SELECT id, category, project, content, status,
                           source_conversation_id, created_at
                    FROM memory_suggestions WHERE id = ?
                    """,
                    (suggestion_id,),
                ).fetchone()
        return self._suggestion_dict(row)

    def list_memory_suggestions(self, session_id: str) -> list[dict]:
        with closing(sqlite3.connect(self._database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, category, project, content, status,
                       source_conversation_id, created_at
                FROM memory_suggestions
                WHERE session_id = ? AND status = 'pending'
                ORDER BY created_at DESC
                """,
                (session_id,),
            ).fetchall()
        return [self._suggestion_dict(row) for row in rows]

    def accept_memory_suggestion(self, session_id: str, suggestion_id: str) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT category, project, content, source_conversation_id
                    FROM memory_suggestions
                    WHERE id = ? AND session_id = ? AND status = 'pending'
                    """,
                    (suggestion_id, session_id),
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    "UPDATE memory_suggestions SET status = 'accepted' WHERE id = ?",
                    (suggestion_id,),
                )
        memory = self.create_memory(
            session_id,
            row[0],
            row[1],
            row[2],
            row[3],
        )
        return memory

    def dismiss_memory_suggestion(self, session_id: str, suggestion_id: str) -> bool:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE memory_suggestions SET status = 'dismissed'
                    WHERE id = ? AND session_id = ? AND status = 'pending'
                    """,
                    (suggestion_id, session_id),
                )
        return cursor.rowcount > 0

    def _audit_memory(
        self,
        session_id: str,
        action: str,
        memory_id: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO memory_audit (session_id, action, memory_id) VALUES (?, ?, ?)",
                    (session_id, action, memory_id),
                )

    @staticmethod
    def _memory_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "category": row[1],
            "project": row[2],
            "content": row[3],
            "enabled": bool(row[4]),
            "source_conversation_id": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

    @staticmethod
    def _suggestion_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "category": row[1],
            "project": row[2],
            "content": row[3],
            "status": row[4],
            "source_conversation_id": row[5],
            "created_at": row[6],
        }

    def clear_conversation(
        self,
        conversation_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute("PRAGMA foreign_keys = ON")
                if user_id:
                    conversation = connection.execute(
                        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                        (conversation_id, user_id),
                    ).fetchone()
                else:
                    conversation = connection.execute(
                        "SELECT id FROM conversations WHERE id = ? AND session_id = ?",
                        (conversation_id, session_id),
                    ).fetchone()
                if conversation is None:
                    return False
                connection.execute(
                    "DELETE FROM chat_exchanges WHERE conversation_id = ?",
                    (conversation_id,),
                )
                connection.execute(
                    "UPDATE conversations SET title = 'New conversation', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (conversation_id,),
                )
        return True

    def get_context(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[dict[str, str]]:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return []
        messages = conversation["messages"][-limit:]
        return [
            {"role": message["role"], "content": message["content"]}
            for message in messages
        ]

    def add_document(
        self,
        document_id: str,
        conversation_id: str,
        original_name: str,
        stored_name: str,
        extension: str,
        size_bytes: int,
        status: str,
        source_summary: str,
        chunks: list[dict],
        user_id: str | None = None,
    ) -> dict:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute("PRAGMA foreign_keys = ON")
                if connection.execute(
                    "SELECT id FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone() is None:
                    raise sqlite3.IntegrityError("Conversation not found")
                connection.execute(
                    """
                    INSERT INTO uploaded_documents
                        (id, conversation_id, user_id, original_name, stored_name, extension,
                         size_bytes, status, source_summary)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        conversation_id,
                        user_id,
                        original_name,
                        stored_name,
                        extension,
                        size_bytes,
                        status,
                        source_summary,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO document_chunks
                        (document_id, chunk_index, source, text)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (document_id, index, chunk["source"], chunk["text"])
                        for index, chunk in enumerate(chunks)
                    ],
                )
                row = connection.execute(
                    """
                    SELECT id, conversation_id, original_name, extension, size_bytes,
                           status, source_summary, created_at
                    FROM uploaded_documents WHERE id = ?
                    """,
                    (document_id,),
                ).fetchone()
        return self._document_dict(row)

    def list_documents(self, conversation_id: str) -> list[dict]:
        with closing(sqlite3.connect(self._database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, original_name, extension, size_bytes,
                       status, source_summary, created_at
                FROM uploaded_documents
                WHERE conversation_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (conversation_id,),
            ).fetchall()
        return [self._document_dict(row) for row in rows]

    def get_document(self, document_id: str, conversation_id: str) -> dict | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT id, conversation_id, original_name, extension, size_bytes,
                       status, source_summary, created_at
                FROM uploaded_documents
                WHERE id = ? AND conversation_id = ?
                """,
                (document_id, conversation_id),
            ).fetchone()
        return self._document_dict(row) if row else None

    def get_document_storage_name(
        self,
        document_id: str,
        conversation_id: str,
    ) -> str | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            row = connection.execute(
                """
                SELECT stored_name FROM uploaded_documents
                WHERE id = ? AND conversation_id = ?
                """,
                (document_id, conversation_id),
            ).fetchone()
        return row[0] if row else None

    def delete_document(self, document_id: str, conversation_id: str) -> str | None:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                connection.execute("PRAGMA foreign_keys = ON")
                row = connection.execute(
                    """
                    SELECT stored_name FROM uploaded_documents
                    WHERE id = ? AND conversation_id = ?
                    """,
                    (document_id, conversation_id),
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    "DELETE FROM uploaded_documents WHERE id = ?",
                    (document_id,),
                )
        return row[0]

    def retrieve_document_chunks(
        self,
        conversation_id: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        with closing(sqlite3.connect(self._database_path)) as connection:
            rows = connection.execute(
                """
                SELECT d.original_name, c.source, c.text
                FROM document_chunks c
                JOIN uploaded_documents d ON d.id = c.document_id
                WHERE d.conversation_id = ?
                ORDER BY d.created_at DESC, c.document_id, c.chunk_index
                """,
                (conversation_id,),
            ).fetchall()
        chunks = [
            {"text": row[2], "source": f"{row[0]} — {row[1]}"}
            for row in rows
        ]
        from app.services.document_service import retrieve_chunks
        return retrieve_chunks(chunks, query, limit)

    @staticmethod
    def _document_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "conversation_id": row[1],
            "original_name": row[2],
            "extension": row[3],
            "size_bytes": row[4],
            "status": row[5],
            "source_summary": row[6],
            "created_at": row[7],
        }

    def save_exchange(
        self,
        message: str,
        response: str,
        mode: ChatMode,
        conversation_id: str | None = None,
    ) -> int:
        with closing(sqlite3.connect(self._database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO chat_exchanges (conversation_id, message, response, mode)
                    VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, message, response, mode),
                )
                if cursor.lastrowid is None:
                    raise sqlite3.DatabaseError("Chat exchange was not saved.")
                if conversation_id:
                    title_row = connection.execute(
                        "SELECT title FROM conversations WHERE id = ?",
                        (conversation_id,),
                    ).fetchone()
                    if title_row and title_row[0] == "New conversation":
                        connection.execute(
                            "UPDATE conversations SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (message[:46] + ("..." if len(message) > 46 else ""), conversation_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (conversation_id,),
                        )
                return cursor.lastrowid

    @staticmethod
    def _conversation_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "title": row[1],
            "created_at": row[2],
            "updated_at": row[3],
        }

    @staticmethod
    def _migrate_conversation_column(connection: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(chat_exchanges)")
        }
        if "conversation_id" not in columns:
            connection.execute(
                "ALTER TABLE chat_exchanges ADD COLUMN conversation_id TEXT "
                "REFERENCES conversations(id) ON DELETE CASCADE"
            )

        legacy_count = connection.execute(
            "SELECT COUNT(*) FROM chat_exchanges WHERE conversation_id IS NULL"
        ).fetchone()[0]
        if legacy_count:
            legacy_id = "legacy"
            connection.execute(
                """
                INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at)
                VALUES ('legacy', 'Previous conversation', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
            connection.execute(
                "UPDATE chat_exchanges SET conversation_id = 'legacy' WHERE conversation_id IS NULL"
            )

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

        columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(chat_exchanges)")
        }
        has_conversation_id = "conversation_id" in columns

        connection.execute(
            """
            CREATE TABLE chat_exchanges_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                message TEXT NOT NULL,
                response TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('demo', 'live')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        if has_conversation_id:
            connection.execute(
                """
                INSERT INTO chat_exchanges_new
                    (id, conversation_id, message, response, mode, created_at)
                SELECT id, conversation_id, message, response, mode, created_at
                FROM chat_exchanges
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO chat_exchanges_new
                    (id, conversation_id, message, response, mode, created_at)
                SELECT id, NULL, message, response, mode, created_at
                FROM chat_exchanges
                """
            )
        connection.execute("DROP TABLE chat_exchanges")
        connection.execute(
            "ALTER TABLE chat_exchanges_new RENAME TO chat_exchanges"
        )
