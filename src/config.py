import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application runtime settings loaded from environment or .env file."""

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    environment: str = "production"

    orchestrator_api_key: str = ""
    allowed_origins: str = "*"

    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    gcs_artifacts_bucket: str = ""

    agy_agent_enabled: bool = True
    agy_read_only_mode: bool = False
    agy_default_system_prompt: str = (
        "You are an autonomous cloud agent orchestrator powered by Antigravity."
    )
    agy_default_model: str = "gemini-2.5-pro"
    agy_task_timeout_seconds: int = 600

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
