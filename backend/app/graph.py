"""核心工作流：LangGraph 多智能体编排。

架构（planner -> executors -> human-in-the-loop -> analytics 闭环）：

    ┌────────────┐   ┌──────────┐   ┌──────────────┐
    │ research    │──▶│  write   │──▶│ review(合规) │──┐
    │ 选题研究员  │   │ 创作Agent│   │ 程序护栏+LLM │  │ 未过&轮次<2: 打回重写
    └────────────┘   └──────────┘   └──────────────┘  │
                                       │ 通过/超过轮次 │
                                       ▼              │
                                  ┌─────────┐          │
                                  │ human ✋ │ interrupt 等待运营确认
                                  └─────────┘
                                       │ approved/edited
                                       ▼
                                  ┌──────────┐   ┌────────────┐
                                  │ schedule │──▶│ analytics  │──▶ END
                                  │ 排期发布 │   │ 数据复盘    │
                                  └──────────┘   └────────────┘

六个节点对应六种角色；review 打回形成"反思循环"（reflection loop），
人工节点用 langgraph interrupt 实现 human-in-the-loop。
"""
import json

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from . import config
from .approvals import issue_approval_token, verify_approval_token
from .guardrails import CandidateList, Draft, validate_json
from .llm import get_llm
from .review import review_draft
from .state import ContentOpsState
from .tools import (TOOL_REGISTRY, fetch_metrics, openai_tools_schema,
                    suggest_publish_slot, write_schedule)
from .tracing import get_recorder

llm = get_llm()


def _record(node, input_summary, output_summary, ok=True):
    get_recorder().record_node(node, input_summary, output_summary, ok)


# ---------------- 节点 1：选题研究员 ----------------

def research_node(state):
    """两阶段工具调用：
    1) LLM 决定调用哪些工具（真实 tool calling 协议）→ 执行 → 把结果喂回；
    2) LLM 综合热榜 + 历史爆款 + 账号定位，产出 3 个候选选题。
    """
    account = state["account"]
    objective = state["objective"]
    tools = openai_tools_schema(["fetch_hot_topics", "search_hit_cases"])
    messages = [
        {"role": "system", "content": f"你是内容选题研究员。账号：{account['name']}，"
                                       f"平台：{account['platform']}，垂类：{account['category']}。"},
        {"role": "user", "content": f"本周运营目标：{objective}。请先调用工具获取热榜与历史爆款案例。"},
    ]
    r = llm.chat(messages, tools=tools, purpose="research_tools", node="research")

    # 执行模型决定的工具调用；工具名、参数和异常必须被隔离，不能击穿整个图。
    tool_outputs = []
    for tc in r.tool_calls:
        name = tc.get("name", "")
        arguments = tc.get("arguments") or {}
        if name not in TOOL_REGISTRY or name not in {"fetch_hot_topics", "search_hit_cases"}:
            tool_outputs.append({
                "tool": name,
                "args": arguments,
                "result": {"error": f"未知或不允许的工具: {name}"},
            })
            continue
        try:
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON object")
            out = TOOL_REGISTRY[name]["fn"](**arguments)
            tool_outputs.append({"tool": name, "args": arguments, "result": out})
        except Exception as exc:
            tool_outputs.append({
                "tool": name,
                "args": arguments,
                "result": {"error": f"{type(exc).__name__}: {exc}"},
            })

    # 工具结果喂回，产出结构化候选选题
    messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call_{i}", "type": "function",
         "function": {"name": t["tool"], "arguments": json.dumps(t["args"], ensure_ascii=False)}}
        for i, t in enumerate(tool_outputs)]})
    for t in tool_outputs:
        messages.append({"role": "tool", "tool_call_id": f"call_{tool_outputs.index(t)}",
                         "content": json.dumps(t["result"], ensure_ascii=False)[:2000]})
    messages.append({"role": "user",
                     "content": "基于以上热榜与爆款案例，输出 3 个候选选题 JSON："
                                '{"candidates":[{"title","reason","target_hook","est_score"}]}'})

    r2 = llm.chat(messages, purpose="research_select", node="research")
    parsed, err = validate_json(r2.content, CandidateList)
    research_evidence = [
        {
            "tool": item["tool"],
            "args": item["args"],
            "result": (
                item["result"][:5]
                if isinstance(item["result"], list)
                else item["result"]
            ),
        }
        for item in tool_outputs
    ]
    if parsed is None or not parsed.candidates:
        _record("research", {"objective": objective}, {"error": err}, ok=False)
        return {"errors": [f"research json parse: {err or '候选选题为空'}"], "status": "blocked",
                "candidates": [], "selected_topic": {},
                "research_evidence": research_evidence}
    candidates = [c.model_dump() for c in parsed.candidates]
    selected = max(candidates, key=lambda c: c.get("est_score", 0))
    _record("research", {"objective": objective},
            {"n_candidates": len(candidates), "selected": selected["title"]})
    return {
        "candidates": candidates,
        "selected_topic": selected,
        "research_evidence": research_evidence,
    }


# ---------------- 节点 2：内容创作 ----------------

def write_node(state):
    """创作 Agent：检索平台规范做 grounding，按爆款结构生成初稿。"""
    topic = state["selected_topic"]
    if not topic or not topic.get("title"):
        return {"errors": ["缺少有效选题，无法创作"], "status": "blocked"}
    account = state["account"]
    brand = account.get("brand_kit") or {}
    brand_guidance = (
        f"品牌语气：{brand.get('tone', '无额外要求')}\n"
        f"必须体现：{brand.get('required_phrases', [])}\n"
        f"禁用表达：{brand.get('banned_claims', [])}\n"
        f"行动号召：{brand.get('cta', '无额外要求')}"
    )
    rules = TOOL_REGISTRY["search_platform_rules"]["fn"](
        f"{account['category']} 规范", top_k=2)
    rules_text = "\n".join(r["text"][:300] for r in rules)

    messages = [
        {"role": "system", "content": f"你是{account['platform']}内容创作Agent，人设：{account['persona']}。"
                                       f"垂类：{account['category']}。"
                                       f"必须遵守以下品牌约束：\n{brand_guidance}\n"
                                       f"必须遵守以下平台规范：\n{rules_text}"},
        {"role": "user", "content": f"运营目标：{state['objective']}\n选题：{topic['title']}\n"
                                    f"目标钩子：{topic.get('target_hook','')}\n"
                                    f"历史打回意见：{state.get('review_result',{}).get('advice','无')}\n"
                                    f"输出JSON：{{\"title\",\"body\",\"tags\",\"structure_note\"}}"},
    ]
    r = llm.chat(messages, purpose="write", node="write")
    parsed, err = validate_json(r.content, Draft)
    if parsed is None:
        _record("write", {"topic": topic["title"]}, {"error": err}, ok=False)
        return {"errors": [f"write json parse: {err}"], "status": "blocked"}
    draft = parsed.model_dump()
    _record("write", {"topic": topic["title"]},
            {"title": draft["title"], "body_len": len(draft["body"])})
    return {"draft": draft}


# ---------------- 节点 3：合规审核（护栏 + LLM 复核） ----------------

def review_node(state):
    """双层审核：
    第一层 程序护栏：违禁词正则扫描（确定性、零成本）；
    第二层 LLM 评估：语义级风险（如暗示疗效），输出 low/medium/high。
    任一风险命中都视为未通过，避免中风险词被静默放行。
    """
    draft = state["draft"]
    review_result = review_draft(
        draft,
        llm,
        (state.get("account") or {}).get("brand_kit"),
    )
    round_no = state.get("review_rounds", 0) + 1
    review_result["round"] = round_no
    _record("review", {"title": draft["title"]},
            {"passed": review_result["passed"],
             "n_violations": len(review_result["violations"]),
             "round": round_no})
    return {"review_result": review_result, "review_rounds": round_no}


def _route_after_review(state):
    """条件路由：打回重写（反思循环）还是进入人工确认。"""
    rr = state.get("review_result", {})
    if rr.get("passed"):
        return "human"
    if state.get("review_rounds", 0) >= config.MAX_REVIEW_ROUNDS:
        return "human"      # 自动修复不了 → 交人工兜底（打上 needs_attention 标记）
    return "write"


def _route_after_work(state):
    """研究/创作失败后直接终止，不能带着缺失字段继续执行。"""
    return "end" if state.get("status") == "blocked" else "continue"


# ---------------- 节点 4：人工确认（human-in-the-loop） ----------------

def human_node(state):
    """LangGraph interrupt：工作流在此暂停，等待运营人员决策。

    CLI: input() 同步确认；API: 返回 pending，由 /api/human 接口
    通过 Command(resume={...}) 恢复。resume 载荷:
      {"action": "approved"|"edited"|"rejected", "content": {...可选改稿...}}
    """
    payload = interrupt({
        "ask": "请人工确认内容是否发布",
        "draft": state.get("final_content") or state.get("draft"),
        "review_result": state.get("review_result"),
        "needs_attention": not state.get("review_result", {}).get("passed", True),
        "allowed_actions": ["approved", "edited", "rejected"],
        "requires_override_reason": not state.get("review_result", {}).get("passed", True),
    })
    action = payload.get("action", "rejected")
    if action not in {"approved", "edited", "rejected"}:
        return {"errors": [f"未知人工决策: {action}"], "status": "blocked"}
    if action == "rejected":
        _record("human", {"decision": action}, {"decision": "rejected"})
        return {"human_decision": "rejected", "status": "rejected"}

    reviewed = state.get("review_result", {})
    override_reason = str(payload.get("override_reason") or "").strip()
    if (
        action == "approved"
        and not reviewed.get("passed", True)
        and not override_reason
    ):
        return {
            "errors": ["合规未通过，人工放行必须填写 override_reason"],
            "status": "blocked",
            "human_decision": "rejected",
        }

    raw_content = payload.get("content") or state.get("draft")
    try:
        final_content = Draft.model_validate(raw_content).model_dump()
    except Exception as exc:
        return {
            "errors": [f"人工提交内容结构无效: {exc}"],
            "status": "blocked",
            "human_decision": "rejected",
        }
    _record("human", {"decision": action},
            {"decision": action, "edited": bool(payload.get("content"))})
    return {
        "human_decision": action,
        "final_content": final_content,
        "human_override_reason": override_reason,
        "status": "reviewing",
    }


def _route_after_human(state):
    if state.get("human_decision") in ("approved", "edited"):
        return "final_check"
    return END


def final_check_node(state):
    """编辑稿强制复审；通过后签发和发布内容绑定的短期审批令牌。"""
    content = state["final_content"]
    action = state.get("human_decision")

    if action == "edited":
        review_result = review_draft(
            content,
            llm,
            (state.get("account") or {}).get("brand_kit"),
        )
        round_no = state.get("review_rounds", 0) + 1
        edit_rounds = state.get("human_edit_rounds", 0) + 1
        review_result["round"] = round_no
        _record(
            "final_check",
            {"title": content["title"], "edited": True},
            {"passed": review_result["passed"], "round": edit_rounds},
            ok=review_result["passed"],
        )
        if not review_result["passed"] and not state.get("human_override_reason"):
            if edit_rounds >= config.MAX_HUMAN_EDIT_ROUNDS:
                return {
                    "review_result": review_result,
                    "review_rounds": round_no,
                    "human_edit_rounds": edit_rounds,
                    "errors": ["人工改稿复核次数已达上限"],
                    "status": "blocked",
                }
            return {
                "review_result": review_result,
                "review_rounds": round_no,
                "human_edit_rounds": edit_rounds,
                "status": "awaiting_human",
            }
    else:
        review_result = state.get("review_result", {})
        edit_rounds = state.get("human_edit_rounds", 0)
        _record(
            "final_check",
            {"title": content["title"], "edited": False},
            {
                "passed": review_result.get("passed", False),
                "override": bool(state.get("human_override_reason")),
            },
            ok=bool(review_result.get("passed") or state.get("human_override_reason")),
        )

    override = bool(state.get("human_override_reason"))
    if review_result.get("passed") or override:
        token = issue_approval_token(
            content, state["account"]["platform"], override=override
        )
        update = {
            "approval_token": token,
            "human_edit_rounds": edit_rounds,
            "status": "approved",
        }
        if action == "edited":
            update.update({
                "review_result": review_result,
                "review_rounds": review_result["round"],
            })
        return update
    return {
        "human_edit_rounds": edit_rounds,
        "errors": ["内容未通过合规检查，且没有人工覆盖理由"],
        "status": "blocked",
    }


def _route_after_final_check(state):
    status = state.get("status")
    if status == "approved":
        return "schedule"
    if status == "awaiting_human":
        return "human"
    return END


# ---------------- 节点 5：排期发布 ----------------

def schedule_node(state):
    content = state["final_content"]
    platform = state["account"]["platform"]
    valid, reason = verify_approval_token(
        state.get("approval_token", ""), content, platform
    )
    if not valid:
        return {
            "errors": [f"排期审批校验失败：{reason}"],
            "status": "blocked",
        }
    slot = suggest_publish_slot(platform)
    result = write_schedule(
        title=content["title"], platform=platform, publish_at=slot,
        author_note=f"objective={state['objective'][:50]}",
        idempotency_key=(
            f"run:{state.get('run_id') or get_recorder().run_id}:"
            f"revision:{state.get('human_edit_rounds', 0)}"
        ),
        body=content.get("body", ""),
        tags=content.get("tags") or [],
    )
    _record("schedule", {"title": content["title"]},
            {"schedule_id": result["schedule_id"], "slot": slot,
             "duplicate": result.get("duplicate", False)})
    return {"schedule": {**result, "approval_token": state["approval_token"]}}


# ---------------- 节点 6：数据复盘 ----------------

def analytics_node(state):
    content = state["final_content"]
    platform = state["account"]["platform"]
    metrics = fetch_metrics(content["title"], platform)
    r = llm.chat([
        {"role": "system", "content": "你是数据复盘分析师。基于 72 小时数据给出结论与下期建议，100字内。"},
        {"role": "user", "content": json.dumps(
            {"title": content["title"], "platform": platform, "metrics": metrics,
             "structure": content.get("structure_note", "")}, ensure_ascii=False)},
    ], purpose="analytics", node="analytics")
    _record("analytics", {"title": content["title"]},
            {"interaction_rate": metrics["interaction_rate"]})
    return {"analytics": {"metrics": metrics, "report": r.content},
            "status": "completed"}


# ---------------- 组装 Graph ----------------

def build_graph(checkpointer=None):
    g = StateGraph(state_schema=ContentOpsState)
    g.add_node("research", research_node)
    g.add_node("write", write_node)
    g.add_node("review", review_node)
    g.add_node("human", human_node)
    g.add_node("final_check", final_check_node)
    g.add_node("schedule", schedule_node)
    g.add_node("analytics", analytics_node)

    g.set_entry_point("research")
    g.add_conditional_edges(
        "research", _route_after_work, {"continue": "write", "end": END}
    )
    g.add_conditional_edges(
        "write", _route_after_work, {"continue": "review", "end": END}
    )
    g.add_conditional_edges("review", _route_after_review, ["human", "write"])
    g.add_conditional_edges("human", _route_after_human, ["final_check", END])
    g.add_conditional_edges(
        "final_check",
        _route_after_final_check,
        {"schedule": "schedule", "human": "human", "end": END},
    )
    g.add_conditional_edges(
        "schedule", _route_after_work, {"continue": "analytics", "end": END}
    )
    g.add_edge("analytics", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


# 预置账号（demo 用）
DEFAULT_ACCOUNT = {
    "name": "小敏的护肤日记", "platform": "小红书",
    "category": "美妆护肤", "persona": "成分党敏感肌博主，语气真诚不夸张",
    "brand_kit": {
        "tone": "专业、真诚、少用夸张形容词",
        "banned_claims": ["根治", "治愈", "全网第一", "零风险"],
        "required_phrases": ["按肤质选择", "先做局部测试"],
        "cta": "收藏并留言分享你的护理困扰",
    },
}


def get_interrupt_payload(graph, cfg) -> object | None:
    """检测工作流是否停在 human 中断节点，返回 interrupt 载荷（无中断返回 None）。

    正确姿势是用 graph.get_state(cfg).next 判断是否有挂起节点，
    再从 tasks[].interrupts[].value 取中断载荷（invoke 的返回值不含它）。
    """
    snap = graph.get_state(cfg)
    if not snap.next:
        return None
    for task in snap.tasks:
        interrupts = getattr(task, "interrupts", None)
        if interrupts:
            return interrupts[0].value
    return {}  # 有挂起节点但取不到载荷时返回空 dict（视为已中断）
