"""LLM-as-Judge：对最终内容做质量评分（相关性/合规性/结构/吸引力，各 1-5 分）。

设计（面试可讲）：
1. Judge 与 Creator 用同一模型的隐患是"自我偏好"，生产做法是换模型/换提示词角色；
   本项目在 README 里说明了升级路径（如 judge 用 deepseek-reasoner）。
2. Mock 模式用启发式评分（关键词重合+结构特征），保证评测离线可回归。
3. 评分输出结构化 JSON，可直接进报告与回归对比。
"""
import json

from app.llm import get_llm

JUDGE_PROMPT = """你是严格的内容质量评委，对小红书笔记评分（1-5，整数）。
维度：relevance(与运营目标相关性) compliance(合规性) structure(结构完整性) appeal(吸引力)。
评分标准从严：3 分代表及格，5 分代表专业水准。
输出JSON: {{"relevance":n,"compliance":n,"structure":n,"appeal":n,"comment":"一句话理由"}}

运营目标：{objective}
内容标题：{title}
内容正文：{body}"""


def heuristic_score(objective: str, content: dict, violations: list) -> dict:
    """Mock/降级评分：确定性启发式，用于离线回归。"""
    text = content.get("title", "") + content.get("body", "")
    obj_tokens = [w for w in objective if "\u4e00" <= w <= "\u9fa5"]
    overlap = sum(1 for t in set(obj_tokens) if t in text)
    relevance = min(5, 3 + (1 if overlap > 8 else 0))
    compliance = 3 if violations else 5
    structure = 4 if content.get("tags") and len(content.get("body", "")) > 100 else 3
    appeal = 4 if any(k in text for k in ["实测", "清单", "攻略", "自救", "别"]) else 3
    return {"relevance": relevance, "compliance": compliance,
            "structure": structure, "appeal": appeal,
            "comment": "heuristic(mock) judge"}


def judge_content(objective: str, content: dict, violations: list) -> dict:
    llm = get_llm()
    if getattr(llm, "mock", False) or not content:
        return heuristic_score(objective, content, violations)
    r = llm.chat([
        {"role": "system", "content": "你是内容质量评委，只输出JSON。"},
        {"role": "user", "content": JUDGE_PROMPT.format(
            objective=objective, title=content.get("title", ""),
            body=content.get("body", "")[:2000])},
    ], purpose="judge", node="eval")
    try:
        return json.loads(r.content)
    except json.JSONDecodeError:
        return heuristic_score(objective, content, violations)
