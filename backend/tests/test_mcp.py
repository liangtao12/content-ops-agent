import pytest

from app.approvals import issue_approval_token
from app.mcp_server import check_violation, get_hot_topics, schedule_publish


def test_mcp_compliance_uses_both_layers():
    result = check_violation("福利", "想要优惠可以加微信", ["护肤"])
    assert result["passed"] is False
    assert result["violations"]


def test_mcp_schedule_rejects_unapproved_content():
    with pytest.raises(ValueError, match="审批令牌"):
        schedule_publish(
            title="未审批内容",
            body="正文",
            platform="小红书",
            approval_token="invalid",
        )


def test_mcp_schedule_accepts_matching_approval_without_duplicate():
    content = {"title": "已审批标题", "body": "已审批正文", "tags": ["护肤"]}
    token = issue_approval_token(content, "小红书")
    first = schedule_publish(
        title=content["title"],
        body=content["body"],
        platform="小红书",
        approval_token=token,
        tags=content["tags"],
        idempotency_key="mcp-same-request",
    )
    second = schedule_publish(
        title=content["title"],
        body=content["body"],
        platform="小红书",
        approval_token=token,
        tags=content["tags"],
        idempotency_key="mcp-same-request",
    )
    assert first["status"] == "scheduled"
    assert first["schedule_id"] == second["schedule_id"]
    assert second["duplicate"] is True


def test_mcp_hot_topics_tool_is_reachable():
    topics = get_hot_topics("小红书", "美妆护肤")
    assert topics and topics[0]["category"] == "美妆护肤"
