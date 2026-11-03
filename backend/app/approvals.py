"""人工审批令牌：供 MCP 等外部入口校验“内容确实已获人工批准”。"""
import base64
import hashlib
import hmac
import json
import time
from typing import Any

from . import config


def _canonical_content(content: dict, platform: str) -> str:
    payload = {
        "title": str(content.get("title", "")),
        "body": str(content.get("body", "")),
        "tags": list(content.get("tags") or []),
        "platform": platform,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sign(payload: bytes) -> str:
    return hmac.new(
        config.APPROVAL_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def issue_approval_token(
    content: dict, platform: str, *, override: bool = False
) -> str:
    data: dict[str, Any] = {
        "content_hash": hashlib.sha256(
            _canonical_content(content, platform).encode("utf-8")
        ).hexdigest(),
        "platform": platform,
        "override": override,
        "exp": int(time.time()) + config.APPROVAL_TTL_MINUTES * 60,
    }
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_approval_token(
    token: str, content: dict, platform: str
) -> tuple[bool, str]:
    try:
        encoded, signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(payload)):
            return False, "审批令牌签名无效"
        data = json.loads(payload)
    except (ValueError, json.JSONDecodeError, TypeError):
        return False, "审批令牌格式无效"

    if data.get("exp", 0) < int(time.time()):
        return False, "审批令牌已过期"
    if data.get("platform") != platform:
        return False, "审批令牌与发布平台不匹配"
    expected = hashlib.sha256(
        _canonical_content(content, platform).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(data.get("content_hash", ""), expected):
        return False, "审批内容已被修改，必须重新审批"
    return True, ""
