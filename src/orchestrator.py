import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from src.config import settings
from src.models import ChatRequest, TaskRecord, TaskRunRequest, TaskStatus
from src.agent import agent_service

logger = logging.getLogger(__name__)


class TaskOrchestrator:
    """Manages background agent workflows, job statuses, and execution history."""

    def __init__(self):
        self._tasks: Dict[str, TaskRecord] = {}

    def create_task(self, req: TaskRunRequest) -> TaskRecord:
        task_id = str(uuid.uuid4())
        record = TaskRecord(
            task_id=task_id,
            title=req.title,
            goal=req.goal,
            status=TaskStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            metadata=req.metadata,
        )
        self._tasks[task_id] = record
        return record

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> List[TaskRecord]:
        return list(self._tasks.values())[-limit:]

    async def run_task_async(self, task_id: str, req: TaskRunRequest) -> None:
        """Asynchronously executes a long-running agent task in the background."""
        task = self._tasks.get(task_id)
        if not task:
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        logger.info(f"Started background orchestrator task {task_id}: '{task.title}'")

        try:
            chat_req = ChatRequest(
                prompt=f"Execute task '{task.title}': {task.goal}",
                system_prompt=f"You are executing automated orchestrator task ID {task_id}.",
                read_only=settings.agy_read_only_mode,
            )

            timeout = req.timeout_seconds or settings.agy_task_timeout_seconds
            chat_result = await asyncio.wait_for(
                agent_service.execute_chat(chat_req),
                timeout=timeout,
            )

            task.status = TaskStatus.COMPLETED
            task.result = chat_result.response
            task.finished_at = datetime.now(timezone.utc)
            logger.info(f"Task {task_id} completed successfully.")
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.error = f"Task timed out after {req.timeout_seconds or settings.agy_task_timeout_seconds}s"
            task.finished_at = datetime.now(timezone.utc)
            logger.error(f"Task {task_id} timed out.")
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.finished_at = datetime.now(timezone.utc)
            logger.error(f"Task {task_id} failed with error: {e}", exc_info=True)


orchestrator = TaskOrchestrator()
