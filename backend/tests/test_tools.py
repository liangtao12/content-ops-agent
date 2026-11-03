"""单元测试：工具层 + RAG 检索层（离线）。"""
import json

import pytest

from app import config
from app.rag import search_hit_cases, search_platform_rules
from app.tools import (fetch_hot_topics, fetch_metrics, suggest_publish_slot,
                       write_schedule, openai_tools_schema)


def test_fetch_hot_topics_with_category_filter():
    topics = fetch_hot_topics("小红书", "美妆护肤")
    assert len(topics) >= 1
    assert all(t["category"] == "美妆护肤" for t in topics)


def test_fetch_hot_topics_sorted_by_heat():
    topics = fetch_hot_topics("抖音")
    heats = [t["heat"] for t in topics]
    assert heats == sorted(heats, reverse=True)


def test_metrics_deterministic_by_title_seed():
    m1 = fetch_metrics("同一标题", "小红书")
    m2 = fetch_metrics("同一标题", "小红书")
    assert m1 == m2  # 同标题数据可复现（评测需要）


def test_write_schedule_creates_entry():
    result = write_schedule(
        "测试标题", "小红书", "2026-11-02T12:00", "unit-test",
        body="测试正文", tags=["测试"],
    )
    assert result["status"] == "scheduled" and result["schedule_id"].startswith("S")
    saved = json.loads(config.SCHEDULE_FILE.read_text(encoding="utf-8"))
    assert saved["items"][-1]["body"] == "测试正文"
    assert saved["items"][-1]["tags"] == ["测试"]
    duplicate = write_schedule("测试标题", "小红书", "2026-11-02T12:00", "unit-test")
    assert duplicate["duplicate"] is False


def test_write_schedule_is_idempotent():
    first = write_schedule(
        "幂等标题", "小红书", "2026-11-02T12:00", idempotency_key="same-request"
    )
    second = write_schedule(
        "幂等标题", "小红书", "2026-11-02T12:00", idempotency_key="same-request"
    )
    assert first["schedule_id"] == second["schedule_id"]
    assert second["duplicate"] is True


def test_idempotency_key_rejects_different_payload():
    write_schedule(
        "原请求", "小红书", "2026-11-02T12:00", idempotency_key="conflict-key"
    )
    with pytest.raises(ValueError, match="不同排期请求"):
        write_schedule(
            "被修改的请求", "小红书", "2026-11-02T12:00",
            idempotency_key="conflict-key",
        )


def test_suggest_publish_slot_in_future():
    assert suggest_publish_slot("小红书") > "2026-01-01"  # ISO 字符串可比较


def test_rag_rules_retrieval_relevant():
    results = search_platform_rules("广告法极限词 第一 最佳", top_k=2)
    assert len(results) >= 1
    assert any("极限词" in r["title"] or "广告法" in r["title"] for r in results)


def test_rag_cases_retrieval_relevant():
    results = search_hit_cases("敏感肌 护肤", top_k=3)
    assert len(results) >= 1
    assert any("敏感肌" in r["title"] for r in results)


def test_openai_tools_schema_format():
    schema = openai_tools_schema(["fetch_hot_topics", "search_hit_cases"])
    assert all(s["type"] == "function" for s in schema)
    assert {s["function"]["name"] for s in schema} == {"fetch_hot_topics", "search_hit_cases"}
    assert "platform" in schema[0]["function"]["parameters"]["properties"]
