"""LLM 访问层：统一封装 DeepSeek(OpenAI 兼容协议)，支持 Mock 降级。

设计要点（面试可讲）：
1. 面向接口编程：BaseLLM 只暴露 chat()，上层工作流不关心是真是 Mock，
   这是"依赖倒置"，让评测流水线在离线环境也能跑。
2. JSON 结构化输出 + purpose 标记：Agent 每个节点声明自己的用途，
   Mock 模式据此返回确定性的响应，测试可复现。
3. 统一做 token 用量统计，供可观测性层记录成本。
"""
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config
from .tracing import get_recorder


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list = field(default_factory=list)  # [{name, arguments}]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    mock: bool = False


class BaseLLM(ABC):
    @abstractmethod
    def chat(self, messages: list, tools: Optional[list] = None,
             purpose: str = "", node: str = "") -> ChatResult:
        ...


class DeepSeekLLM(BaseLLM):
    """DeepSeek Chat API（OpenAI 兼容协议，tool calling 原生支持）。"""

    def __init__(self):
        from openai import OpenAI  # 延迟导入，Mock 模式无需安装后仍可导入失败时报错清晰
        self.client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
        )
        self.model = config.DEEPSEEK_MODEL

    def chat(self, messages, tools=None, purpose="", node="") -> ChatResult:
        t0 = time.time()
        kwargs = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            kwargs["tools"] = tools
        if purpose in ("research_select", "write", "review_assess"):
            # DeepSeek 支持 json_object 输出模式，降低解析失败率
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        usage = resp.usage
        result = ChatResult(
            content=msg.content or "",
            tool_calls=[
                {"name": tc.function.name,
                 "arguments": self._parse_arguments(tc.function.arguments)}
                for tc in (msg.tool_calls or [])
            ],
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_ms=int((time.time() - t0) * 1000),
        )
        get_recorder().record_llm(node, purpose, result)
        return result

    @staticmethod
    def _parse_arguments(raw: str | None) -> dict:
        try:
            value = json.loads(raw or "{}")
            return value if isinstance(value, dict) else {"invalid_arguments": value}
        except json.JSONDecodeError:
            return {"invalid_json": raw or ""}


# ---------- Mock 素材：按账号垂类产出，而不是全局写死一段文案 ----------
# 早期 Mock 只返回一段护肤文案，导致"换个选题还是同一篇烂脸文"，演示会当场穿帮：
# 用户填美食目标，Agent 却输出敏感肌内容，等于告诉对方这是假流程。
# 这里按垂类各备一套素材，既保留确定性（可回归），又让 Mock 像真的读过账号定位。

_WRITE_TEMPLATES: dict[str, dict] = {
    "美妆护肤": {
        "candidate_suffix": "从烂脸到稳定只用 2 周",
        "title": "敏感肌换季自救｜2 周稳定烂脸实测",
        "body": ("敏感肌换季烂脸别慌，实测 2 周屏障修复方案：\n"
                 "1. 早上只用温水+保湿霜，砍掉所有功效步骤\n"
                 "2. 晚上洁面后神经酰胺精华局部厚敷\n"
                 "3. 一周后逐步加回防晒\n评论区告诉我你的烂脸经历~"),
        "tags": ["敏感肌", "屏障修复", "平价护肤"],
        "structure_note": "痛点共情→分点方案→互动引导",
    },
    "美食": {
        "candidate_suffix": "一周 100 元搞定三餐",
        "title": "打工人一周备餐｜100 元吃好五天",
        "body": ("一周备餐按这个顺序来，周日两小时能备好五天午饭：\n"
                 "1. 先定主食：糙米和燕麦一次煮好，分装冷冻，吃前彻底加热\n"
                 "2. 蛋白质提前腌：鸡胸、鸡蛋、豆腐分三份，冷藏三天内吃完\n"
                 "3. 蔬菜现切现炒，绿叶菜不留到第二天\n"
                 "4. 肠胃敏感的人先少量尝试新搭配\n先列清单再采购，一周预算控制在 100 元以内。"),
        "tags": ["备餐", "打工人", "省钱攻略"],
        "structure_note": "成本锚点→分步流程→保存技巧",
    },
    "家居": {
        "candidate_suffix": "500 元清单不动墙",
        "title": "出租屋改造｜500 元换个客厅",
        "body": ("出租屋改造不用动墙，500 元就能换个样子：\n"
                 "1. 先换光源：暖光落地灯加可调色温灯泡，晚上不刺眼\n"
                 "2. 收纳用同色系收纳盒，视线里的杂物先藏起来\n"
                 "3. 墙面用免钉挂钩和挂画轨道，退租时能整片取下\n"
                 "4. 软装加一块地毯和两个抱枕，空间马上有层次\n所有改动都以不破坏墙面为前提，退租能恢复原样。"),
        "tags": ["出租屋改造", "低成本", "收纳"],
        "structure_note": "前后对比→分区域清单→退租友好",
    },
    "运动健身": {
        "candidate_suffix": "30 天打卡记录",
        "title": "圆肩驼背 30 天记录｜每天 10 分钟",
        "body": ("圆肩驼背 30 天记录，每天 10 分钟，动作都很简单：\n"
                 "1. 靠墙天使 15 次 × 2 组，肩胛骨贴墙慢慢滑动\n"
                 "2. 弹力带划船 12 次 × 3 组，肩膀下沉别耸肩\n"
                 "3. 胸小肌拉伸 30 秒 × 2 侧，过程中别憋气\n"
                 "4. 睡前靠墙站 3 分钟，找背部发力的感觉\n前两周先练动作质量，别急着加负重。"),
        "tags": ["体态改善", "居家训练", "打卡"],
        "structure_note": "体态自测→每日动作→进度记录",
    },
    "穿搭": {
        "candidate_suffix": "5 套搭配直接抄",
        "title": "155 小个子通勤穿搭｜5 套直接抄",
        "body": ("155 小个子通勤穿搭，5 套搭配直接抄：\n"
                 "1. 短上衣配高腰直筒裤，腰线提上去，比例立刻变\n"
                 "2. 同色系叠穿，深浅差一档，视觉上更连贯\n"
                 "3. 外套长度卡在胯骨上方，别盖过大腿根\n"
                 "4. 鞋子和裤子同色，腿部线条不断开\n"
                 "5. 配饰只留一处亮点，包和耳饰别同时抢眼"),
        "tags": ["小个子穿搭", "通勤穿搭", "基本款"],
        "structure_note": "痛点共鸣→分套搭配→单品思路",
    },
    "职场": {
        "candidate_suffix": "3 个模板直接套",
        "title": "工作汇报这样讲｜领导终于记住我了",
        "body": ("工作汇报这样讲，领导终于记住我了：\n"
                 "1. 先给结论：一句话说清这次要解决什么问题\n"
                 "2. 再给依据：数据和影响范围，别按时间线记流水账\n"
                 "3. 最后给选项：A/B 两个方案，各自代价写清楚\n"
                 "4. 结尾留一句：需要您拍板的是哪一项\n汇报前先写三行提纲，整体控制在 5 分钟内。"),
        "tags": ["职场沟通", "汇报", "结构化表达"],
        "structure_note": "错误示范→结构化模板→实例对比",
    },
    "旅行": {
        "candidate_suffix": "本地人才知道的 3 个地方",
        "title": "小城旅行｜本地人才知道的 3 个地方",
        "body": ("小城旅行，本地人才知道的 3 个地方：\n"
                 "1. 早上 7 点的老菜市场，比景区小吃街真实\n"
                 "2. 城郊的旧铁路线，日落前光线柔和\n"
                 "3. 图书馆顶楼，能拍到整片老城区屋顶\n出行前查好公交末班时间，晚上别走没有路灯的小路。"),
        "tags": ["小城旅行", "冷门景点", "攻略"],
        "structure_note": "反差钩子→分点地点→实用提醒",
    },
    "通用": {
        "candidate_suffix": "一次讲清",
        "title": "先把问题拆开｜3 个可照做的步骤",
        "body": ("把目标拆成三步，按顺序推进就不容易乱：\n"
                 "1. 明确目标：这次要解决的具体问题是什么，怎么判断做成了\n"
                 "2. 收集依据：找 2-3 条可验证的信息，别只凭感觉\n"
                 "3. 小范围试：先跑一轮看结果，再决定是否放大\n每一步都记下变化，方便下一轮复盘。"),
        "tags": ["方法论", "实操", "复盘"],
        "structure_note": "目标澄清→依据收集→小步验证",
    },
}

# 关键词只用于兜底：正常链路的 system 消息里已带"垂类：X"，优先用显式值。
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "美食": ("餐", "食", "吃", "做饭", "厨", "菜"),
    "家居": ("出租屋", "家居", "收纳", "改造", "装修", "房间"),
    "运动健身": ("健身", "体态", "运动", "训练", "减脂", "增肌", "打卡"),
    "穿搭": ("穿搭", "通勤", "小个子", "叠穿", "搭配"),
    "职场": ("职场", "汇报", "沟通", "面试", "简历", "领导"),
    "旅行": ("旅行", "景点", "攻略", "citywalk", "出游"),
    "美妆护肤": ("护肤", "敏感肌", "精华", "彩妆", "妆容", "面膜", "底妆", "防晒"),
}


def _infer_category(text: str) -> str:
    """按命中关键词数量选垂类，避免"第一个匹配"导致的误判。"""
    best, best_score = "通用", 0
    for category, words in _CATEGORY_KEYWORDS.items():
        score = sum(1 for word in words if word in text)
        if score > best_score:
            best, best_score = category, score
    return best


def _parse_account(messages: list) -> tuple[str, str]:
    """从 system 消息解析平台与垂类。解析不到返回空串，由调用方兜底。"""
    blob = "\n".join(
        m.get("content") or "" for m in messages if m.get("role") == "system")
    platform = re.search(r"平台[：:]\s*([^\s，,。]+)", blob)
    category = re.search(r"垂类[：:]\s*([^\s，,。]+)", blob)
    return (
        platform.group(1) if platform else "",
        category.group(1) if category else "",
    )


def _template_for(category: str) -> dict:
    return _WRITE_TEMPLATES.get(category) or _WRITE_TEMPLATES["通用"]


def _clean_topic(objective: str) -> str:
    """去掉评测/演示指令残留，别让"测试人工拒绝""[注入极限词]"混进标题。"""
    cleaned = re.sub(r"\[注入[^\]]*\]", "", objective)
    cleaned = re.sub(r"[，,]\s*测试[^，,。]*$", "", cleaned)
    return cleaned.strip() or "秋冬敏感肌自救指南"


class MockLLM(BaseLLM):
    """离线确定性 Mock：按 purpose 返回可用响应，覆盖全流程节点。

    真实价值：让 graph / eval / 单测在无 Key、无网络环境可回归，
    这也是 CI 里跑 Agent 测试的标准做法（不打真实 API）。

    注意：素材按账号垂类分套（_WRITE_TEMPLATES），不是全局写死一篇。
    同一垂类内仍然完全确定，这样评测基线可复现；跨垂类才有内容差异。
    """

    mock = True

    def chat(self, messages, tools=None, purpose="", node="") -> ChatResult:
        t0 = time.time()
        # 从用户消息里解析业务上下文（测试用例的 objective 会传进来）
        objective = ""
        account_desc = ""
        for m in messages:
            if m["role"] == "system" and "账号" in m["content"]:
                account_desc = m["content"]
            if m["role"] == "user":
                # 优先取"运营目标：xxx"里的真实目标，避免把节点内的指令 prompt 当成业务目标
                matched = re.search(r"运营目标[:：]\s*([^\n。]+)", m["content"])
                if matched:
                    objective = matched.group(1).strip()
                elif not objective:
                    objective = m["content"]

        if purpose == "research_tools":
            # 工具参数取账号的真实平台/垂类，不再硬编码小红书+美妆护肤
            platform, category = _parse_account(messages)
            platform = platform or "小红书"
            tools_category = category or _infer_category(objective)
            if tools_category == "通用":
                tools_category = "美妆护肤"  # 热榜数据只有具体垂类，兜底用默认垂类
            calls = [
                {"name": "fetch_hot_topics",
                 "arguments": {"platform": platform, "category": tools_category}},
                {"name": "search_hit_cases",
                 "arguments": {"query": objective or tools_category, "top_k": 3}},
            ]
            r = ChatResult(content="", tool_calls=calls, mock=True)
        elif purpose == "research_select":
            _, category = _parse_account(messages)
            template = _template_for(category or _infer_category(objective))
            topic = _clean_topic(objective) if objective else "秋冬敏感肌自救指南"
            r = ChatResult(content=json.dumps({
                "candidates": [
                    {"title": f"{topic}｜{template['candidate_suffix']}",
                     "reason": "热榜 rising 话题 + 账号垂类强相关，历史同类爆款 3 篇",
                     "target_hook": "人群锚点+数字钩子", "est_score": 0.86},
                    {"title": f"{topic}的 3 个真相，别急着下手",
                     "reason": "反常识角度，横评类历史数据好", "target_hook": "反常识", "est_score": 0.74},
                    {"title": f"低成本清单：{topic}一次配齐",
                     "reason": "预算数字钩子，收藏率高", "target_hook": "低预算", "est_score": 0.71},
                ]
            }, ensure_ascii=False), mock=True)
        elif purpose == "write":
            # 评测注入用：objective 带 [注入极限词] 标记时，模拟模型写出违规文案
            inject = "[注入极限词]" in objective or "[注入导流词]" in objective
            _, category = _parse_account(messages)
            template = _template_for(category or _infer_category(objective))
            body = template["body"]
            if inject:
                # 注入句与垂类无关，评测需要稳定命中极限词/导流词
                body += "\n这款产品是全网第一好用的，根治问题，加微信 cheap_123 私聊购买"
            r = ChatResult(content=json.dumps({
                "title": template["title"],
                "body": body,
                "tags": list(template["tags"]),
                "structure_note": template["structure_note"],
            }, ensure_ascii=False), mock=True)
        elif purpose == "review_assess":
            # 真实审查由程序化 guardrails 先行，LLM 只负责风险评估意见
            r = ChatResult(content=json.dumps({
                "risk": "low", "confidence": 0.9,
                "opinion": "程序化违禁词扫描为主，人工确认兜底",
            }, ensure_ascii=False), mock=True)
        elif purpose == "analytics":
            r = ChatResult(content=(
                "复盘结论：本期选题命中热榜 rising 话题，完播/收藏达标；"
                "互动率高于账号均值 23%；建议下期沿用『人群锚点+时间数字』标题结构，"
                "并在发布 2 小时后追加评论区置顶互动。"), mock=True)
        else:
            r = ChatResult(content=f"[mock] purpose={purpose}", mock=True)
        r.latency_ms = max(1, int((time.time() - t0) * 1000))
        get_recorder().record_llm(node, purpose, r)
        return r


def get_llm() -> BaseLLM:
    if config.USE_MOCK:
        return MockLLM()
    # 已配置 API Key 时不静默降级：静默 Mock 会让生产故障看起来“成功”。
    return DeepSeekLLM()
