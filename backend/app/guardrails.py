"""护栏层（Guardrails）：程序化合规检查 + LLM 输出 schema 校验。

为什么护栏要"程序化"而不是全靠 LLM：
1. 违禁词检测是确定性问题，正则/词表快、零成本、可解释、可回归测试；
2. LLM 负责语义级风险评估（比如"内容暗示疗效但没踩违禁词"），
   两者互补——这是 Agent 工程里典型的 deterministic guardrail + LLM judgment 组合。
"""
import json
import re
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from . import config


# ---------- LLM 结构化输出 Schema（输入/输出校验） --------------------

class Candidate(BaseModel):
    title: str
    reason: str
    target_hook: str = ""
    est_score: float = Field(default=0.5, ge=0, le=1)


class CandidateList(BaseModel):
    candidates: list[Candidate]


class Draft(BaseModel):
    title: str
    body: str
    tags: list[str] = []
    structure_note: str = ""


class ReviewOpinion(BaseModel):
    risk: str = Field(pattern="^(low|medium|high)$")
    confidence: float = Field(default=0.5, ge=0, le=1)
    opinion: str = ""


# ---------- 违禁词扫描 -------------------------------------------------

def load_banned_words() -> dict:
    with open(config.BANNED_WORDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def check_text(text: str) -> list[dict]:
    """扫描文本，返回违禁项列表 [{word, category, risk_level, advice, positions}]"""
    hits = []
    for category, rule in load_banned_words().items():
        for word in rule["words"]:
            positions = [m.start() for m in re.finditer(re.escape(word), text)]
            if positions:
                hits.append({
                    "word": word, "category": category,
                    "risk_level": rule["risk_level"], "advice": rule["advice"],
                    "positions": positions,
                })
    return hits


def auto_fix(text: str) -> str:
    """确定性替换高危违禁词（第一道自动修复），保底方案，仍需 LLM 重写。"""
    fixes = {
        "全网第一": "实测口碑前列", "第一品牌": "口碑品牌", "史上最": "备受好评的",
        "根治": "改善", "治愈": "缓解", "包治": "帮助改善", "药到病除": "辅助改善",
        "保本": "注意风险", "稳赚不赔": "存在波动风险", "零风险": "有风险",
        "加微信": "评论区告诉我", "加V": "评论区告诉我",
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    return text


def validate_json(raw: str, schema) -> tuple[Optional[object], Optional[str]]:
    """解析并校验 LLM 的 JSON 输出。返回 (model, error)。"""
    try:
        data = json.loads(raw)
        return schema.model_validate(data), None
    except (json.JSONDecodeError, ValidationError) as e:
        return None, str(e)[:500]
