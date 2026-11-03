"""单元测试：护栏层（无需 API Key，纯程序化，秒级）。"""
import pytest

from app.guardrails import auto_fix, check_text, validate_json
from app.guardrails import CandidateList


def test_catch_ad_extreme_words():
    hits = check_text("这款精华是全网第一好用的，根治敏感肌")
    words = [h["word"] for h in hits]
    assert "全网第一" in words
    assert "根治" in words
    assert all(h["risk_level"] == "high" for h in hits)  # 广告/医疗都是高危


def test_catch_finance_words():
    hits = check_text("这个项目保本零风险，稳赚不赔")
    words = [h["word"] for h in hits]
    assert "保本" in words and "稳赚不赔" in words and "零风险" in words


def test_catch_diversion_words_medium_risk():
    hits = check_text("想要同款加微信私聊")
    assert any(h["word"] == "加微信" and h["risk_level"] == "medium" for h in hits)


def test_clean_text_passes():
    assert check_text("实测两周屏障修复，成分党可以冲") == []


def test_positions_reported():
    hits = check_text("全网第一好用，真的全网第一")
    hit = next(h for h in hits if h["word"] == "全网第一")
    assert len(hit["positions"]) == 2  # 位置信息用于高亮展示


def test_auto_fix_replaces_high_risk_words():
    fixed = auto_fix("全网第一，根治，加微信")
    assert "全网第一" not in fixed and "根治" not in fixed and "加微信" not in fixed


def test_json_schema_validation():
    ok, err = validate_json('{"candidates":[{"title":"t","reason":"r","est_score":0.9}]}', CandidateList)
    assert ok is not None and len(ok.candidates) == 1
    bad, err = validate_json('{"candidates": "not a list"}', CandidateList)
    assert bad is None and err
