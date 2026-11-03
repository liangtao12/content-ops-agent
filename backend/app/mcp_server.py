"""MCP Server：把合规检测与排期写入能力通过 Model Context Protocol 标准化暴露。

为什么单独做 MCP（面试可讲）：
Agent 工具目前是进程内函数调用；而 MCP 是行业标准的工具互操作协议——
同一个工具服务可以被 Claude Desktop、其他 Agent 客户端、跨团队系统复用。
本项目把最常被外部复用的两个能力（违禁词检测、排期写入）封装成 MCP server，
体现"工具即服务"的工程化思维。

运行: python -m app.mcp_server  (依赖 mcp 包，已包含在 requirements.txt)
测试: 用任意 MCP 客户端连接 stdio，或参考 README 的 inspector 命令。
"""
from .approvals import verify_approval_token
from .llm import get_llm
from .review import review_draft
from .tools import fetch_hot_topics, suggest_publish_slot, write_schedule

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    raise SystemExit(
        "需要安装 MCP 依赖: pip install -r requirements.txt") from e

mcp = FastMCP("content-ops-tools")


@mcp.tool()
def check_violation(title: str, body: str, tags: list[str] | None = None) -> dict:
    """执行与主工作流一致的正则 + LLM 双层合规检查。"""
    return review_draft(
        {"title": title, "body": body, "tags": tags or []},
        get_llm(),
    )


@mcp.tool()
def get_hot_topics(platform: str, category: str = "") -> list[dict]:
    """获取平台热榜话题。"""
    return fetch_hot_topics(platform, category)


@mcp.tool()
def schedule_publish(
    title: str,
    body: str,
    platform: str,
    approval_token: str,
    tags: list[str] | None = None,
    author_note: str = "",
    idempotency_key: str = "",
) -> dict:
    """仅允许已获人工批准的原始内容进入排期；内容变更后令牌自动失效。"""
    content = {"title": title, "body": body, "tags": tags or []}
    valid, reason = verify_approval_token(approval_token, content, platform)
    if not valid:
        raise ValueError(reason)
    if not idempotency_key:
        idempotency_key = f"approval:{approval_token.rsplit('.', 1)[-1]}"
    return write_schedule(
        title,
        platform,
        suggest_publish_slot(platform),
        author_note,
        idempotency_key=idempotency_key,
        body=body,
        tags=tags or [],
    )


if __name__ == "__main__":
    mcp.run()  # stdio 模式
