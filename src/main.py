import logging
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from src.config import settings
from src.models import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    TaskRecord,
    TaskRunRequest,
)
from src.agent import agent_service
from src.orchestrator import orchestrator

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cloud-agy-orchestrator")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verifies API Key if configured in settings; allows unauthenticated access if blank."""
    if not settings.orchestrator_api_key:
        return True
    if api_key != settings.orchestrator_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Cloud AGY Orchestrator starting up...")
    logger.info(f"Environment: {settings.environment} | Port: {settings.port}")
    logger.info(f"Native Antigravity SDK Available: {agent_service.is_sdk_available}")
    yield
    logger.info("Cloud AGY Orchestrator shutting down...")


app = FastAPI(
    title="Cloud Antigravity (AGY) Orchestrator",
    version="0.1.0",
    description="Scalable cloud orchestrator and agent service for Google Antigravity (AGY).",
    lifespan=lifespan,
)

# CORS Middleware
origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["General"])
async def root():
    return {
        "service": "cloud-agy-orchestrator",
        "status": "online",
        "documentation": "/docs",
        "version": "0.1.0",
    }


@app.get("/health", response_model=HealthResponse, tags=["General"])
async def health():
    return HealthResponse(
        status="ok",
        version="0.1.0",
        environment=settings.environment,
        agent_enabled=settings.agy_agent_enabled,
    )


@app.post(
    "/v1/agents/chat",
    response_model=ChatResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Agent Service"],
)
async def chat(request: ChatRequest):
    """Executes a direct agent chat request via the Antigravity Agent Service."""
    if not settings.agy_agent_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Antigravity agent service is currently disabled in configuration",
        )
    return await agent_service.execute_chat(request)


@app.post(
    "/v1/tasks/run",
    response_model=TaskRecord,
    dependencies=[Depends(verify_api_key)],
    tags=["Orchestration"],
)
async def run_task(request: TaskRunRequest, background_tasks: BackgroundTasks):
    """Schedules an asynchronous, long-running agent task in the orchestrator."""
    if not settings.agy_agent_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Antigravity agent service is currently disabled in configuration",
        )

    task = orchestrator.create_task(request)
    background_tasks.add_task(orchestrator.run_task_async, task.task_id, request)
    return task


@app.get(
    "/v1/tasks/{task_id}",
    response_model=TaskRecord,
    dependencies=[Depends(verify_api_key)],
    tags=["Orchestration"],
)
async def get_task(task_id: str):
    """Fetches details, execution logs, and current status of an orchestrated task."""
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID '{task_id}' not found",
        )
    return task


@app.get(
    "/v1/tasks",
    response_model=list[TaskRecord],
    dependencies=[Depends(verify_api_key)],
    tags=["Orchestration"],
)
async def list_tasks(limit: int = 50):
    """Returns recently orchestrated agent tasks."""
    return orchestrator.list_tasks(limit=limit)
