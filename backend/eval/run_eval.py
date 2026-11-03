"""评测流水线：跑 golden 数据集 -> 断言业务预期 -> LLM-as-judge 打分 -> 输出报告。

用法:
    python -m eval.run_eval              # 全量
    python -m eval.run_eval E02          # 单用例（调试）

产出:
    eval/report.md          人读报告（含指标汇总表）
    traces/run_*.jsonl      每用例的执行 trace

核心指标（简历/面试引用的就是这些）:
    - 全流程完成率          用例跑到终态的比例
    - 合规拦截率            应拦截用例中被正确打回/标记的比例
    - 误伤率                不应拦截却被打回的比例
    - 平均审核轮次          反思循环的收敛速度
    - 内容质量分            LLM-as-judge 四维均分
"""
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

from langgraph.types import Command

from app import config
from app.graph import DEFAULT_ACCOUNT, build_graph, get_interrupt_payload
from app.guardrails import check_text
from app.tracing import get_recorder, new_recorder
from eval.judge import judge_content

EVAL_DIR = Path(__file__).parent
# 评测模拟人工排期，但不能污染演示/生产运行态排期表。
_EVAL_TMPDIR = tempfile.TemporaryDirectory(
    prefix=".eval-tmp-",
    dir=config.BASE_DIR,
)
config.SCHEDULE_FILE = Path(_EVAL_TMPDIR.name) / "schedule.json"
# 评测 trace 同样落临时目录：否则每跑一次评测就往 backend/traces 里塞 10 个文件，
# 真正的线上运行日志会被评测噪声淹没。
config.TRACE_DIR = Path(_EVAL_TMPDIR.name) / "traces"
HUMAN_ACTIONS = {  # golden 用例声明的人工决策，评测自动模拟运营人员
    "approved": {"action": "approved"},
    "edited": {"action": "edited", "content": None},
    "rejected": {"action": "rejected"},
}


def load_cases():
    return [json.loads(l) for l in
            (EVAL_DIR / "golden.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def run_case(case: dict) -> dict:
    """跑单个用例，模拟人工决策并恢复工作流。"""
    thread_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
    new_recorder(thread_id)
    graph = build_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    expect = case.get("expect", {})
    human_action = expect.get("human", "approved")

    result = graph.invoke(
        {
            "run_id": thread_id,
            "account": DEFAULT_ACCOUNT,
            "objective": case["objective"],
            "status": "running",
        },
        config=cfg)

    pending = get_interrupt_payload(graph, cfg)
    if pending is not None:
        resume = dict(HUMAN_ACTIONS[human_action])
        if resume["action"] == "edited":
            edited = dict(pending["draft"])
            edited["title"] = edited["title"] + "（运营改）"
            resume["content"] = edited
        if (
            pending.get("requires_override_reason")
            and resume["action"] == "approved"
        ):
            resume["override_reason"] = "评测模拟运营确认转人工兜底"
        result = graph.invoke(Command(resume=resume), config=cfg)

    review = result.get("review_result", {})
    final = result.get("final_content") or result.get("draft", {})
    status = result.get("status", "unknown")

    # 断言
    checks = {}
    if "status" in expect:
        checks["status"] = (status == expect["status"])
    if expect.get("violations_caught"):
        checks["violations_caught"] = len(review.get("violations", [])) > 0
    if expect.get("review_pass"):
        checks["review_pass"] = review.get("passed", False)
    if expect.get("review_pass_any_round"):
        checks["review_pass_any_round"] = review.get("passed", False)
    if expect.get("needs_attention"):
        checks["needs_attention"] = not review.get("passed", True)  # 转人工兜底标记

    # quality judge（仅对完成的内容）
    # Mock 模式所有创作节点使用同一模板，不能把启发式结果包装成内容质量。
    quality = None
    if final and not config.USE_MOCK:
        quality = judge_content(case["objective"], final, review.get("violations", []))

    return {
        "case": case["id"], "desc": case["desc"], "status": status,
        "review_rounds": result.get("review_rounds", 0),
        "violations": len(review.get("violations", [])),
        "review_passed": review.get("passed", False),
        "needs_attention": not review.get("passed", True) and status == "completed",
        "checks": checks, "passed": all(checks.values()) if checks else True,
        "quality": quality, "trace": get_recorder().summary(),
    }


def run_guardrail_cases() -> tuple[list[dict], dict]:
    path = EVAL_DIR / "compliance_cases.jsonl"
    if not path.exists():
        return [], {}
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = []
    tp = fp = tn = fn = 0
    for case in cases:
        violations = check_text(case["text"])
        predicted = bool(violations)
        expected = bool(case["expected_block"])
        correct = predicted == expected
        if predicted and expected:
            tp += 1
        elif predicted and not expected:
            fp += 1
        elif not predicted and not expected:
            tn += 1
        else:
            fn += 1
        rows.append({
            **case,
            "predicted_block": predicted,
            "hits": [v["word"] for v in violations],
            "correct": correct,
        })

    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    accuracy = (tp + tn) / len(cases) if cases else 0
    return rows, {
        "样本数": len(cases),
        "TP": tp,
        "FP": fp,
        "TN": tn,
        "FN": fn,
        "Precision": f"{precision:.3f}",
        "Recall": f"{recall:.3f}",
        "F1": f"{f1:.3f}",
        "Accuracy": f"{accuracy:.3f}",
    }


def main():
    cases = load_cases()
    if len(sys.argv) > 1:  # 只跑指定用例
        cases = [c for c in cases if c["id"] in sys.argv[1:]]

    results = []
    guardrail_rows, guardrail_metrics = run_guardrail_cases()
    t0 = time.time()
    for c in cases:
        r = run_case(c)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {r['case']} {r['desc']}  status={r['status']} "
              f"rounds={r['review_rounds']} violations={r['violations']}")

    # ---- 指标汇总 ----
    n = len(results)
    done = [r for r in results if r["status"] in ("completed", "rejected")]
    should_block = [r for r in results if "violations_caught" in r["checks"]]
    blocked_ok = [r for r in should_block if r["checks"].get("violations_caught")]
    should_pass = [r for r in results if r["checks"].get("review_pass")]
    false_block = [r for r in should_pass if not r["review_passed"]]
    qualities = [r["quality"] for r in results if r["quality"]]
    avg_q = round(sum(
        q["relevance"] + q["compliance"] + q["structure"] + q["appeal"]
        for q in qualities) / max(1, len(qualities) * 4), 2) if qualities else None

    metrics = {
        "全流程完成率": f"{len(done)}/{n}",
        "合规拦截率": f"{len(blocked_ok)}/{len(should_block)}",
        "误伤率(不应拦而拦)": f"{len(false_block)}/{len(should_pass)}",
        "平均审核轮次": round(sum(r['review_rounds'] for r in results) / max(1, n), 2),
        "内容质量分(1-5)": avg_q if avg_q is not None else "N/A（Mock 模式不评）",
        "断言通过": f"{sum(1 for r in results if r['passed'])}/{n}",
    }
    print("\n===== 指标 =====")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    # ---- 输出 markdown 报告 ----
    lines = [
        "# ContentOps Agent 评测报告", "",
        f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}  模式: {'Mock' if __import__('app.config', fromlist=['USE_MOCK']).USE_MOCK else 'DeepSeek'}",
        f"- 用例数: {n}  总耗时: {time.time() - t0:.1f}s", "",
        "| 指标 | 结果 |", "|---|---|",
    ] + [f"| {k} | {v} |" for k, v in metrics.items()] + [
        "", "## 用例明细", "",
        "| 用例 | 描述 | 状态 | 审核轮次 | 违规命中 | 断言 | 质量分 |", "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        q = r["quality"]
        qs = f"{(q['relevance']+q['compliance']+q['structure']+q['appeal'])/4:.1f}" if q else "-"
        lines.append(f"| {r['case']} | {r['desc']} | {r['status']} | {r['review_rounds']} "
                     f"| {r['violations']} | {'✅' if r['passed'] else '❌'} | {qs} |")
    lines += [
        "",
        "## 规则护栏评测",
        "",
        "> 该部分只评价确定性违禁词护栏，不等同于完整 LLM 语义合规效果。",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in guardrail_metrics.items()]
    if guardrail_rows:
        lines += ["", "### 分类错误", ""]
        errors = [r for r in guardrail_rows if not r["correct"]]
        if errors:
            lines += ["| 用例 | 文本 | 期望 | 预测 | 命中 |", "|---|---|---|---|---|"]
            for r in errors:
                lines.append(
                    f"| {r['id']} | {r['text']} | {r['expected_block']} | "
                    f"{r['predicted_block']} | {', '.join(r['hits']) or '-'} |"
                )
        else:
            lines.append("本轮没有分类错误。")
    lines += ["", "## 结论与回归基线", "",
              "以上指标作为回归基线：任何代码/提示词改动后重跑 `python -m eval.run_eval`，",
              "若合规 Recall 下降、误伤率上升或流程断言下降则判定回归，需要在合并前修复。",
              "",
              "评测边界：",
              "",
              "- 10 条 golden 用例用于验证 LangGraph 状态流转、人工中断、编辑复审和兜底路径。",
              "- 44 条规则用例只验证确定性违禁词护栏，不等同于 LLM 的完整语义判断。",
              "- Mock 模式不计算内容质量分；接入 DeepSeek 后才启用 LLM-as-judge。",
              "- 否定或警示语境中出现风险词仍可能被规则层召回，需结合 LLM 或人工复核。"]
    (EVAL_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 eval/report.md")


if __name__ == "__main__":
    main()
