import re

from fastapi import APIRouter, HTTPException, Request

from app.schemas.memory import (
    Memory,
    MemoryCreate,
    MemorySettings,
    MemorySuggestion,
    MemoryUpdate,
)
from app.api.routes.auth import get_auth_session, validate_csrf


router = APIRouter(prefix="/memory", tags=["memory"])

SENSITIVE_PATTERNS = (
    r"\b(api[_ -]?key|password|passwd|secret|token|credential|private[_ -]?key)\b",
    r"\b(sk-[A-Za-z0-9_-]+|gsk_[A-Za-z0-9_-]+)\b",
)


def session_id(request: Request) -> str:
    auth_session = get_auth_session(request)
    if auth_session:
        return auth_session["user_id"]
    return getattr(request.state, "session_id", "test-session")


def contains_sensitive_content(content: str) -> bool:
    return any(re.search(pattern, content, re.IGNORECASE) for pattern in SENSITIVE_PATTERNS)


def validate_authenticated_csrf(request: Request) -> None:
    auth_session = get_auth_session(request)
    if auth_session:
        validate_csrf(request, auth_session)


def extract_suggestion(message: str) -> tuple[str, str, str] | None:
    text = message.strip()
    if len(text) < 8 or len(text) > 500:
        return None
    if contains_sensitive_content(text):
        return None

    patterns = (
        (
            r"(?i)^(?:please )?(?:remember|my long-term instruction(?: is)?|always)\s*[:\-]?\s*(.+)",
            "long_term_instructions",
            "",
        ),
        (
            r"(?i)^my (?:name|job|role|company|location|email|phone) is\s+(.+)",
            "important_facts",
            "",
        ),
        (
            r"(?i)^i (?:prefer|use|like)\s+(.+)",
            "communication_preferences",
            "",
        ),
        (
            r"(?i)^i am working on (?:the )?(.+)",
            "work_and_projects",
            "",
        ),
    )
    for pattern, category, project in patterns:
        match = re.match(pattern, text)
        if match:
            content = match.group(1).strip(" .")
            if content:
                return category, project, content[:500]
    return None


@router.get("", response_model=list[Memory])
def list_memories(request: Request, project: str | None = None) -> list[Memory]:
    rows = request.app.state.chat_repository.list_memories(
        session_id(request),
        project,
    )
    return [Memory(**row) for row in rows]


@router.get("/settings", response_model=MemorySettings)
def get_memory_settings(request: Request) -> MemorySettings:
    return MemorySettings(
        enabled=request.app.state.chat_repository.is_memory_enabled(session_id(request))
    )


@router.patch("/settings", response_model=MemorySettings)
def update_memory_settings(payload: MemorySettings, request: Request) -> MemorySettings:
    validate_authenticated_csrf(request)
    row = request.app.state.chat_repository.set_memory_enabled(
        session_id(request),
        payload.enabled,
    )
    return MemorySettings(**row)


@router.post("", response_model=Memory, status_code=201)
def create_memory(payload: MemoryCreate, request: Request) -> Memory:
    validate_authenticated_csrf(request)
    if contains_sensitive_content(payload.content):
        raise HTTPException(
            status_code=400,
            detail="Passwords, API keys, tokens and credentials cannot be saved.",
        )
    row = request.app.state.chat_repository.create_memory(
        session_id(request),
        payload.category,
        payload.project.strip(),
        payload.content.strip(),
    )
    return Memory(**row)


@router.patch("/{memory_id}", response_model=Memory)
def update_memory(memory_id: str, payload: MemoryUpdate, request: Request) -> Memory:
    validate_authenticated_csrf(request)
    if contains_sensitive_content(payload.content):
        raise HTTPException(
            status_code=400,
            detail="Passwords, API keys, tokens and credentials cannot be saved.",
        )
    row = request.app.state.chat_repository.update_memory(
        session_id(request),
        memory_id,
        payload.category,
        payload.project.strip(),
        payload.content.strip(),
        payload.enabled,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return Memory(**row)


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: str, request: Request) -> None:
    validate_authenticated_csrf(request)
    if not request.app.state.chat_repository.delete_memory(
        session_id(request),
        memory_id,
    ):
        raise HTTPException(status_code=404, detail="Memory not found.")


@router.delete("", status_code=204)
def clear_memories(request: Request) -> None:
    validate_authenticated_csrf(request)
    request.app.state.chat_repository.clear_memories(session_id(request))




@router.get("/suggestions", response_model=list[MemorySuggestion])
def list_suggestions(request: Request) -> list[MemorySuggestion]:
    rows = request.app.state.chat_repository.list_memory_suggestions(session_id(request))
    return [MemorySuggestion(**row) for row in rows]


@router.post("/suggestions/{suggestion_id}/accept", response_model=Memory)
def accept_suggestion(suggestion_id: str, request: Request) -> Memory:
    validate_authenticated_csrf(request)
    row = request.app.state.chat_repository.accept_memory_suggestion(
        session_id(request),
        suggestion_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    return Memory(**row)


@router.post("/suggestions/{suggestion_id}/dismiss", status_code=204)
def dismiss_suggestion(suggestion_id: str, request: Request) -> None:
    validate_authenticated_csrf(request)
    if not request.app.state.chat_repository.dismiss_memory_suggestion(
        session_id(request),
        suggestion_id,
    ):
        raise HTTPException(status_code=404, detail="Suggestion not found.")
