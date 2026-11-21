from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=128)
    regenerate: bool = False

    @field_validator("message", mode="before")
    @classmethod
    def trim_message(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ChatResponse(BaseModel):
    request_id: int
    response: str
    mode: Literal["demo", "live"]
    conversation_id: str | None = None


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="New conversation", min_length=1, max_length=120)
    conversation_id: str | None = Field(default=None, max_length=128)


class ConversationRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)


class Conversation(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class ConversationDetail(Conversation):
    messages: list["ChatMessage"]


class ChatMessage(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    mode: Literal["demo", "live"] | None = None
    created_at: str


class UploadedDocument(BaseModel):
    id: str
    conversation_id: str
    original_name: str
    extension: str
    size_bytes: int
    status: Literal["ready", "error", "limited"]
    source_summary: str
    created_at: str
