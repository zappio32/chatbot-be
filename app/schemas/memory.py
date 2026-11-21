from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MemoryCategory = Literal[
    "user_preferences",
    "important_facts",
    "work_and_projects",
    "frequently_used",
    "communication_preferences",
    "long_term_instructions",
]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: MemoryCategory
    project: str = Field(default="", max_length=120)
    content: str = Field(min_length=1, max_length=1000)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: MemoryCategory
    project: str = Field(default="", max_length=120)
    content: str = Field(min_length=1, max_length=1000)
    enabled: bool = True


class Memory(BaseModel):
    id: str
    category: MemoryCategory
    project: str
    content: str
    enabled: bool
    source_conversation_id: str | None
    created_at: str
    updated_at: str


class MemorySettings(BaseModel):
    enabled: bool


class MemorySuggestion(BaseModel):
    id: str
    category: MemoryCategory
    project: str
    content: str
    status: Literal["pending", "accepted", "dismissed"]
    source_conversation_id: str | None
    created_at: str
