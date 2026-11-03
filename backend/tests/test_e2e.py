"""端到端集成测试：完整工作流（Mock LLM，离线可跑）。

这是这个项目最有价值的测试之一：证明"多智能体编排 + interrupt 恢复
+ 合规打回循环"整条链路是通的，而不只是单个节点能跑。
"""
import pytest
from langgraph.types import Command

from app.graph import DEFAULT_ACCOUNT, build_graph, get_interrupt_payload
from app.tracing import new_recorder


@pytest.fixture
def graph():
    return build_graph()


def _run(graph, objective, human_action="approved", override_reason=""):
    run_id = f"pytest-{objective[:8]}"
    new_recorder(run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    result = graph.invoke(
        {
            "run_id": run_id,
            "account": DEFAULT_ACCOUNT,
            "objective": objective,
            "status": "running",
        },
        config=cfg)
    if get_interrupt_payload(graph, cfg) is not None:
        result = graph.invoke(
            Command(resume={
                "action": human_action,
                "override_reason": override_reason,
            }),
            config=cfg,
        )
    return result


def test_full_pipeline_completes(graph):
    result = _run(graph, "秋冬敏感肌护理选题")
    assert result["status"] == "completed"
    assert result["final_content"]["title"]
    assert result["schedule"]["schedule_id"]
    assert result["analytics"]["report"]
    assert result["analytics"]["metrics"]["views"] > 0


def test_violation_triggers_rewrite_loop(graph):
    """注入极限词 → 合规打回 → 重写 → （Mock 仍含违禁）→ 第二轮打回 → 超轮次转人工。"""
    result = _run(
        graph,
        "[注入极限词] 要全网第一根治",
        override_reason="人工复核确认只做风险案例演示",
    )
    review = result["review_result"]
    assert len(review["violations"]) > 0       # 违规被程序护栏捕获
    assert result["review_rounds"] >= 2        # 打回循环确实发生了
    assert not review["passed"]                # 始终未自动通过
    # needs_attention：自动修复失败，靠人工兜底确认
    assert result["status"] == "completed"     # 人工 approved 兜底放行


def test_human_reject_stops_pipeline(graph):
    result = _run(graph, "职场沟通选题", human_action="rejected")
    assert result["status"] == "rejected"
    assert "schedule" not in result            # 拒绝后不排期不复盘


def test_research_produces_three_candidates(graph):
    run_id = "t-cand"
    new_recorder(run_id)
    cfg = {"configurable": {"thread_id": "t-cand"}}
    result = graph.invoke(
        {
            "run_id": run_id,
            "account": DEFAULT_ACCOUNT,
            "objective": "美食备餐选题",
            "status": "running",
        },
        config=cfg)
    assert len(result["candidates"]) == 3
    assert all("title" in c and "reason" in c for c in result["candidates"])


def test_approved_requires_override_after_failed_review(graph):
    result = _run(graph, "[注入极限词] 要全网第一根治")
    assert result["status"] == "blocked"
    assert result["human_decision"] == "rejected"


def test_edited_content_is_rechecked_and_can_pass(graph):
    run_id = "t-edit-pass"
    new_recorder(run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    graph.invoke({
        "run_id": run_id,
        "account": DEFAULT_ACCOUNT,
        "objective": "[注入极限词] 全网第一根治",
        "status": "running",
    }, config=cfg)
    pending = get_interrupt_payload(graph, cfg)
    assert pending["needs_attention"] is True

    edited = dict(pending["draft"])
    edited["body"] = "记录敏感肌日常护理，每个人肤质不同，请先做局部测试。"
    result = graph.invoke(
        Command(resume={"action": "edited", "content": edited}),
        config=cfg,
    )
    assert result["status"] == "completed"
    assert result["review_result"]["passed"] is True
    assert result["schedule"]["approval_token"]


def test_bad_edit_reinterrupts_instead_of_bypassing_review(graph):
    run_id = "t-edit-fail"
    new_recorder(run_id)
    cfg = {"configurable": {"thread_id": run_id}}
    graph.invoke({
        "run_id": run_id,
        "account": DEFAULT_ACCOUNT,
        "objective": "[注入极限词] 全网第一根治",
        "status": "running",
    }, config=cfg)
    pending = get_interrupt_payload(graph, cfg)
    result = graph.invoke(
        Command(resume={"action": "edited", "content": pending["draft"]}),
        config=cfg,
    )
    assert result["status"] == "awaiting_human"
    assert get_interrupt_payload(graph, cfg)["requires_override_reason"] is True
