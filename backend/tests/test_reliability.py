import json
from concurrent.futures import ThreadPoolExecutor

from langgraph.types import Command

import app.graph as graph_module
from app.approvals import issue_approval_token, verify_approval_token
from app.graph import DEFAULT_ACCOUNT, build_graph, get_interrupt_payload
from app.llm import ChatResult, MockLLM
from app.persistence import create_checkpointer
from app.review import review_draft
from app.tools import write_schedule
from app.tracing import new_recorder
from app import config


def _candidates_json():
    return json.dumps({
        "candidates": [{
            "title": "测试选题",
            "reason": "测试",
            "target_hook": "测试",
            "est_score": 0.8,
        }]
    }, ensure_ascii=False)


class BrokenResearchLLM(MockLLM):
    def chat(self, messages, tools=None, purpose="", node=""):
        if purpose == "research_tools":
            return ChatResult(content="", mock=True)
        return ChatResult(content="not-json", mock=True)


class BrokenWriteLLM(MockLLM):
    def chat(self, messages, tools=None, purpose="", node=""):
        if purpose == "research_tools":
            return ChatResult(content="", mock=True)
        if purpose == "research_select":
            return ChatResult(content=_candidates_json(), mock=True)
        return ChatResult(content="not-json", mock=True)


class UnknownToolLLM(MockLLM):
    def chat(self, messages, tools=None, purpose="", node=""):
        if purpose == "research_tools":
            return ChatResult(content="", tool_calls=[{
                "name": "drop_database",
                "arguments": {},
            }], mock=True)
        if purpose == "research_select":
            return ChatResult(content=_candidates_json(), mock=True)
        if purpose == "write":
            return ChatResult(content=json.dumps({
                "title": "测试正常标题",
                "body": "这是一段用于测试的正常内容。",
                "tags": ["测试"],
                "structure_note": "测试",
            }, ensure_ascii=False), mock=True)
        return super().chat(messages, tools=tools, purpose=purpose, node=node)


def test_medium_risk_is_not_silently_passed():
    result = review_draft(
        {"title": "福利", "body": "需要同款加微信私聊", "tags": []},
        MockLLM(),
    )
    assert result["passed"] is False
    assert any(v["risk_level"] == "medium" for v in result["violations"])


def test_malformed_research_terminates_without_key_error(monkeypatch):
    monkeypatch.setattr(graph_module, "llm", BrokenResearchLLM())
    run_id = "broken-research"
    new_recorder(run_id)
    graph = build_graph()
    cfg = {"configurable": {"thread_id": run_id}}
    result = graph.invoke({
        "run_id": run_id,
        "account": DEFAULT_ACCOUNT,
        "objective": "测试异常",
        "status": "running",
    }, config=cfg)
    assert result["status"] == "blocked"
    assert result["selected_topic"] == {}


def test_malformed_write_terminates_without_key_error(monkeypatch):
    monkeypatch.setattr(graph_module, "llm", BrokenWriteLLM())
    run_id = "broken-write"
    new_recorder(run_id)
    graph = build_graph()
    cfg = {"configurable": {"thread_id": run_id}}
    result = graph.invoke({
        "run_id": run_id,
        "account": DEFAULT_ACCOUNT,
        "objective": "测试异常",
        "status": "running",
    }, config=cfg)
    assert result["status"] == "blocked"
    assert result["errors"]


def test_unknown_tool_is_isolated(monkeypatch):
    monkeypatch.setattr(graph_module, "llm", UnknownToolLLM())
    run_id = "unknown-tool"
    new_recorder(run_id)
    graph = build_graph()
    cfg = {"configurable": {"thread_id": run_id}}
    result = graph.invoke({
        "run_id": run_id,
        "account": DEFAULT_ACCOUNT,
        "objective": "测试未知工具",
        "status": "running",
    }, config=cfg)
    assert get_interrupt_payload(graph, cfg) is not None
    assert result["selected_topic"]["title"] == "测试选题"


def test_approval_token_is_bound_to_content_and_platform():
    content = {"title": "标题", "body": "已审核正文", "tags": ["护肤"]}
    token = issue_approval_token(content, "小红书")
    assert verify_approval_token(token, content, "小红书")[0] is True
    changed = {**content, "body": "偷偷加上根治"}
    assert verify_approval_token(token, changed, "小红书")[0] is False
    assert verify_approval_token(token, content, "抖音")[0] is False


def test_checkpoint_survives_new_graph_instance(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite"
    run_id = "restart-thread"
    new_recorder(run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    first_graph = build_graph(create_checkpointer(checkpoint))
    first_graph.invoke({
        "run_id": run_id,
        "account": DEFAULT_ACCOUNT,
        "objective": "秋冬敏感肌护理",
        "status": "running",
    }, config=cfg)
    assert get_interrupt_payload(first_graph, cfg) is not None

    second_graph = build_graph(create_checkpointer(checkpoint))
    assert get_interrupt_payload(second_graph, cfg) is not None
    result = second_graph.invoke(
        Command(resume={"action": "approved"}),
        config=cfg,
    )
    assert result["status"] == "completed"


def test_schedule_lock_is_idempotent_across_threads():
    def schedule_once():
        return write_schedule(
            "并发标题", "小红书", "2026-11-02T12:00", idempotency_key="并发请求"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: schedule_once(), range(8)))
    assert len({r["schedule_id"] for r in results}) == 1
    assert sum(not r["duplicate"] for r in results) == 1
