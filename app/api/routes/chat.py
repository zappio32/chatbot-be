import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.ai_service import AIServiceError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    ai_service = request.app.state.ai_service
    repository = request.app.state.chat_repository

    try:
        ai_response = ai_service.generate_response(payload.message)
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
        )
    except sqlite3.Error as exc:
        logger.exception("Could not save chat exchange")
        raise HTTPException(
            status_code=503,
            detail="Chat storage is unavailable.",
        ) from exc

    return ChatResponse(
        request_id=request_id,
        response=ai_response.text,
        mode=ai_response.mode,
    )
