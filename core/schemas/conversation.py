from datetime import datetime

from pydantic import BaseModel, Field


class WindowCreate(BaseModel):
    external_source_id: str = Field(default="", description="Identifier of the external program sending messages.")


class WindowOut(BaseModel):
    id: str
    external_source_id: str
    status: str
    message_count: int
    total_char_count: int
    created_at: datetime
    first_message_at: datetime | None
    last_message_at: datetime | None

    model_config = {"from_attributes": True}


class MessageAdd(BaseModel):
    role: str = Field(description="Message role: user, assistant, or system.")
    content: str = Field(description="Message text content.")


class MessageOut(BaseModel):
    id: str
    window_id: str
    role: str
    content: str
    char_count: int
    sequence_no: int
    processed: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AddMessageResponse(BaseModel):
    message: MessageOut
    flush_triggered: bool = Field(description="True if a flush was triggered by this message.")


class TaskOut(BaseModel):
    id: str
    agent_type: str
    instruction: str
    context_json: str | None
    status: str
    batch_id: str | None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None

    model_config = {"from_attributes": True}
