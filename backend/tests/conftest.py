import pytest

from app import config
from app.auth import AuthStore
from app.store import WorkspaceRepository


@pytest.fixture(autouse=True)
def isolated_runtime_files(tmp_path, monkeypatch):
    """所有测试都写临时目录，禁止污染真实排期与 trace。"""
    monkeypatch.setattr(config, "SCHEDULE_FILE", tmp_path / "schedule.json")
    monkeypatch.setattr(config, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(config, "APPROVAL_SECRET", "test-only-approval-secret")
    monkeypatch.setattr(config, "APPROVAL_SECRET_IS_DEV", False)
    monkeypatch.setattr(config, "CONTENTOPS_API_KEY", "")
    monkeypatch.setattr(config, "REGISTRATION_CODE", "")
    monkeypatch.setattr(config, "AUTH_SESSION_HOURS", 12)
    workspace_db = tmp_path / "workspace.sqlite"
    monkeypatch.setattr(config, "WORKSPACE_DB", workspace_db)

    import app.server as server

    monkeypatch.setattr(server, "repository", WorkspaceRepository(workspace_db))
    monkeypatch.setattr(server, "auth_store", AuthStore(workspace_db))
