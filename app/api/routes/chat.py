import logging
import sqlite3
import json
import re
from pathlib import Path
from uuid import uuid4
from sqlalchemy.exc import SQLAlchemyError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Conversation,
    ConversationCreate,
    ConversationDetail,
    ConversationRename,
)
from app.services.ai_service import AIServiceError
from app.api.routes.memory import contains_sensitive_content, extract_suggestion
from app.api.routes.auth import get_auth_session, validate_csrf

logger = logging.getLogger(__name__)
router = APIRouter()


def add_document_context(
    repository,
    conversation_id: str,
    message: str,
    settings,
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    chunks = repository.retrieve_document_chunks(
        conversation_id,
        message,
        settings.document_retrieval_chunks,
    )
    if not chunks:
        return history
    document_context = "\n\n".join(
        f"[Source: {chunk['source']}]\n{chunk['text']}"
        for chunk in chunks
    )
    return [
        *history,
        {
            "role": "system",
            "content": (
                "Use only the following uploaded document context to answer document "
                "questions. Cite sources by name and page/sheet/section where available.\n\n"
                f"{document_context}"
            ),
        },
    ]


def add_memory_context(
    repository,
    request: Request,
    message: str,
    settings,
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    auth_session = get_auth_session(request)
    session_id = (
        auth_session["user_id"]
        if auth_session
        else getattr(request.state, "session_id", "test-session")
    )
    memories = repository.retrieve_memories(
        session_id,
        message,
        settings.memory_retrieval_limit,
    )
    if not memories:
        return history
    memory_context = "\n".join(
        f"- [{memory['category']} / {memory['project'] or 'General'}] {memory['content']}"
        for memory in memories
    )
    return [
        *history,
        {
            "role": "system",
            "content": (
                "Use the following relevant saved user memories only when appropriate "
                "and do not reveal that they came from stored memory unless asked.\n"
                f"{memory_context}"
            ),
        },
    ]


def suggest_memory(repository, request: Request, conversation_id: str, message: str) -> None:
    auth_session = get_auth_session(request)
    session_id = (
        auth_session["user_id"]
        if auth_session
        else getattr(request.state, "session_id", "test-session")
    )
    if not repository.is_memory_enabled(session_id):
        return
    suggestion = extract_suggestion(message)
    if suggestion is None:
        return
    category, project, content = suggestion
    repository.add_memory_suggestion(
        session_id,
        category,
        project,
        content,
        conversation_id,
    )


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    ai_service = request.app.state.ai_service
    repository = request.app.state.chat_repository

    conversation_id = payload.conversation_id
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)
    try:
        if conversation_id:
            conversation = repository.get_conversation(
                conversation_id,
                session_id,
                user_id,
            )
            if conversation is None:
                raise HTTPException(status_code=404, detail="Conversation not found.")
        else:
            conversation_id = str(uuid4())
            repository.create_conversation(
                title=payload.message[:46] + ("..." if len(payload.message) > 46 else ""),
                conversation_id=conversation_id,
                session_id=session_id,
                user_id=user_id,
            )

        history = repository.get_context(
            conversation_id,
            request.app.state.settings.chat_context_limit,
        )
        if payload.regenerate and history:
            if history[-1]["role"] == "assistant":
                history = history[:-1]
                if history and history[-1]["role"] == "user":
                    history = history[:-1]
        history = add_document_context(
            repository,
            conversation_id,
            payload.message,
            request.app.state.settings,
            history,
        )
        history = add_memory_context(
            repository,
            request,
            payload.message,
            request.app.state.settings,
            history,
        )

        ai_response = ai_service.generate_response(payload.message, history)
    except AIServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.public_message,
        ) from exc

    try:
        request_id = repository.save_exchange(
            payload.message,
            ai_response.text,
            ai_response.mode,
            conversation_id,
        )
        suggest_memory(repository, request, conversation_id, payload.message)
    except (sqlite3.Error, SQLAlchemyError) as exc:
        logger.exception("Could not save chat exchange")
        raise HTTPException(
            status_code=503,
            detail="Chat storage is unavailable.",
        ) from exc

    return ChatResponse(
        request_id=request_id,
        response=ai_response.text,
        mode=ai_response.mode,
        conversation_id=conversation_id,
    )


@router.post("/chat/stream")
def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    ai_service = request.app.state.ai_service
    repository = request.app.state.chat_repository

    conversation_id = payload.conversation_id
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)
    try:
        if conversation_id:
            if repository.get_conversation(conversation_id, session_id, user_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found.")
        else:
            conversation_id = str(uuid4())
            repository.create_conversation(
                title=payload.message[:46] + ("..." if len(payload.message) > 46 else ""),
                conversation_id=conversation_id,
                session_id=session_id,
                user_id=user_id,
            )

        history = repository.get_context(
            conversation_id,
            request.app.state.settings.chat_context_limit,
        )
        if payload.regenerate and history:
            if history[-1]["role"] == "assistant":
                history = history[:-1]
                if history and history[-1]["role"] == "user":
                    history = history[:-1]
        history = add_document_context(
            repository,
            conversation_id,
            payload.message,
            request.app.state.settings,
            history,
        )
        history = add_memory_context(
            repository,
            request,
            payload.message,
            request.app.state.settings,
            history,
        )

        mode, chunks = ai_service.generate_stream(payload.message, history)
    except AIServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.public_message,
        ) from exc

    async def event_stream():
        full_text = []
        completed = False
        try:
            for chunk in chunks:
                if await request.is_disconnected():
                    return
                if not chunk:
                    continue
                full_text.append(chunk)
                yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n"

            text = "".join(full_text).strip()
            if not text:
                yield f"data: {json.dumps({'type': 'error', 'detail': 'The AI provider returned an empty response.'})}\n\n"
                return

            try:
                request_id = repository.save_exchange(
                    payload.message,
                    text,
                    mode,
                    conversation_id,
                )
                suggest_memory(
                    repository,
                    request,
                    conversation_id,
                    payload.message,
                )
            except (sqlite3.Error, SQLAlchemyError):
                logger.exception("Could not save streamed chat exchange")
                yield f"data: {json.dumps({'type': 'error', 'detail': 'Chat storage is unavailable.'})}\n\n"
                return

            completed = True
            yield f"data: {json.dumps({'type': 'done', 'request_id': request_id, 'response': text, 'mode': mode, 'conversation_id': conversation_id})}\n\n"
        except AIServiceError as exc:
            yield f"data: {json.dumps({'type': 'error', 'detail': exc.public_message})}\n\n"
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                close()
            if not completed and not await request.is_disconnected():
                logger.info("Streaming response ended before completion.")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations", response_model=list[Conversation])
def list_conversations(request: Request) -> list[Conversation]:
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    return [
        Conversation(**conversation)
        for conversation in request.app.state.chat_repository.list_conversations(
            session_id,
            user_id,
        )
    ]


@router.post("/conversations", response_model=Conversation, status_code=201)
def create_conversation(payload: ConversationCreate, request: Request) -> Conversation:
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)
    try:
        conversation = request.app.state.chat_repository.create_conversation(
            payload.title,
            payload.conversation_id,
            session_id,
            user_id,
        )
    except (sqlite3.IntegrityError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=409, detail="Conversation already exists.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (sqlite3.Error, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="Chat storage is unavailable.") from exc
    return Conversation(**conversation)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, request: Request) -> ConversationDetail:
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    conversation = request.app.state.chat_repository.get_conversation(
        conversation_id,
        session_id,
        user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return ConversationDetail(**conversation)


@router.patch("/conversations/{conversation_id}", response_model=Conversation)
def rename_conversation(
    conversation_id: str,
    payload: ConversationRename,
    request: Request,
) -> Conversation:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title must not be empty.")
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)
    conversation = request.app.state.chat_repository.rename_conversation(
        conversation_id,
        title,
        session_id,
        user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return Conversation(**conversation)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, request: Request) -> None:
    repository = request.app.state.chat_repository
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)
    if not repository.delete_conversation(conversation_id, session_id, user_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    safe_conversation = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)
    conversation_dir = (
        Path(request.app.state.settings.upload_root).resolve()
        / safe_conversation
    )
    if conversation_dir.is_dir():
        for deleted_name in repository.pop_deleted_document_names():
            (conversation_dir / deleted_name).unlink(missing_ok=True)
        try:
            conversation_dir.rmdir()
        except OSError:
            pass


@router.delete("/conversations/{conversation_id}/messages", status_code=204)
def clear_conversation(conversation_id: str, request: Request) -> None:
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)
    if not request.app.state.chat_repository.clear_conversation(
        conversation_id,
        session_id,
        user_id,
    ):
        raise HTTPException(status_code=404, detail="Conversation not found.")
