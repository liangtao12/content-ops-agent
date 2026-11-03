"""命令行运行全流程：python run_cli.py "本周运营目标..."

体验完整的多智能体流水线：选题→创作→合规→(打回循环)→人工确认→排期→复盘。
"""
import json
import sys
import uuid

from langgraph.types import Command

from app.graph import DEFAULT_ACCOUNT, build_graph, get_interrupt_payload
from app.persistence import create_checkpointer
from app.tracing import new_recorder, get_recorder


def pretty(obj, title=""):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:1500])


def main():
    objective = sys.argv[1] if len(sys.argv) > 1 else "秋冬敏感肌换季护理选题，目标是涨粉和收藏"
    print(f"[ContentOps] 运营目标: {objective}")

    thread_id = uuid.uuid4().hex[:12]
    new_recorder(thread_id)
    graph = build_graph(create_checkpointer())
    cfg = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(
        {
            "run_id": thread_id,
            "account": DEFAULT_ACCOUNT,
            "objective": objective,
            "status": "running",
        },
        config=cfg)

    pending = get_interrupt_payload(graph, cfg)
    while pending is not None:
        p = pending
        pretty(p.get("draft"), "初稿（等待人工确认）")
        review = p.get("review_result", {})
        if not review.get("passed", True):
            print(f"\n[!] 合规未通过（第 {review.get('round')} 轮）: {review.get('advice','')}")
            print("[!] 已超过自动重写轮次，转人工兜底")
        while True:
            ans = input("\n人工决策 [a=通过 / e=编辑后通过 / r=拒绝]: ").strip().lower()
            if ans in ("a", "e", "r"):
                break
            print("请输入 a / e / r")
        action = {"a": "approved", "e": "edited", "r": "rejected"}[ans]
        content = None
        if action == "edited":
            content = dict(p["draft"])
            content["title"] = input("新标题(回车保留原样): ").strip() or content["title"]
            content["body"] = input("新正文(回车保留原样): ").strip() or content["body"]
        override_reason = ""
        if p.get("requires_override_reason") and action in ("approved", "edited"):
            override_reason = input("合规未通过，填写人工放行理由（留空则取消）: ").strip()
            if not override_reason:
                continue
        result = graph.invoke(
            Command(resume={
                "action": action,
                "content": content,
                "override_reason": override_reason,
            }),
            config=cfg,
        )
        pending = get_interrupt_payload(graph, cfg)

    print(f"\n[最终状态] {result.get('status')}")
    if result.get("schedule"):
        pretty(result["schedule"], "排期结果")
    if result.get("analytics"):
        pretty(result["analytics"], "数据复盘")
    if result.get("errors"):
        pretty(result["errors"], "错误")
    print(f"\n[trace] traces/run_{get_recorder().run_id}.jsonl")
    print(json.dumps(get_recorder().summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
