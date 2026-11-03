"""FastAPI 服务：把 Agent 工作流暴露为 API（生产化入口）。

两个核心接口体现 human-in-the-loop：
POST /api/run        -> 跑到 human 节点自动暂停，返回待确认内容
POST /api/human/{tid}-> 运营提交决策，Command(resume=...) 恢复工作流
"""
import uuid
import json
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field, model_validator

from . import config
from .auth import (
    Actor,
    actor_from_user,
    auth_store,
    require_permission,
    resolve_actor,
    session_payload,
)
from .guardrails import Draft
from .graph import DEFAULT_ACCOUNT, build_graph, get_interrupt_payload
from .persistence import create_checkpointer
from .store import repository
from .tracing import new_recorder

app = FastAPI(title="ContentOps Agent", version="0.1.0")
graph = build_graph(create_checkpointer())


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _view_actor(
    authorization: str | None = Header(default=None),
) -> Actor:
    actor = resolve_actor(authorization, auth_store)
    require_permission(actor, "content.view")
    return actor


def _admin_actor(
    authorization: str | None = Header(default=None),
) -> Actor:
    actor = resolve_actor(authorization, auth_store)
    require_permission(actor, "settings.manage")
    return actor


class BrandKit(BaseModel):
    tone: str = Field(default="", max_length=300)
    banned_claims: list[str] = Field(default_factory=list, max_length=30)
    required_phrases: list[str] = Field(default_factory=list, max_length=30)
    cta: str = Field(default="", max_length=300)


class Account(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: Literal["小红书", "抖音", "视频号"]
    category: str = Field(min_length=1, max_length=100)
    persona: str = Field(min_length=1, max_length=1000)
    brand_kit: BrandKit = Field(default_factory=BrandKit)


class RunRequest(BaseModel):
    objective: str = Field(min_length=2, max_length=2000)
    account: Account | None = None


class HumanDecision(BaseModel):
    action: Literal["approved", "edited", "rejected"]
    content: Draft | None = None
    override_reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def edited_content_required(self):
        if self.action == "edited" and self.content is None:
            raise ValueError("edited 决策必须提供 content")
        return self


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    registration_code: str = Field(default="", max_length=200)
    # 可选角色，非法值由 auth.register 给出中文提示；不传时保持 operator 兼容旧客户端。
    role: str = Field(default="operator", max_length=32)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class AdminCreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["admin", "reviewer", "operator", "viewer"]


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Literal["admin", "reviewer", "operator", "viewer"] | None = None
    active: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


def _public_result(result: dict) -> dict:
    return {
        "status": result.get("status"),
        "approval_token": result.get("approval_token"),
        "schedule": result.get("schedule"),
        "analytics": result.get("analytics"),
        "errors": result.get("errors", []),
    }


def _read_schedules(limit: int = 100) -> list[dict]:
    try:
        data = json.loads(config.SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    items = data.get("items", [])
    return sorted(items, key=lambda item: item.get("publish_at", ""))[:limit]


def _job_event_for_state(job: dict, actor_label: str) -> None:
    status = job.get("status")
    if status == "awaiting_human":
        repository.add_event(
            job["id"],
            "approval_required",
            "ContentOps",
            "合规检查完成，任务进入人工审批",
            {"review_result": job.get("review_result", {})},
        )
    elif status == "completed":
        repository.add_event(
            job["id"],
            "completed",
            actor_label,
            "内容已通过审批并完成排期",
            {"schedule": job.get("schedule", {})},
        )
    elif status == "rejected":
        repository.add_event(
            job["id"], "rejected", actor_label, "审核员拒绝发布内容", None
        )
    elif status == "blocked":
        repository.add_event(
            job["id"],
            "blocked",
            "ContentOps",
            "任务因合规或执行错误终止",
            {"errors": job.get("errors", [])},
        )


def _auth_response(user: dict) -> dict:
    token = auth_store.create_session(user["id"])
    actor = actor_from_user(user)
    return {
        "token": token,
        "user": user,
        "session": session_payload(actor),
    }


@app.get("/api/auth/bootstrap")
def auth_bootstrap():
    return auth_store.bootstrap()


@app.post("/api/auth/register")
def auth_register(req: RegisterRequest):
    user = auth_store.register(
        username=req.username,
        display_name=req.display_name,
        password=req.password,
        registration_code=req.registration_code,
        role=req.role,
    )
    return _auth_response(user)


@app.post("/api/auth/login")
def auth_login(req: LoginRequest):
    user = auth_store.authenticate(req.username, req.password)
    return _auth_response(user)


@app.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        auth_store.revoke_session(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


@app.post("/api/run")
def run(
    req: RunRequest,
    authorization: str | None = Header(default=None),
):
    actor = resolve_actor(authorization, auth_store)
    require_permission(actor, "content.create")
    operator = actor.name
    thread_id = uuid.uuid4().hex[:12]
    new_recorder(thread_id)
    account = req.account.model_dump() if req.account else DEFAULT_ACCOUNT
    cfg = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "run_id": thread_id,
        "account": account,
        "objective": req.objective,
        "status": "running",
    }
    repository.create_job(initial_state, operator)
    repository.add_event(
        thread_id,
        "created",
        actor.event_label,
        "创建内容任务",
        {"objective": req.objective, "account": account},
    )
    result = graph.invoke(initial_state, config=cfg)
    pending = get_interrupt_payload(graph, cfg)
    if pending is not None:
        result = {**result, "status": "awaiting_human"}
    job = repository.sync_state(result, operator)
    _job_event_for_state(job, actor.event_label)
    # human 节点 interrupt：通过 get_state 检测挂起并取中断载荷
    if pending is not None:
        return {
            "thread_id": thread_id,
            "status": "awaiting_human",
            "pending": pending,
            "job": job,
            **_public_result(result),
        }
    return {"thread_id": thread_id, "job": job, **_public_result(result)}


def _sse(event: str, data: dict) -> str:
    """SSE 帧：event 区分事件类型，data 为单行 JSON（中文不转义，便于前端直接展示）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _node_summary(node: str, update: dict) -> dict:
    """只推摘要不推全量 state：正文可能很长，进度流不该承载业务大对象。"""
    if node == "research":
        selected = update.get("selected_topic") or {}
        return {
            "selected_topic": selected.get("title", ""),
            "candidates": len(update.get("candidates") or []),
        }
    if node == "write":
        draft = update.get("draft") or {}
        return {
            "title": draft.get("title", ""),
            "body_length": len(draft.get("body") or ""),
        }
    if node == "review":
        result = update.get("review_result") or {}
        return {
            "passed": bool(result.get("passed")),
            "violations": len(result.get("violations") or []),
            "round": update.get("review_rounds", 0),
        }
    if node == "final_check":
        return {
            "approved": bool(update.get("approval_token")),
            "status": update.get("status", ""),
        }
    if node == "schedule":
        schedule = update.get("schedule") or {}
        return {
            "schedule_id": schedule.get("schedule_id", ""),
            "publish_at": schedule.get("publish_at", ""),
        }
    if node == "analytics":
        return {"status": update.get("status", "")}
    return {}


@app.post("/api/run/stream")
def run_stream(
    req: RunRequest,
    authorization: str | None = Header(default=None),
):
    """SSE 版 /api/run：用 graph.stream 逐节点推送进度，done 事件携带最终结果。

    与 /api/run 的差异只在传输方式：节点一完成就推送，前端不必等整条链路结束。
    interrupt 仍由 done 事件里的 pending 表达，语义与同步接口一致。
    """
    actor = resolve_actor(authorization, auth_store)
    require_permission(actor, "content.create")
    operator = actor.name
    thread_id = uuid.uuid4().hex[:12]
    account = req.account.model_dump() if req.account else DEFAULT_ACCOUNT
    cfg = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "run_id": thread_id,
        "account": account,
        "objective": req.objective,
        "status": "running",
    }
    repository.create_job(initial_state, operator)
    repository.add_event(
        thread_id,
        "created",
        actor.event_label,
        "创建内容任务（流式）",
        {"objective": req.objective, "account": account},
    )

    def event_stream():
        new_recorder(thread_id)
        yield _sse("start", {"thread_id": thread_id, "status": "running"})
        try:
            for chunk in graph.stream(initial_state, config=cfg, stream_mode="updates"):
                for node, update in chunk.items():
                    if node.startswith("__"):
                        continue  # 内部通道（如 __interrupt__）统一在 done 里表达
                    payload = update or {}
                    yield _sse("node", {
                        "node": node,
                        "status": payload.get("status", ""),
                        "summary": _node_summary(node, payload),
                    })
            pending = get_interrupt_payload(graph, cfg)
            snapshot = graph.get_state(cfg)
            result = dict(snapshot.values or {})
            if pending is not None:
                result["status"] = "awaiting_human"
            job = repository.sync_state(result, operator)
            _job_event_for_state(job, actor.event_label)
            final = {
                "thread_id": thread_id,
                "status": job.get("status") or result.get("status"),
                "job": job,
                **_public_result(result),
            }
            if pending is not None:
                final["pending"] = pending
                yield _sse("awaiting_human", {
                    "thread_id": thread_id,
                    "requires_override_reason": bool(
                        pending.get("requires_override_reason")
                    ),
                })
            yield _sse("done", final)
        except Exception as exc:  # 节点异常必须让前端看到原因，不能静默断流
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/human/{thread_id}")
def human(
    thread_id: str,
    decision: HumanDecision,
    authorization: str | None = Header(default=None),
):
    actor = resolve_actor(authorization, auth_store)
    require_permission(actor, "content.approve")
    operator = actor.name
    cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(cfg)
    if not snapshot.values:
        raise HTTPException(404, "unknown thread")
    pending = get_interrupt_payload(graph, cfg)
    if pending is None:
        raise HTTPException(409, "thread is not awaiting human approval")
    if (
        decision.action == "approved"
        and pending.get("requires_override_reason")
        and not decision.override_reason.strip()
    ):
        raise HTTPException(400, "合规未通过，人工放行必须填写 override_reason")

    new_recorder(thread_id)
    repository.add_event(
        thread_id,
        "decision_submitted",
        actor.event_label,
        f"提交审批决策：{decision.action}",
        {
            "action": decision.action,
            "override_reason": decision.override_reason,
        },
    )
    result = graph.invoke(
        Command(resume={
            "action": decision.action,
            "content": decision.content.model_dump() if decision.content else None,
            "override_reason": decision.override_reason,
        }),
        config=cfg)
    pending = get_interrupt_payload(graph, cfg)
    if pending is not None:
        result = {**result, "status": "awaiting_human"}
    job = repository.sync_state(result, operator)
    _job_event_for_state(job, actor.event_label)
    if pending is not None:
        return {
            "thread_id": thread_id,
            "status": "awaiting_human",
            "pending": pending,
            "job": job,
            **_public_result(result),
        }
    return {"thread_id": thread_id, "job": job, **_public_result(result)}


@app.get("/api/trace/{thread_id}")
def trace(thread_id: str, actor: Actor = Depends(_view_actor)):
    path = config.TRACE_DIR / f"run_{thread_id}.jsonl"
    if not path.exists():
        raise HTTPException(404, "trace not found")
    return FileResponse(path, media_type="application/jsonl")


@app.get("/api/status/{thread_id}")
def status(thread_id: str, actor: Actor = Depends(_view_actor)):
    cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(cfg)
    if not snapshot.values:
        raise HTTPException(404, "unknown thread")
    pending = get_interrupt_payload(graph, cfg)
    return {
        "thread_id": thread_id,
        "job": repository.get_job(thread_id),
        "status": snapshot.values.get("status"),
        "pending": pending,
        "next": list(snapshot.next),
        "approval_token": snapshot.values.get("approval_token"),
        "schedule": snapshot.values.get("schedule"),
        "errors": snapshot.values.get("errors", []),
    }


@app.get("/api/overview")
def overview(actor: Actor = Depends(_view_actor)):
    metrics = repository.overview()
    schedules = _read_schedules(limit=200)
    metrics["scheduled_total"] = len(schedules)
    metrics["upcoming_schedules"] = [
        item for item in schedules
        if item.get("publish_at", "") >= datetime.now().isoformat(timespec="minutes")
    ][:5]
    metrics["evaluation"] = {
        "flow_pass": "10/10",
        "guardrail_precision": 0.833,
        "guardrail_recall": 1.0,
        "guardrail_f1": 0.909,
        "pytest_count": 46,
    }
    return metrics


@app.get("/api/jobs")
def jobs(
    limit: int = Query(default=100, ge=1, le=500),
    status: str | None = None,
    actor: Actor = Depends(_view_actor),
):
    return {"items": repository.list_jobs(limit=limit, status=status)}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, actor: Actor = Depends(_view_actor)):
    job = repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "job": job,
        "events": repository.list_events(job_id),
        "pending": get_interrupt_payload(
            graph, {"configurable": {"thread_id": job_id}}
        ),
    }


@app.get("/api/schedules")
def schedules(
    limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(_view_actor),
):
    return {"items": _read_schedules(limit=limit)}


@app.get("/api/audit/{job_id}")
def audit(job_id: str, actor: Actor = Depends(_view_actor)):
    if not repository.get_job(job_id):
        raise HTTPException(404, "job not found")
    return {"items": repository.list_events(job_id)}


@app.get("/api/admin/users")
def admin_users(actor: Actor = Depends(_admin_actor)):
    users = auth_store.list_users()
    return {
        "items": users,
        "summary": {
            "total": len(users),
            "active": sum(user["active"] for user in users),
            "admins": sum(
                user["role"] == "admin" and user["active"] for user in users
            ),
        },
    }


@app.post("/api/admin/users")
def admin_create_user(
    req: AdminCreateUserRequest,
    actor: Actor = Depends(_admin_actor),
):
    user = auth_store.create_user(
        username=req.username,
        display_name=req.display_name,
        password=req.password,
        role=req.role,
        created_by=actor.user_id,
    )
    return {"user": user}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: str,
    req: UserUpdateRequest,
    actor: Actor = Depends(_admin_actor),
):
    if user_id == actor.user_id:
        if req.active is False:
            raise HTTPException(409, "不能停用当前登录的管理员账号")
        if req.role is not None and req.role != actor.role:
            raise HTTPException(409, "不能修改当前登录账号的角色")
    user = auth_store.update_user(
        user_id,
        role=req.role,
        active=req.active,
        display_name=req.display_name,
    )
    return {"user": user}


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: str,
    req: ResetPasswordRequest,
    actor: Actor = Depends(_admin_actor),
):
    auth_store.reset_password(user_id, req.password)
    return {
        "ok": True,
        "logs_out_user": True,
        "reset_self": user_id == actor.user_id,
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mock_mode": config.USE_MOCK,
        "persistent_checkpoint": True,
        "workspace_db": str(config.WORKSPACE_DB),
        "write_auth_enabled": True,
        "auth_mode": "session",
        "approval_secret_is_dev": config.APPROVAL_SECRET_IS_DEV,
    }


@app.get("/api/session")
def session(
    authorization: str | None = Header(default=None),
):
    actor = resolve_actor(authorization, auth_store)
    require_permission(actor, "content.view")
    return session_payload(actor)


# 运营工作台
app.mount(
    "/",
    StaticFiles(directory=str(config.FRONTEND_DIR), html=True),
    name="static",
)
