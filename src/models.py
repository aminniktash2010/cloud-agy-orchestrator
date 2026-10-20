from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str
    agent_enabled: bool


class ChatMessage(BaseModel):
    role: str = Field(..., description="Role: 'user', 'assistant', or 'system'")
    content: str = Field(..., description="Message text content")


class ChatRequest(BaseModel):
    prompt: str = Field(..., description="The user prompt or instruction to execute")
    history: List[ChatMessage] = Field(default_factory=list, description="Optional conversation context")
    system_prompt: Optional[str] = Field(
        default=None, description="Custom system instructions for this session"
    )
    read_only: Optional[bool] = Field(
        default=None, description="Enforce read-only tool execution"
    )
    model: Optional[str] = Field(default=None, description="LLM model override")


class ToolExecutionRecord(BaseModel):
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    response: str
    thoughts: List[str] = Field(default_factory=list)
    tool_calls: List[ToolExecutionRecord] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskRunRequest(BaseModel):
    title: str = Field(..., description="Short task title")
    goal: str = Field(..., description="The specific goal/task to execute")
    workspace_path: Optional[str] = Field(
        default=None, description="Local or container workspace directory"
    )
    timeout_seconds: Optional[int] = Field(default=None, description="Max runtime in seconds")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    task_id: str
    title: str
    goal: str
    status: TaskStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
