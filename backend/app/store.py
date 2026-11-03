"""ContentOps 业务数据仓库。

LangGraph checkpoint 保存工作流执行状态；本模块保存运营工作台需要的任务、
审计事件和聚合指标。两张表都放在独立 SQLite 业务库中，便于本地演示，
也可以后续替换为 PostgreSQL 实现。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else None, ensure_ascii=False)


def _load(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class WorkspaceRepository:
    def __init__(self, path: Path | None = None):
        self.path = path or config.WORKSPACE_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_jobs (
                    id TEXT PRIMARY KEY,
                    account_json TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    candidates_json TEXT,
                    selected_topic_json TEXT,
                    research_evidence_json TEXT,
                    draft_json TEXT,
                    review_result_json TEXT,
                    final_content_json TEXT,
                    analytics_json TEXT,
                    schedule_json TEXT,
                    errors_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES content_jobs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_content_jobs_status
                    ON content_jobs(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_job_events_job
                    ON job_events(job_id, id DESC);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(content_jobs)"
                ).fetchall()
            }
            if "research_evidence_json" not in columns:
                connection.execute(
                    "ALTER TABLE content_jobs ADD COLUMN research_evidence_json TEXT"
                )

    def create_job(self, state: dict, operator: str) -> dict:
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO content_jobs (
                    id, account_json, objective, status, operator,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    state["run_id"],
                    _json(state.get("account")),
                    state.get("objective", ""),
                    state.get("status", "running"),
                    operator,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_job(state["run_id"])

    def sync_state(self, state: dict, operator: str = "系统") -> dict:
        job_id = state["run_id"]
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE content_jobs SET
                    status = ?,
                    operator = CASE WHEN ? = '系统' THEN operator ELSE ? END,
                    candidates_json = ?,
                    selected_topic_json = ?,
                    research_evidence_json = ?,
                    draft_json = ?,
                    review_result_json = ?,
                    final_content_json = ?,
                    analytics_json = ?,
                    schedule_json = ?,
                    errors_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    state.get("status", "running"),
                    operator,
                    operator,
                    _json(state.get("candidates")),
                    _json(state.get("selected_topic")),
                    _json(state.get("research_evidence")),
                    _json(state.get("draft")),
                    _json(state.get("review_result")),
                    _json(state.get("final_content")),
                    _json(state.get("analytics")),
                    _json(state.get("schedule")),
                    _json(state.get("errors") or []),
                    timestamp,
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def add_event(
        self,
        job_id: str,
        event_type: str,
        actor: str,
        message: str,
        data: Any = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, event_type, actor, message, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, event_type, actor, message, _json(data), _now()),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM content_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self, limit: int = 100, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM content_jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._job_row(row) for row in rows]

    def list_events(self, job_id: str) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ?
                ORDER BY id DESC
                """,
                (job_id,),
            ).fetchall()
        return [{
            "id": row["id"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "message": row["message"],
            "data": _load(row["data_json"], {}),
            "created_at": row["created_at"],
        } for row in rows]

    def overview(self) -> dict:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM content_jobs
                GROUP BY status
                """
            ).fetchall()
            recent = connection.execute(
                """
                SELECT * FROM content_jobs
                ORDER BY updated_at DESC LIMIT 8
                """
            ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        total = sum(counts.values())
        finished = (
            counts.get("completed", 0)
            + counts.get("rejected", 0)
            + counts.get("blocked", 0)
        )
        return {
            "total_jobs": total,
            "awaiting_human": counts.get("awaiting_human", 0),
            "active_jobs": counts.get("running", 0) + counts.get("reviewing", 0),
            "completed_jobs": counts.get("completed", 0),
            "rejected_jobs": counts.get("rejected", 0),
            "blocked_jobs": counts.get("blocked", 0),
            "completion_rate": round(
                counts.get("completed", 0) / finished * 100, 1
            ) if finished else 0,
            "recent_jobs": [self._job_row(row) for row in recent],
        }

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "account": _load(row["account_json"], {}),
            "objective": row["objective"],
            "status": row["status"],
            "operator": row["operator"],
            "candidates": _load(row["candidates_json"], []),
            "selected_topic": _load(row["selected_topic_json"], {}),
            "research_evidence": _load(row["research_evidence_json"], []),
            "draft": _load(row["draft_json"], {}),
            "review_result": _load(row["review_result_json"], {}),
            "final_content": _load(row["final_content_json"], {}),
            "analytics": _load(row["analytics_json"], {}),
            "schedule": _load(row["schedule_json"], {}),
            "errors": _load(row["errors_json"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


repository = WorkspaceRepository()
