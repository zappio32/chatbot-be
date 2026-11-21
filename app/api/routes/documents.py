import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.exc import SQLAlchemyError

from app.schemas.chat import UploadedDocument
from app.api.routes.auth import get_auth_session, validate_csrf
from app.services.document_service import (
    ALLOWED_EXTENSIONS,
    DocumentProcessingError,
    DocumentProcessingUnavailableError,
    chunk_sections,
    extract_text,
)

router = APIRouter(prefix="/conversations/{conversation_id}/documents", tags=["documents"])


def owned_conversation(conversation_id: str, request: Request) -> bool:
    session_id = getattr(request.state, "session_id", "test-session")
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    conversation = request.app.state.chat_repository.get_conversation(
        conversation_id,
        session_id,
        user_id,
    )
    return conversation is not None


@router.get("", response_model=list[UploadedDocument])
def list_documents(conversation_id: str, request: Request) -> list[UploadedDocument]:
    repository = request.app.state.chat_repository
    if not owned_conversation(conversation_id, request):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return [
        UploadedDocument(**document)
        for document in repository.list_documents(conversation_id)
    ]


@router.post("", response_model=list[UploadedDocument])
async def upload_documents(
    conversation_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> list[UploadedDocument]:
    settings = request.app.state.settings
    repository = request.app.state.chat_repository
    if not owned_conversation(conversation_id, request):
        raise HTTPException(status_code=404, detail="Conversation not found.")

    upload_root = Path(settings.upload_root).resolve()
    conversation_root = upload_root / re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)
    conversation_root.mkdir(parents=True, exist_ok=True)
    uploaded = []
    auth_session = get_auth_session(request)
    user_id = auth_session["user_id"] if auth_session else None
    if auth_session:
        validate_csrf(request, auth_session)

    for file in files:
        filename = Path(file.filename or "upload").name
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=415, detail=f"{filename}: unsupported file type.")

        content = await file.read()
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{filename}: file exceeds the configured size limit.",
            )

        try:
            suffix = extract_suffix = None
            sections = extract_text(
                filename,
                content,
                ocr_enabled=settings.ocr_enabled,
                tesseract_cmd=settings.tesseract_cmd,
                poppler_path=settings.poppler_path,
                ocr_language=settings.ocr_language,
                ocr_timeout_seconds=settings.ocr_timeout_seconds,
                ocr_max_image_pixels=settings.ocr_max_image_pixels,
                ocr_max_pdf_pages=settings.ocr_max_pdf_pages,
            )
            for section in sections:
                if section["source"] in {"Image metadata", "Image OCR"}:
                    status = "limited"
                    if section["source"] == "Image OCR":
                        status = "ready"
                    break
            else:
                status = "ready"
            chunks = chunk_sections(sections, settings.document_chunk_chars)
            if not chunks and status != "limited":
                status = "ready"
        except DocumentProcessingError as exc:
            if isinstance(exc, DocumentProcessingUnavailableError):
                sections = [{
                    "text": str(exc),
                    "source": "Processing limitation",
                }]
                status = "limited"
                chunks = chunk_sections(sections, settings.document_chunk_chars)
            else:
                raise HTTPException(status_code=422, detail=f"{filename}: {exc}") from exc

        document_id = str(uuid4())
        stored_name = f"{document_id}{extension}"
        stored_path = conversation_root / stored_name
        stored_path.write_bytes(content)

        source_summary = ", ".join(
            dict.fromkeys(section["source"] for section in sections)
        ) or filename
        try:
            document = repository.add_document(
                document_id=document_id,
                conversation_id=conversation_id,
                original_name=filename,
                stored_name=stored_name,
                extension=extension,
                size_bytes=len(content),
                status=status,
                source_summary=source_summary,
                chunks=chunks,
                user_id=user_id,
            )
        except (sqlite3.Error, SQLAlchemyError) as exc:
            stored_path.unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail="Chat storage is unavailable.") from exc
        uploaded.append(UploadedDocument(**document))

    return uploaded


@router.get("/{document_id}", response_model=UploadedDocument)
def get_document(conversation_id: str, document_id: str, request: Request) -> UploadedDocument:
    repository = request.app.state.chat_repository
    if not owned_conversation(conversation_id, request):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    document = repository.get_document(document_id, conversation_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return UploadedDocument(**document)


@router.get("/{document_id}/download")
def download_document(conversation_id: str, document_id: str, request: Request) -> FileResponse:
    repository = request.app.state.chat_repository
    if not owned_conversation(conversation_id, request):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    document = repository.get_document(document_id, conversation_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    stored_name = repository.get_document_storage_name(document_id, conversation_id)
    if not stored_name:
        raise HTTPException(status_code=404, detail="Document file not found.")
    safe_conversation = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)
    path = (Path(request.app.state.settings.upload_root).resolve() / safe_conversation / stored_name).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file not found.")
    return FileResponse(path, filename=document["original_name"])


@router.delete("/{document_id}", status_code=204)
def delete_document(conversation_id: str, document_id: str, request: Request) -> None:
    repository = request.app.state.chat_repository
    auth_session = get_auth_session(request)
    if auth_session:
        validate_csrf(request, auth_session)
    if not owned_conversation(conversation_id, request):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    stored_name = repository.delete_document(document_id, conversation_id)
    if stored_name is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    safe_conversation = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)
    path = (
        Path(request.app.state.settings.upload_root).resolve()
        / safe_conversation
        / stored_name
    ).resolve()
    path.unlink(missing_ok=True)
