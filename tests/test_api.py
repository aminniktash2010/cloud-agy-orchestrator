import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "cloud-agy-orchestrator"
    assert data["status"] == "online"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_chat():
    payload = {"prompt": "List all configured tools."}
    response = client.post("/v1/agents/chat", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "session_id" in data
    assert "response" in data
    assert len(data["thoughts"]) > 0


def test_task_orchestration_lifecycle():
    task_payload = {
        "title": "Smoke Test Task",
        "goal": "Verify asynchronous cloud orchestrator worker",
        "metadata": {"test_env": "ci"},
    }
    # Create task
    response = client.post("/v1/tasks/run", json=task_payload)
    assert response.status_code == 200
    task_data = response.json()
    task_id = task_data["task_id"]
    assert task_data["title"] == "Smoke Test Task"

    # Query task
    status_resp = client.get(f"/v1/tasks/{task_id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["task_id"] == task_id

    # List tasks
    list_resp = client.get("/v1/tasks")
    assert list_resp.status_code == 200
    assert any(t["task_id"] == task_id for t in list_resp.json())
