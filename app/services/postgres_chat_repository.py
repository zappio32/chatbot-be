import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import bcrypt
from sqlalchemy import case, select, update, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import create_database_engine
from app.models import (
    AuthSession,
    ChatExchange,
    Conversation,
    DocumentChunk,
    EmailVerificationToken,
    MemoryAudit,
    MemoryEntry,
    MemorySettings,
    MemorySuggestion,
    PasswordResetToken,
    SecurityRateLimit,
    UploadedDocument,
    User,
)
from app.db import Base


class PostgresChatRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.engine = create_database_engine(database_url)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def _session(self) -> Session:
        return self.SessionLocal()

    @staticmethod
    def _resolve_user_id(session: Session, session_id: str) -> str | None:
        if not session_id:
            return None
        return session_id if session.scalar(
            select(User.id).where(User.id == session_id)
        ) else None

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except (ValueError, TypeError):
            return False

    def create_conversation(
        self,
        title: str = "New conversation",
        conversation_id: str | None = None,
        session_id: str = "legacy",
        user_id: str | None = None,
    ) -> dict:
        conversation_id = conversation_id or str(uuid4())
        now = datetime.now(timezone.utc)
        with self._session() as session:
            conversation = Conversation(
                id=conversation_id,
                session_id=session_id,
                user_id=user_id,
                title=title,
                created_at=now,
                updated_at=now,
            )
            session.add(conversation)
            try:
                session.commit()
            except IntegrityError as exc:
                raise ValueError("Conversation already exists.") from exc
        return self._conversation_dict(conversation)

    def list_conversations(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        with self._session() as session:
            statement = select(Conversation).order_by(
                Conversation.updated_at.desc(),
                Conversation.created_at.desc(),
            )
            if user_id:
                statement = statement.where(Conversation.user_id == user_id)
            elif session_id:
                statement = statement.where(Conversation.session_id == session_id)
            return [self._conversation_dict(row) for row in session.scalars(statement)]

    def get_conversation(
        self,
        conversation_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict | None:
        with self._session() as session:
            statement = select(Conversation).where(Conversation.id == conversation_id)
            if user_id:
                statement = statement.where(Conversation.user_id == user_id)
            elif session_id:
                statement = statement.where(Conversation.session_id == session_id)
            conversation = session.scalar(statement)
            if conversation is None:
                return None
            exchanges = session.scalars(
                select(ChatExchange)
                .where(ChatExchange.conversation_id == conversation_id)
                .order_by(ChatExchange.id)
            ).all()
            result = self._conversation_dict(conversation)
            messages = []
            for exchange in exchanges:
                created_at = exchange.created_at.isoformat()
                messages.append({
                    "id": exchange.id * 2 - 1,
                    "role": "user",
                    "content": exchange.message,
                    "mode": None,
                    "created_at": created_at,
                })
                messages.append({
                    "id": exchange.id * 2,
                    "role": "assistant",
                    "content": exchange.response,
                    "mode": exchange.mode,
                    "created_at": created_at,
                })
            result["messages"] = messages
            return result

    def rename_conversation(
        self,
        conversation_id: str,
        title: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict | None:
        with self._session() as session:
            statement = select(Conversation).where(Conversation.id == conversation_id)
            if user_id:
                statement = statement.where(Conversation.user_id == user_id)
            elif session_id:
                statement = statement.where(Conversation.session_id == session_id)
            conversation = session.scalar(statement)
            if conversation is None:
                return None
            conversation.title = title
            conversation.updated_at = datetime.now(timezone.utc)
            session.commit()
            return self._conversation_dict(conversation)

    def delete_conversation(
        self,
        conversation_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        with self._session() as session:
            statement = select(Conversation).where(Conversation.id == conversation_id)
            if user_id:
                statement = statement.where(Conversation.user_id == user_id)
            elif session_id:
                statement = statement.where(Conversation.session_id == session_id)
            conversation = session.scalar(statement)
            if conversation is None:
                return False
            documents = session.scalars(
                select(UploadedDocument).where(
                    UploadedDocument.conversation_id == conversation_id
                )
            ).all()
            self._deleted_document_names = [document.stored_name for document in documents]
            session.delete(conversation)
            session.commit()
            return True

    def pop_deleted_document_names(self) -> list[str]:
        names = getattr(self, "_deleted_document_names", [])
        if hasattr(self, "_deleted_document_names"):
            delattr(self, "_deleted_document_names")
        return names

    def clear_conversation(
        self,
        conversation_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        with self._session() as session:
            statement = select(Conversation).where(Conversation.id == conversation_id)
            if user_id:
                statement = statement.where(Conversation.user_id == user_id)
            elif session_id:
                statement = statement.where(Conversation.session_id == session_id)
            conversation = session.scalar(statement)
            if conversation is None:
                return False
            session.execute(
                delete(ChatExchange).where(
                    ChatExchange.conversation_id == conversation_id
                )
            )
            conversation.title = "New conversation"
            conversation.updated_at = datetime.now(timezone.utc)
            session.commit()
            return True

    def get_context(self, conversation_id: str, limit: int) -> list[dict[str, str]]:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return []
        messages = conversation["messages"][-limit:]
        return [
            {"role": message["role"], "content": message["content"]}
            for message in messages
        ]

    def save_exchange(
        self,
        message: str,
        response: str,
        mode: str,
        conversation_id: str | None = None,
    ) -> int:
        with self._session() as session:
            exchange = ChatExchange(
                conversation_id=conversation_id,
                message=message,
                response=response,
                mode=mode,
                created_at=datetime.now(timezone.utc),
            )
            session.add(exchange)
            session.flush()
            request_id = exchange.id
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            if conversation:
                if conversation.title == "New conversation":
                    conversation.title = message[:46] + ("..." if len(message) > 46 else "")
                conversation.updated_at = datetime.now(timezone.utc)
            session.commit()
            return request_id

    def register_user(
        self,
        email: str,
        password: str,
        display_name: str,
        role: str = "USER",
    ) -> dict:
        now = datetime.now(timezone.utc)
        user_id = str(uuid4())
        with self._session() as session:
            try:
                user = User(
                    id=user_id,
                    email=self.normalize_email(email),
                    password_hash=self.hash_password(password),
                    display_name=display_name.strip(),
                    role=role,
                    active=True,
                    email_verified=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.commit()
            except IntegrityError as exc:
                raise ValueError("An account with this email already exists.") from exc
        return self.get_user_by_id(user_id)

    def get_user_by_email(self, email: str) -> dict | None:
        with self._session() as session:
            user = session.scalar(
                select(User).where(User.email == self.normalize_email(email))
            )
            return self._auth_user_dict(user) if user else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        with self._session() as session:
            user = session.scalar(select(User).where(User.id == user_id))
            return self._auth_user_dict(user) if user else None

    def authenticate_user(self, email: str, password: str) -> dict | None:
        user = self.get_user_by_email(email)
        if user is None or not user["active"]:
            return None
        if not self.verify_password(password, user["password_hash"]):
            return None
        return user

    def create_auth_session(self, user_id: str, expires_hours: int) -> dict:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        with self._session() as session:
            session.add(AuthSession(
                id=session_id,
                user_id=user_id,
                csrf_token=csrf_token,
                expires_at=expires_at,
                revoked=False,
                created_at=datetime.now(timezone.utc),
            ))
            session.commit()
        return {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at.isoformat(),
        }

    def get_auth_session(self, session_id: str) -> dict | None:
        with self._session() as session:
            row = session.execute(
                select(AuthSession, User).join(User, User.id == AuthSession.user_id)
                .where(AuthSession.id == session_id)
            ).first()
            if row is None:
                return None
            auth_session, user = row
            if auth_session.revoked or not user.active:
                return None
            expires_at = auth_session.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None
            return {
                "session_id": auth_session.id,
                "user_id": user.id,
                "csrf_token": auth_session.csrf_token,
                "expires_at": auth_session.expires_at.isoformat(),
                "email": user.email,
                "display_name": user.display_name,
                "role": user.role,
            }

    def revoke_auth_session(self, session_id: str) -> bool:
        with self._session() as session:
            auth_session = session.scalar(select(AuthSession).where(AuthSession.id == session_id))
            if auth_session is None:
                return False
            auth_session.revoked = True
            session.commit()
            return True

    def check_rate_limit(self, key: str, limit: int, window_seconds: int = 900) -> bool:
        now = datetime.now(timezone.utc)
        with self._session() as session:
            cutoff = now - timedelta(seconds=window_seconds)
            statement = pg_insert(SecurityRateLimit).values(
                key=key,
                window_started=now,
                count=1,
            ).on_conflict_do_update(
                index_elements=[SecurityRateLimit.key],
                set={
                    "window_started": case(
                        (SecurityRateLimit.window_started <= cutoff, now),
                        else_=SecurityRateLimit.window_started,
                    ),
                    "count": case(
                        (SecurityRateLimit.window_started <= cutoff, 1),
                        (
                            SecurityRateLimit.count >= limit,
                            SecurityRateLimit.count,
                        ),
                        else_=SecurityRateLimit.count + 1,
                    ),
                },
            ).returning(SecurityRateLimit.count)
            count = session.scalar(statement)
            session.commit()
            if count is None:
                return False
            return count <= limit

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> bool:
        user = self.get_user_by_id(user_id)
        if user is None or not self.verify_password(current_password, user["password_hash"]):
            return False
        with self._session() as session:
            db_user = session.scalar(select(User).where(User.id == user_id))
            db_user.password_hash = self.hash_password(new_password)
            db_user.updated_at = datetime.now(timezone.utc)
            session.execute(
                update(AuthSession).where(AuthSession.user_id == user_id).values(revoked=True)
            )
            session.commit()
        return True

    def create_password_reset(self, email: str, minutes: int) -> str | None:
        user = self.get_user_by_email(email)
        if user is None:
            return None
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self._session() as session:
            session.add(PasswordResetToken(
                id=str(uuid4()),
                user_id=user["id"],
                token_hash=token_hash,
                expires_at=now + timedelta(minutes=minutes),
                used=False,
                created_at=now,
            ))
            session.commit()
        return token

    def reset_password(self, token: str, new_password: str) -> bool:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self._session() as session:
            reset = session.scalar(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_hash == token_hash,
                    PasswordResetToken.used == False,
                )
            )
            if reset is None:
                return False
            expires_at = reset.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                return False
            user = session.scalar(select(User).where(User.id == reset.user_id))
            if user is None:
                return False
            user.password_hash = self.hash_password(new_password)
            user.updated_at = now
            reset.used = True
            session.execute(
                update(AuthSession).where(AuthSession.user_id == reset.user_id).values(revoked=True)
            )
            session.commit()
            return True

    def create_email_verification(self, user_id: str, minutes: int = 60) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self._session() as session:
            session.add(EmailVerificationToken(
                id=str(uuid4()),
                user_id=user_id,
                token_hash=token_hash,
                expires_at=now + timedelta(minutes=minutes),
                used=False,
                created_at=now,
            ))
            session.commit()
        return token

    def verify_email_token(self, token: str) -> bool:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self._session() as session:
            row = session.scalar(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.token_hash == token_hash,
                    EmailVerificationToken.used == False,
                )
            )
            if row is None:
                return False
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                return False
            user = session.scalar(select(User).where(User.id == row.user_id))
            if user is None:
                return False
            user.email_verified = True
            user.updated_at = now
            row.used = True
            session.commit()
            return True

    def set_initial_admin(self, email: str, password: str) -> dict:
        user = self.get_user_by_email(email)
        if user:
            if user["role"] != "ADMIN":
                with self._session() as session:
                    db_user = session.scalar(select(User).where(User.id == user["id"]))
                    db_user.role = "ADMIN"
                    db_user.active = True
                    db_user.updated_at = datetime.now(timezone.utc)
                    session.commit()
            return self.get_user_by_id(user["id"])
        return self.register_user(email, password, "Administrator", role="ADMIN")

    def set_user_active(self, user_id: str, active: bool) -> dict | None:
        with self._session() as session:
            user = session.scalar(select(User).where(User.id == user_id))
            if user is None:
                return None
            user.active = active
            user.updated_at = datetime.now(timezone.utc)
            if not active:
                session.execute(
                    update(AuthSession).where(AuthSession.user_id == user_id).values(revoked=True)
                )
            session.commit()
        return self.get_user_by_id(user_id)

    def claim_legacy_session(self, session_id: str, user_id: str) -> int:
        if not session_id:
            return 0
        with self._session() as session:
            count = 0
            conversations = session.scalars(
                select(Conversation).where(
                    Conversation.session_id == session_id,
                    Conversation.user_id.is_(None),
                )
            ).all()
            for row in conversations:
                row.user_id = user_id
                row.session_id = user_id
                count += 1

            memories = session.scalars(
                select(MemoryEntry).where(
                    MemoryEntry.session_id == session_id,
                    MemoryEntry.user_id.is_(None),
                )
            ).all()
            for row in memories:
                duplicate = session.scalar(
                    select(MemoryEntry).where(
                        MemoryEntry.session_id == user_id,
                        MemoryEntry.project == row.project,
                        MemoryEntry.content == row.content,
                    )
                )
                if duplicate is not None:
                    session.delete(row)
                else:
                    row.user_id = user_id
                    row.session_id = user_id
                    count += 1

            suggestions = session.scalars(
                select(MemorySuggestion).where(
                    MemorySuggestion.session_id == session_id,
                    MemorySuggestion.user_id.is_(None),
                )
            ).all()
            for row in suggestions:
                duplicate = session.scalar(
                    select(MemorySuggestion).where(
                        MemorySuggestion.session_id == user_id,
                        MemorySuggestion.content == row.content,
                    )
                )
                if duplicate is not None:
                    session.delete(row)
                else:
                    row.user_id = user_id
                    row.session_id = user_id
                    count += 1

            legacy_settings = session.scalar(
                select(MemorySettings).where(MemorySettings.session_id == session_id)
            )
            if legacy_settings:
                target_settings = session.scalar(
                    select(MemorySettings).where(MemorySettings.session_id == user_id)
                )
                if target_settings is not None:
                    session.delete(legacy_settings)
                else:
                    legacy_settings.user_id = user_id
                    legacy_settings.session_id = user_id
            session.commit()
        return count

    def is_memory_enabled(self, session_id: str) -> bool:
        with self._session() as session:
            row = session.scalar(
                select(MemorySettings).where(MemorySettings.session_id == session_id)
            )
            return True if row is None else row.enabled

    def set_memory_enabled(self, session_id: str, enabled: bool) -> dict:
        with self._session() as session:
            user_id = self._resolve_user_id(session, session_id)
            row = session.scalar(
                select(MemorySettings).where(MemorySettings.session_id == session_id)
            )
            now = datetime.now(timezone.utc)
            if row is None:
                session.add(MemorySettings(
                    session_id=session_id,
                    user_id=user_id,
                    enabled=enabled,
                    updated_at=now,
                ))
            else:
                row.enabled = enabled
                row.updated_at = now
            session.commit()
        self._audit_memory(session_id, "memory_disabled" if not enabled else "memory_enabled")
        return {"enabled": enabled}

    def list_memories(self, session_id: str, project: str | None = None) -> list[dict]:
        with self._session() as session:
            statement = select(MemoryEntry).where(MemoryEntry.session_id == session_id)
            if project:
                statement = statement.where(MemoryEntry.project == project)
            statement = statement.order_by(MemoryEntry.updated_at.desc())
            return [self._memory_dict(row) for row in session.scalars(statement)]

    def create_memory(
        self,
        session_id: str,
        category: str,
        project: str,
        content: str,
        source_conversation_id: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        with self._session() as session:
            user_id = self._resolve_user_id(session, session_id)
            existing = session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.session_id == session_id,
                    MemoryEntry.project == project,
                    MemoryEntry.content == content,
                )
            )
            if existing:
                return self._memory_dict(existing)
            row = MemoryEntry(
                id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                category=category,
                project=project,
                content=content,
                enabled=True,
                source_conversation_id=source_conversation_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            result = self._memory_dict(row)
        self._audit_memory(session_id, "memory_created", result["id"])
        return result

    def update_memory(
        self,
        session_id: str,
        memory_id: str,
        category: str,
        project: str,
        content: str,
        enabled: bool,
    ) -> dict | None:
        with self._session() as session:
            row = session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.id == memory_id,
                    MemoryEntry.session_id == session_id,
                )
            )
            if row is None:
                return None
            row.category = category
            row.project = project
            row.content = content
            row.enabled = enabled
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            result = self._memory_dict(row)
        self._audit_memory(session_id, "memory_updated", memory_id)
        return result

    def delete_memory(self, session_id: str, memory_id: str) -> bool:
        with self._session() as session:
            row = session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.id == memory_id,
                    MemoryEntry.session_id == session_id,
                )
            )
            if row is None:
                return False
            session.delete(row)
            session.commit()
        self._audit_memory(session_id, "memory_deleted", memory_id)
        return True

    def clear_memories(self, session_id: str) -> int:
        with self._session() as session:
            rows = session.scalars(
                select(MemoryEntry).where(MemoryEntry.session_id == session_id)
            ).all()
            count = len(rows)
            for row in rows:
                session.delete(row)
            session.commit()
        self._audit_memory(session_id, "memories_cleared")
        return count

    def retrieve_memories(self, session_id: str, query: str, limit: int) -> list[dict]:
        if not self.is_memory_enabled(session_id):
            return []
        memories = self.list_memories(session_id)
        import re
        terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query) if len(term) > 1]
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
        with self._session() as session:
            user_id = self._resolve_user_id(session, session_id)
            existing = session.scalar(
                select(MemorySuggestion).where(
                    MemorySuggestion.session_id == session_id,
                    MemorySuggestion.content == content,
                )
            )
            if existing:
                return None
            row = MemorySuggestion(
                id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                category=category,
                project=project,
                content=content,
                status="pending",
                source_conversation_id=source_conversation_id,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
            return self._suggestion_dict(row)

    def list_memory_suggestions(self, session_id: str) -> list[dict]:
        with self._session() as session:
            rows = session.scalars(
                select(MemorySuggestion)
                .where(
                    MemorySuggestion.session_id == session_id,
                    MemorySuggestion.status == "pending",
                )
                .order_by(MemorySuggestion.created_at.desc())
            ).all()
            return [self._suggestion_dict(row) for row in rows]

    def accept_memory_suggestion(self, session_id: str, suggestion_id: str) -> dict | None:
        with self._session() as session:
            row = session.scalar(
                select(MemorySuggestion).where(
                    MemorySuggestion.id == suggestion_id,
                    MemorySuggestion.session_id == session_id,
                    MemorySuggestion.status == "pending",
                )
            )
            if row is None:
                return None
            row.status = "accepted"
            session.commit()
            suggestion = (row.category, row.project, row.content, row.source_conversation_id)
        return self.create_memory(
            session_id,
            suggestion[0],
            suggestion[1],
            suggestion[2],
            suggestion[3],
        )

    def dismiss_memory_suggestion(self, session_id: str, suggestion_id: str) -> bool:
        with self._session() as session:
            row = session.scalar(
                select(MemorySuggestion).where(
                    MemorySuggestion.id == suggestion_id,
                    MemorySuggestion.session_id == session_id,
                    MemorySuggestion.status == "pending",
                )
            )
            if row is None:
                return False
            row.status = "dismissed"
            session.commit()
            return True

    def _audit_memory(
        self,
        session_id: str,
        action: str,
        memory_id: str | None = None,
    ) -> None:
        with self._session() as session:
            user_id = self._resolve_user_id(session, session_id)
            session.add(MemoryAudit(
                session_id=session_id,
                user_id=user_id,
                action=action,
                memory_id=memory_id,
                created_at=datetime.now(timezone.utc),
            ))
            session.commit()

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
        now = datetime.now(timezone.utc)
        with self._session() as session:
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            if conversation is None:
                raise IntegrityError("Conversation not found", {}, Exception())
            document = UploadedDocument(
                id=document_id,
                conversation_id=conversation_id,
                user_id=user_id,
                original_name=original_name,
                stored_name=stored_name,
                extension=extension,
                size_bytes=size_bytes,
                status=status,
                source_summary=source_summary,
                created_at=now,
            )
            session.add(document)
            session.flush()
            for index, chunk in enumerate(chunks):
                session.add(DocumentChunk(
                    document_id=document_id,
                    chunk_index=index,
                    source=chunk["source"],
                    text=chunk["text"],
                ))
            session.commit()
            return self._document_dict(document)

    def list_documents(self, conversation_id: str) -> list[dict]:
        with self._session() as session:
            rows = session.scalars(
                select(UploadedDocument)
                .where(UploadedDocument.conversation_id == conversation_id)
                .order_by(UploadedDocument.created_at.desc(), UploadedDocument.id.desc())
            ).all()
            return [self._document_dict(row) for row in rows]

    def get_document(self, document_id: str, conversation_id: str) -> dict | None:
        with self._session() as session:
            row = session.scalar(
                select(UploadedDocument).where(
                    UploadedDocument.id == document_id,
                    UploadedDocument.conversation_id == conversation_id,
                )
            )
            return self._document_dict(row) if row else None

    def get_document_storage_name(
        self,
        document_id: str,
        conversation_id: str,
    ) -> str | None:
        with self._session() as session:
            row = session.scalar(
                select(UploadedDocument).where(
                    UploadedDocument.id == document_id,
                    UploadedDocument.conversation_id == conversation_id,
                )
            )
            return row.stored_name if row else None

    def delete_document(self, document_id: str, conversation_id: str) -> str | None:
        with self._session() as session:
            row = session.scalar(
                select(UploadedDocument).where(
                    UploadedDocument.id == document_id,
                    UploadedDocument.conversation_id == conversation_id,
                )
            )
            if row is None:
                return None
            stored_name = row.stored_name
            session.delete(row)
            session.commit()
            return stored_name

    def retrieve_document_chunks(
        self,
        conversation_id: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        with self._session() as session:
            rows = session.execute(
                select(
                    UploadedDocument.original_name,
                    DocumentChunk.source,
                    DocumentChunk.text,
                )
                .join(DocumentChunk, DocumentChunk.document_id == UploadedDocument.id)
                .where(UploadedDocument.conversation_id == conversation_id)
                .order_by(UploadedDocument.created_at.desc(), DocumentChunk.document_id, DocumentChunk.chunk_index)
            ).all()
        chunks = [
            {"text": row.text, "source": f"{row.original_name} — {row.source}"}
            for row in rows
        ]
        from app.services.document_service import retrieve_chunks
        return retrieve_chunks(chunks, query, limit)

    @staticmethod
    def _conversation_dict(row: Conversation) -> dict:
        return {
            "id": row.id,
            "title": row.title,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    @staticmethod
    def _document_dict(row: UploadedDocument) -> dict:
        return {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "original_name": row.original_name,
            "extension": row.extension,
            "size_bytes": row.size_bytes,
            "status": row.status,
            "source_summary": row.source_summary,
            "created_at": row.created_at.isoformat(),
        }

    @staticmethod
    def _memory_dict(row: MemoryEntry) -> dict:
        return {
            "id": row.id,
            "category": row.category,
            "project": row.project,
            "content": row.content,
            "enabled": row.enabled,
            "source_conversation_id": row.source_conversation_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    @staticmethod
    def _suggestion_dict(row: MemorySuggestion) -> dict:
        return {
            "id": row.id,
            "category": row.category,
            "project": row.project,
            "content": row.content,
            "status": row.status,
            "source_conversation_id": row.source_conversation_id,
            "created_at": row.created_at.isoformat(),
        }

    @staticmethod
    def _auth_user_dict(user: User) -> dict:
        return {
            "id": user.id,
            "email": user.email,
            "password_hash": user.password_hash,
            "display_name": user.display_name,
            "role": user.role,
            "active": user.active,
            "email_verified": user.email_verified,
            "created_at": user.created_at.isoformat(),
            "updated_at": user.updated_at.isoformat(),
        }
