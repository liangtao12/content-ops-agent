"""账号、密码、会话和角色权限。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

from fastapi import HTTPException

from . import config

Role = Literal["admin", "reviewer", "operator", "viewer"]

ROLE_LABELS: dict[str, str] = {
    "admin": "管理员",
    "reviewer": "内容审核员",
    "operator": "内容运营",
    "viewer": "只读观察者",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "content.view",
        "content.create",
        "content.approve",
        "settings.manage",
    },
    "reviewer": {"content.view", "content.approve"},
    "operator": {"content.view", "content.create"},
    "viewer": {"content.view"},
}

# 角色功能说明，供注册页、成员管理和会话信息统一展示。
ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "拥有全部权限：创建内容、审批编辑、成员管理与系统治理。",
    "reviewer": "查看并审批内容，可编辑后重新复审；不能创建任务或管理成员。",
    "operator": "查看内容并创建任务；不能审批内容或管理成员。",
    "viewer": "仅查看内容与报表，不能创建、审批或管理。",
}

# 公开注册允许自选的角色。admin 只能由现有管理员在成员管理中分配，
# 避免任何人通过注册接口直接提权。
SELF_REGISTER_ROLES: tuple[str, ...] = ("operator", "reviewer", "viewer")

PERMISSION_LABELS: dict[str, str] = {
    "content.view": "查看内容",
    "content.create": "创建任务",
    "content.approve": "审批与编辑",
    "settings.manage": "成员与系统治理",
}

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
PASSWORD_ITERATIONS = 310_000
MAX_LOGIN_FAILURES = 5
LOCK_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_username(username: str) -> str:
    value = username.strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise HTTPException(
            422,
            "用户名需为 3-32 位字母、数字、下划线、点或短横线",
        )
    return value.lower()


def validate_password(password: str) -> str:
    if len(password) < 8 or len(password) > 128:
        raise HTTPException(422, "密码长度必须为 8-128 位")
    if password.isdigit() or password.isalpha():
        raise HTTPException(422, "密码必须同时包含字母和数字")
    return password


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}$"
        f"{base64.b64encode(digest).decode()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Actor:
    user_id: str
    username: str
    name: str
    role: Role
    is_service: bool = False

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]

    @property
    def event_label(self) -> str:
        return f"{self.name}（{self.role_label}）"

    @property
    def permissions(self) -> list[str]:
        return sorted(ROLE_PERMISSIONS[self.role])


class AuthStore:
    """SQLite 用户与会话存储。"""

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
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT,
                    failed_login_count INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    last_login_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                    ON auth_sessions(user_id, expires_at);
                """
            )

    def user_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            )

    def bootstrap(self) -> dict:
        count = self.user_count()
        return {
            "user_count": count,
            "first_user": count == 0,
            "registration_code_required": bool(
                count > 0 and config.REGISTRATION_CODE
            ),
            "public_registration": (
                count == 0 or not bool(config.REGISTRATION_CODE)
            ),
            "self_register_roles": list(SELF_REGISTER_ROLES),
            # 全量角色目录，供前端展示各角色功能与权限差异。
            "role_catalog": [
                {
                    "id": role,
                    "label": label,
                    "description": ROLE_DESCRIPTIONS[role],
                    "permissions": [
                        PERMISSION_LABELS[permission]
                        for permission in (
                            "content.view",
                            "content.create",
                            "content.approve",
                            "settings.manage",
                        )
                        if permission in ROLE_PERMISSIONS[role]
                    ],
                    "self_register": role in SELF_REGISTER_ROLES,
                }
                for role, label in ROLE_LABELS.items()
            ],
        }

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: Role = "operator",
        created_by: str | None = None,
    ) -> dict:
        username = validate_username(username)
        display_name = display_name.strip()
        if not display_name or len(display_name) > 80:
            raise HTTPException(422, "显示名称需为 1-80 个字符")
        if role not in ROLE_LABELS:
            raise HTTPException(422, "unsupported role")
        user_id = uuid.uuid4().hex
        now = _iso(_now())
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, display_name, password_hash, role,
                        active, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        display_name,
                        hash_password(password),
                        role,
                        created_by,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "用户名已存在") from exc
        return self.get_user(user_id)

    def register(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        registration_code: str = "",
        role: str = "operator",
    ) -> dict:
        count = self.user_count()
        if count == 0:
            # 首个账号固定为管理员，不开放角色选择。
            assigned_role: Role = "admin"
        else:
            if role not in SELF_REGISTER_ROLES:
                raise HTTPException(
                    422,
                    "注册时只能选择内容运营、内容审核员或只读观察者；"
                    "管理员角色需由现有管理员在成员管理中分配",
                )
            assigned_role = cast(Role, role)
            if config.REGISTRATION_CODE and not hmac.compare_digest(
                registration_code,
                config.REGISTRATION_CODE,
            ):
                raise HTTPException(403, "邀请码无效")
        return self.create_user(
            username=username,
            display_name=display_name,
            password=password,
            role=assigned_role,
            created_by=None if count == 0 else "self-register",
        )

    def authenticate(self, username: str, password: str) -> dict:
        normalized = validate_username(username)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (normalized,),
            ).fetchone()
            if not row:
                raise HTTPException(401, "用户名或密码错误")
            if not row["active"]:
                raise HTTPException(403, "账号已停用，请联系管理员")
            locked_until = row["locked_until"]
            if locked_until and datetime.fromisoformat(locked_until) > _now():
                raise HTTPException(429, "登录尝试过多，请稍后再试")
            if not verify_password(password, row["password_hash"]):
                failures = int(row["failed_login_count"]) + 1
                lock_value = (
                    _iso(_now() + timedelta(minutes=LOCK_MINUTES))
                    if failures >= MAX_LOGIN_FAILURES
                    else None
                )
                connection.execute(
                    """
                    UPDATE users SET failed_login_count = ?,
                        locked_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (failures, lock_value, _iso(_now()), row["id"]),
                )
                raise HTTPException(401, "用户名或密码错误")
            connection.execute(
                """
                UPDATE users SET failed_login_count = 0, locked_until = NULL,
                    last_login_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (_iso(_now()), _iso(_now()), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM users WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._public_user(updated)

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(hours=config.AUTH_SESSION_HOURS)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    token_hash, user_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (_token_hash(token), user_id, _iso(now), _iso(expires)),
            )
        return token

    def resolve_session(self, token: str) -> dict | None:
        now = _now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.*
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND u.active = 1
                """,
                (_token_hash(token), _iso(now)),
            ).fetchone()
        return self._public_user(row) if row else None

    def revoke_session(self, token: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_iso(_now()), _token_hash(token)),
            )

    def get_user(self, user_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._public_user(row) if row else None

    def list_users(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM users
                ORDER BY
                    CASE role
                        WHEN 'admin' THEN 0
                        WHEN 'reviewer' THEN 1
                        WHEN 'operator' THEN 2
                        ELSE 3
                    END,
                    created_at
                """
            ).fetchall()
        return [self._public_user(row) for row in rows]

    def update_user(
        self,
        user_id: str,
        *,
        role: Role | None = None,
        active: bool | None = None,
        display_name: str | None = None,
    ) -> dict:
        user = self.get_user(user_id)
        if not user:
            raise HTTPException(404, "用户不存在")
        if role is not None and role not in ROLE_LABELS:
            raise HTTPException(422, "unsupported role")
        if display_name is not None:
            display_name = display_name.strip()
            if not display_name or len(display_name) > 80:
                raise HTTPException(422, "显示名称需为 1-80 个字符")

        if user["role"] == "admin" and (
            (role is not None and role != "admin") or active is False
        ):
            users = self.list_users()
            active_admins = [
                item for item in users
                if item["role"] == "admin" and item["active"]
            ]
            if len(active_admins) <= 1:
                raise HTTPException(409, "系统必须保留至少一名启用状态的管理员")

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE users SET
                    role = COALESCE(?, role),
                    active = COALESCE(?, active),
                    display_name = COALESCE(?, display_name),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    role,
                    int(active) if active is not None else None,
                    display_name,
                    _iso(_now()),
                    user_id,
                ),
            )
        return self.get_user(user_id)

    def reset_password(self, user_id: str, password: str) -> None:
        if not self.get_user(user_id):
            raise HTTPException(404, "用户不存在")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE users SET password_hash = ?, failed_login_count = 0,
                    locked_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (hash_password(password), _iso(_now()), user_id),
            )
            connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (_iso(_now()), user_id),
            )

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "role_label": ROLE_LABELS.get(row["role"], row["role"]),
            "active": bool(row["active"]),
            "created_by": row["created_by"],
            "last_login_at": row["last_login_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def actor_from_user(user: dict) -> Actor:
    return Actor(
        user_id=user["id"],
        username=user["username"],
        name=user["display_name"],
        role=user["role"],
    )


def resolve_actor(
    authorization: str | None,
    store: AuthStore,
) -> Actor:
    if authorization and config.CONTENTOPS_API_KEY:
        if hmac.compare_digest(
            authorization,
            f"Bearer {config.CONTENTOPS_API_KEY}",
        ):
            return Actor(
                user_id="service",
                username="service",
                name="自动化服务",
                role="admin",
                is_service=True,
            )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    token = authorization.removeprefix("Bearer ").strip()
    user = store.resolve_session(token)
    if not user:
        raise HTTPException(401, "登录已失效，请重新登录")
    return actor_from_user(user)


def require_permission(actor: Actor, permission: str) -> None:
    if permission not in ROLE_PERMISSIONS[actor.role]:
        raise HTTPException(
            403,
            f"{actor.role_label}没有执行 {permission} 的权限",
        )


def session_payload(actor: Actor) -> dict:
    return {
        "actor": {
            "id": actor.user_id,
            "username": actor.username,
            "name": actor.name,
            "role": actor.role,
            "role_label": actor.role_label,
            "permissions": actor.permissions,
            "is_service": actor.is_service,
        },
        "available_roles": [
            {
                "id": role,
                "label": label,
                "permissions": sorted(ROLE_PERMISSIONS[role]),
            }
            for role, label in ROLE_LABELS.items()
        ],
    }


auth_store = AuthStore()
