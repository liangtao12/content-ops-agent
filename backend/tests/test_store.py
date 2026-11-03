from app.store import WorkspaceRepository


def test_workspace_repository_persists_jobs_and_events(tmp_path):
    path = tmp_path / "workspace.sqlite"
    repository = WorkspaceRepository(path)
    state = {
        "run_id": "job-001",
        "account": {"name": "测试账号", "platform": "小红书"},
        "objective": "测试任务",
        "status": "awaiting_human",
        "selected_topic": {"title": "候选选题"},
        "draft": {"title": "初稿", "body": "正文", "tags": ["测试"]},
        "review_result": {"passed": True, "violations": []},
        "errors": [],
    }
    repository.create_job(state, "运营 A")
    repository.sync_state(state, "运营 A")
    repository.add_event(
        "job-001", "approval_required", "ContentOps", "等待人工审批"
    )

    reopened = WorkspaceRepository(path)
    job = reopened.get_job("job-001")
    assert job["status"] == "awaiting_human"
    assert job["draft"]["title"] == "初稿"
    assert reopened.overview()["awaiting_human"] == 1
    assert reopened.list_events("job-001")[0]["message"] == "等待人工审批"


def test_workspace_repository_returns_empty_dashboard(tmp_path):
    repository = WorkspaceRepository(tmp_path / "empty.sqlite")
    overview = repository.overview()
    assert overview["total_jobs"] == 0
    assert overview["completion_rate"] == 0
    assert overview["recent_jobs"] == []
