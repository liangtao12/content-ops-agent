"""工具层（Function Calling 的工具本体）。

Agent 通过 LLM 的 tool calling 决定调用哪些工具、传什么参数；
本文件是工具的真实实现。每个工具都有类型注解和 docstring——
这两者会被转换成 OpenAI tools 协议里的 JSON Schema，模型靠它理解工具。
"""
import json
import hashlib
import os
import random
from datetime import datetime, timedelta

from filelock import FileLock

from . import config
from .rag import search_hit_cases, search_platform_rules


def fetch_hot_topics(platform: str, category: str = "") -> list[dict]:
    """获取指定平台的实时热榜话题（模拟外部 API，数据在 data/hot_topics.json）。

    Args:
        platform: 平台名，如 "小红书" / "抖音" / "视频号"
        category: 可选，按类目过滤，如 "美妆护肤"
    Returns:
        热榜话题列表 [{topic, heat, category, keywords, trend}]
    """
    with open(config.HOT_TOPICS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    data.pop("_desc", None)  # 元数据字段不参与业务
    topics = data.get(platform, [])
    if category:
        topics = [t for t in topics if t["category"] == category]
    # 模拟接口波动
    return sorted(topics, key=lambda t: -t["heat"])[:5]


def write_schedule(
    title: str,
    platform: str,
    publish_at: str,
    author_note: str = "",
    idempotency_key: str = "",
    body: str = "",
    tags: list[str] | None = None,
) -> dict:
    """将通过人工审核的内容写入发布排期表。

    Args:
        title: 内容标题
        platform: 发布平台
        publish_at: ISO 格式发布时间
        author_note: 运营备注
    Returns:
        {"schedule_id": ..., "status": "scheduled", "duplicate": bool}

    idempotency_key 防止重复审批、网络重试或 MCP 客户端重复调用生成多条排期。
    """
    path = config.SCHEDULE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=10)
    with lock:
        try:
            schedule = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            schedule = {"items": []}
        schedule.setdefault("items", [])
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "title": title,
                    "platform": platform,
                    "publish_at": publish_at,
                    "author_note": author_note,
                    "body": body,
                    "tags": tags or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        if idempotency_key:
            for existing in schedule["items"]:
                if existing.get("idempotency_key") == idempotency_key:
                    if existing.get("request_hash") != request_hash:
                        raise ValueError("幂等键已被不同排期请求使用")
                    return {
                        "schedule_id": existing["schedule_id"],
                        "status": "scheduled",
                        "duplicate": True,
                    }

        max_id = 0
        for existing in schedule["items"]:
            value = str(existing.get("schedule_id", ""))
            if value.startswith("S") and value[1:].isdigit():
                max_id = max(max_id, int(value[1:]))
        entry = {
            "schedule_id": f"S{max_id + 1:04d}",
            "title": title,
            "body": body,
            "tags": tags or [],
            "platform": platform,
            "publish_at": publish_at,
            "author_note": author_note,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        schedule["items"].append(entry)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    return {
        "schedule_id": entry["schedule_id"],
        "status": "scheduled",
        "duplicate": False,
    }


def fetch_metrics(title: str, platform: str) -> dict:
    """获取发布后 72 小时数据（模拟数据回流 API），用于复盘 Agent。

    Returns:
        {views, likes, saves, comments, interaction_rate}
    """
    rng = random.Random(title)  # 用标题做种子保证同一条内容数据可复现
    views = rng.randint(80_000, 600_000)
    likes = int(views * rng.uniform(0.06, 0.14))
    saves = int(likes * rng.uniform(0.5, 1.3))
    comments = int(likes * rng.uniform(0.08, 0.2))
    return {"views": views, "likes": likes, "saves": saves, "comments": comments,
            "interaction_rate": round((likes + saves + comments) / views, 4),
            "window": "72h"}


def suggest_publish_slot(platform: str) -> str:
    """按平台流量高峰推荐下一个发布时间槽。"""
    peak_hours = {"小红书": (12, 21), "抖音": (12, 19), "视频号": (20, 21)}
    h1, h2 = peak_hours.get(platform, (12, 20))
    now = datetime.now()
    for day in range(8):
        for h in (h1, h2):
            slot = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            if slot > now:
                return slot.isoformat(timespec="minutes")
    return now.isoformat(timespec="minutes")


# ---------- 工具注册表：名称 -> (函数, 描述)，供 graph / MCP server 共用 ----

TOOL_REGISTRY = {
    "fetch_hot_topics": {
        "fn": fetch_hot_topics,
        "description": "获取指定平台热榜话题，返回话题、热度、类目、趋势。参数: platform(str), category(str, 可选)",
    },
    "search_hit_cases": {
        "fn": search_hit_cases,
        "description": "检索历史爆款案例库，参数: query(str), top_k(int, 默认3)。返回标题、钩子、结构、数据",
    },
    "search_platform_rules": {
        "fn": search_platform_rules,
        "description": "检索平台内容规范知识库(广告法/社区规范/导流规则)，参数: query(str), top_k(int, 默认3)",
    },
    "write_schedule": {
        "fn": write_schedule,
        "description": "将内容写入发布排期表，参数: title, platform, publish_at(ISO时间), author_note(可选), idempotency_key(可选), body(可选), tags(可选)",
    },
    "fetch_metrics": {
        "fn": fetch_metrics,
        "description": "获取内容发布后72小时数据，参数: title, platform",
    },
    "suggest_publish_slot": {
        "fn": suggest_publish_slot,
        "description": "推荐下一个流量高峰发布时间槽，参数: platform",
    },
}


def openai_tools_schema(names: list[str]) -> list[dict]:
    """把注册表里的工具转成 OpenAI tools 协议格式。"""
    props = {
        "fetch_hot_topics": {"platform": {"type": "string"}, "category": {"type": "string"}},
        "search_hit_cases": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "search_platform_rules": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "write_schedule": {"title": {"type": "string"}, "platform": {"type": "string"},
                           "publish_at": {"type": "string"}, "author_note": {"type": "string"},
                           "idempotency_key": {"type": "string"},
                           "body": {"type": "string"},
                           "tags": {"type": "array", "items": {"type": "string"}}},
        "fetch_metrics": {"title": {"type": "string"}, "platform": {"type": "string"}},
        "suggest_publish_slot": {"platform": {"type": "string"}},
    }
    return [{"type": "function", "function": {
        "name": n, "description": TOOL_REGISTRY[n]["description"],
        "parameters": {
            "type": "object",
            "properties": props[n],
            "additionalProperties": False,
        },
    }} for n in names]
