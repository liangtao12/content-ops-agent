"""工作流状态定义：LangGraph 的共享 State，所有节点读写它。

设计（面试可讲）：用 TypedDict 而不是 dataclass，与 LangGraph 的
state 通道机制兼容；total=False 让节点只更新自己负责的字段，
LangGraph 会做浅合并，各节点互不覆盖。
"""
from typing import TypedDict


class ContentOpsState(TypedDict, total=False):
    # 输入
    run_id: str            # 本次运行 ID，用于 tracing、幂等和审计
    account: dict          # 账号定位 {name, platform, category, persona}
    objective: str         # 本周运营目标（自然语言）

    # 选题研究员产出
    candidates: list       # 候选选题 [{title, reason, target_hook, est_score}]
    selected_topic: dict   # 最终选题（人工或规则择优）
    research_evidence: list  # 热榜与爆款案例检索证据，供运营追溯

    # 创作产出 / 审核状态
    draft: dict            # {title, body, tags, structure_note}
    review_result: dict    # {passed, violations, llm_risk, advice, round}
    review_rounds: int

    # 人工确认（interrupt 恢复时写入）
    human_decision: str    # approved / edited / rejected
    human_override_reason: str
    human_edit_rounds: int
    final_content: dict
    approval_token: str

    # 发布与复盘
    schedule: dict
    analytics: dict

    # 全局
    errors: list
    status: str            # running / awaiting_human / completed / rejected / blocked
