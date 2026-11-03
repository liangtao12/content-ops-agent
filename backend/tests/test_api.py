from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import app.server as server
from app import config
from app.graph import build_graph


ADMIN_PASSWORD = "AdminPass123"


def _raw_client(monkeypatch):
    monkeypatch.setattr(server, "graph", build_graph(MemorySaver()))
    return TestClient(server.app)


def _client(monkeypatch):
    client = _raw_client(monkeypatch)
    response = client.post("/api/auth/register", json={
        "username": "admin",
        "display_name": "系统管理员",
        "password": ADMIN_PASSWORD,
    })
    assert response.status_code == 200
    token = response.json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _create_user(client, username, role):
    created = client.post("/api/admin/users", json={
        "username": username,
        "display_name": f"{role} 用户",
        "password": "UserPass123",
        "role": role,
    })
    assert created.status_code == 200
    login = client.post("/api/auth/login", json={
        "username": username,
        "password": "UserPass123",
    })
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def test_auth_bootstrap_login_logout(monkeypatch):
    client = _client(monkeypatch)
    bootstrap = client.get("/api/auth/bootstrap").json()
    assert bootstrap["user_count"] == 1
    assert bootstrap["first_user"] is False

    wrong = client.post("/api/auth/login", json={
        "username": "admin",
        "password": "WrongPass123",
    })
    assert wrong.status_code == 401

    login = client.post("/api/auth/login", json={
        "username": "admin",
        "password": ADMIN_PASSWORD,
    })
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/session", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/session", headers=headers).status_code == 401


def test_first_user_is_admin_and_invite_controls_registration(monkeypatch):
    client = _raw_client(monkeypatch)
    assert client.get("/api/auth/bootstrap").json()["first_user"] is True
    first = client.post("/api/auth/register", json={
        "username": "first-admin",
        "display_name": "首位管理员",
        "password": ADMIN_PASSWORD,
    })
    assert first.status_code == 200
    assert first.json()["user"]["role"] == "admin"

    monkeypatch.setattr(config, "REGISTRATION_CODE", "invite-123")
    denied = client.post("/api/auth/register", json={
        "username": "second-user",
        "display_name": "第二用户",
        "password": "UserPass123",
    })
    assert denied.status_code == 403
    allowed = client.post("/api/auth/register", json={
        "username": "second-user",
        "display_name": "第二用户",
        "password": "UserPass123",
        "registration_code": "invite-123",
    })
    assert allowed.status_code == 200
    assert allowed.json()["user"]["role"] == "operator"


def test_api_human_loop_and_persistence_endpoints(monkeypatch):
    client = _client(monkeypatch)
    reviewer = _create_user(client, "reviewer1", "reviewer")
    response = client.post("/api/run", json={"objective": "秋冬敏感肌护理"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_human"
    assert body["job"]["status"] == "awaiting_human"
    assert body["job"]["research_evidence"]
    assert "brand_kit" in body["job"]["account"]
    thread_id = body["thread_id"]

    jobs = client.get("/api/jobs").json()["items"]
    assert jobs and jobs[0]["id"] == thread_id
    assert client.get("/api/overview").json()["awaiting_human"] == 1
    assert client.get(f"/api/status/{thread_id}").json()["next"] == ["human"]

    approved = client.post(
        f"/api/human/{thread_id}",
        headers=reviewer,
        json={"action": "approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert approved.json()["schedule"]["approval_token"]
    audit = client.get(f"/api/audit/{thread_id}").json()["items"]
    actors = {event["actor"] for event in audit}
    assert any("内容审核员" in actor for actor in actors)


def test_edit_requires_content_and_override_reason(monkeypatch):
    client = _client(monkeypatch)
    reviewer = _create_user(client, "reviewer2", "reviewer")
    body = client.post("/api/run", json={"objective": "通勤穿搭选题"}).json()
    missing = client.post(
        f"/api/human/{body['thread_id']}",
        headers=reviewer,
        json={"action": "edited"},
    )
    assert missing.status_code == 422

    risky = client.post(
        "/api/run",
        json={"objective": "[注入极限词] 全网第一根治"},
    ).json()
    no_reason = client.post(
        f"/api/human/{risky['thread_id']}",
        headers=reviewer,
        json={"action": "approved"},
    )
    assert no_reason.status_code == 400


def test_api_rejects_unsupported_platform(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/run", json={
        "objective": "测试账号",
        "account": {
            "name": "测试账号",
            "platform": "微博",
            "category": "美妆",
            "persona": "测试",
        },
    })
    assert response.status_code == 422


def test_static_demo_and_health_are_available(monkeypatch):
    client = _client(monkeypatch)
    page = client.get("/")
    assert page.status_code == 200
    assert "ContentOps Agent" in page.text
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["auth_mode"] == "session"
    assert health.headers["x-content-type-options"] == "nosniff"


def test_role_permissions_split_create_and_approval(monkeypatch):
    client = _client(monkeypatch)
    operator = _create_user(client, "operator1", "operator")
    reviewer = _create_user(client, "reviewer3", "reviewer")
    viewer = _create_user(client, "viewer1", "viewer")

    assert client.post(
        "/api/run",
        headers=reviewer,
        json={"objective": "审核员不能创建任务"},
    ).status_code == 403
    assert client.post(
        "/api/run",
        headers=viewer,
        json={"objective": "观察者不能创建任务"},
    ).status_code == 403

    created = client.post(
        "/api/run",
        headers=operator,
        json={"objective": "运营创建任务"},
    )
    assert created.status_code == 200
    job_id = created.json()["thread_id"]

    assert client.post(
        f"/api/human/{job_id}",
        headers=operator,
        json={"action": "approved"},
    ).status_code == 403
    assert client.post(
        f"/api/human/{job_id}",
        headers=reviewer,
        json={"action": "approved"},
    ).status_code == 200

    session = client.get("/api/session", headers=viewer).json()
    assert session["actor"]["role"] == "viewer"
    assert session["actor"]["permissions"] == ["content.view"]


def test_admin_manages_members_and_sessions(monkeypatch):
    client = _client(monkeypatch)
    operator_headers = _create_user(client, "operator2", "operator")
    session_user = client.get("/api/session", headers=operator_headers).json()
    operator_id = session_user["actor"]["id"]

    users = client.get("/api/admin/users").json()
    assert users["summary"]["total"] == 2
    assert {item["role"] for item in users["items"]} == {"admin", "operator"}

    updated = client.patch(f"/api/admin/users/{operator_id}", json={
        "role": "reviewer",
        "display_name": "新审核员",
    })
    assert updated.status_code == 200
    assert updated.json()["user"]["role"] == "reviewer"

    disabled = client.patch(f"/api/admin/users/{operator_id}", json={
        "active": False,
    })
    assert disabled.status_code == 200
    assert client.get("/api/session", headers=operator_headers).status_code == 401

    client.patch(f"/api/admin/users/{operator_id}", json={"active": True})
    reset = client.post(
        f"/api/admin/users/{operator_id}/reset-password",
        json={"password": "NewPass123"},
    )
    assert reset.status_code == 200
    old_login = client.post("/api/auth/login", json={
        "username": "operator2",
        "password": "UserPass123",
    })
    assert old_login.status_code == 401
    new_login = client.post("/api/auth/login", json={
        "username": "operator2",
        "password": "NewPass123",
    })
    assert new_login.status_code == 200


def test_service_api_key_still_works_for_automation(monkeypatch):
    monkeypatch.setattr(config, "CONTENTOPS_API_KEY", "service-key")
    client = _raw_client(monkeypatch)
    denied = client.post("/api/run", json={"objective": "秋冬敏感肌护理"})
    assert denied.status_code == 401
    allowed = client.post(
        "/api/run",
        headers={"Authorization": "Bearer service-key"},
        json={"objective": "秋冬敏感肌护理"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["job"]["operator"] == "自动化服务"
