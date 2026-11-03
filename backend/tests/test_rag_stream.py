"""RAG 三路检索与 SSE 流式接口的回归测试（离线 Mock，无需 API Key）。

这两块是简历上最容易讲不清、也最容易改坏的地方：
- 检索：默认模式、权重自适应、同义词扩展一旦改动，排序就会漂移 → 断言不锁死具体
  文档顺序（脆弱），只锁"必须召回正确答案"这类不变量。
- 流式：事件名与顺序是前端契约 → 锁住 start/node/awaiting_human/done 的语义。
"""
import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import app.server as server
from app import config
from app.graph import build_graph
from app.rag import (adaptive_weights, expand_query, resolve_mode,
                     search_hit_cases, search_platform_rules)
from app.vector_store import LocalHashEmbedder, VectorIndex, cosine_similarity


# ---------- 检索层 ----------------------------------------------------

def test_resolve_mode_normalizes_and_falls_back():
    assert resolve_mode("BM25") == "bm25"
    assert resolve_mode(" vector ") == "vector"
    assert resolve_mode("hybrid") == "hybrid"
    # 非法值不能抛异常，否则一个错的环境变量能让整个服务起不来
    assert resolve_mode("faiss") == "hybrid"
    assert resolve_mode(None) == config.RAG_MODE


def test_expand_query_adds_domain_synonyms():
    expanded = expand_query("能不能说天下无敌")
    assert "极限词" in expanded and "绝对化" in expanded
    # 原查询必须保留，扩展是叠加而不是替换
    assert expanded.startswith("能不能说天下无敌")
    # 无同义词命中时保持原样，避免无谓的查询膨胀
    assert expand_query("今天天气如何") == "今天天气如何"


def test_every_mode_retrieves_relevant_rules():
    for mode in ("bm25", "vector", "hybrid"):
        results = search_platform_rules("广告法极限词 第一 最佳", top_k=2, mode=mode)
        assert results, f"{mode} 模式没有召回任何规范"
        assert any("极限词" in r["title"] or "广告法" in r["title"] for r in results), mode


def test_every_mode_retrieves_relevant_cases():
    for mode in ("bm25", "vector", "hybrid"):
        results = search_hit_cases("敏感肌 护肤", top_k=3, mode=mode)
        assert results, f"{mode} 模式没有召回任何案例"
        assert any("敏感肌" in r["title"] for r in results), mode


def test_hybrid_tags_retrieval_sources():
    results = search_platform_rules("蒲公英 报备", top_k=3, mode="hybrid")
    assert results
    sources = results[0]["retrieval_sources"]
    assert set(sources) <= {"bm25", "vector"}
    assert len(sources) >= 1
    # 双路一致性是有意保留的重排信号，至少应有一条命中同时来自两路
    assert any(len(r["retrieval_sources"]) > 1 for r in results)


def test_adaptive_weights_follow_bm25_confidence():
    mean_weight = adaptive_weights([])["bm25"]
    strong_weight = adaptive_weights([{"bm25_score": 6.0}])["bm25"]
    weak_weight = adaptive_weights([{"bm25_score": 0.5}])["bm25"]

    assert mean_weight == pytest.approx(0.15)
    assert strong_weight == pytest.approx(0.80)
    assert mean_weight < weak_weight < strong_weight
    weights = adaptive_weights([{"bm25_score": 3.0}])
    assert weights["bm25"] + weights["vector"] == pytest.approx(1.0)


def test_local_embedder_is_deterministic_and_normalized():
    embedder = LocalHashEmbedder(128)
    first = embedder.embed("广告法极限词")
    second = embedder.embed("广告法极限词")
    assert first == second, "同一文本必须得到同一向量，否则评测不可复现"
    assert len(first) == 128
    assert sum(v * v for v in first) == pytest.approx(1.0, abs=1e-6)
    assert embedder.embed("敏感肌护肤") != first


def test_vector_index_ranks_by_similarity():
    docs = [
        {"id": "a", "text": "广告法极限词 最高级 绝对化用语 不得使用"},
        {"id": "b", "text": "敏感肌护肤 温和清洁 屏障修复"},
    ]
    index = VectorIndex(docs, embedder=LocalHashEmbedder(256))
    hits = index.retrieve("极限词 绝对化用语", top_k=1)
    assert hits[0]["id"] == "a"
    assert 0.0 < hits[0]["vector_score"] <= 1.0


def test_cosine_similarity_edge_cases():
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# ---------- SSE 流式接口 ----------------------------------------------

def _client(monkeypatch):
    monkeypatch.setattr(server, "graph", build_graph(MemorySaver()))
    client = TestClient(server.app)
    response = client.post("/api/auth/register", json={
        "username": "streamer",
        "display_name": "流式用户",
        "password": "StreamPass123",
    })
    assert response.status_code == 200
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


def _collect_events(client, payload):
    events = []
    with client.stream("POST", "/api/run/stream", json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        current = None
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("event: "):
                current = line[len("event: "):].strip()
            elif line.startswith("data: "):
                events.append((current, json.loads(line[len("data: "):])))
    return events


def test_stream_emits_start_node_and_done(monkeypatch):
    client = _client(monkeypatch)
    events = _collect_events(client, {"objective": "秋冬敏感肌选题，目标涨粉"})
    names = [name for name, _ in events]

    assert names[0] == "start"
    assert names[-1] == "done"
    nodes = [payload["node"] for name, payload in events if name == "node"]
    # 人工确认节点是 interrupt 点，不会以 node 事件出现，进度停在 review 之后
    assert nodes == ["research", "write", "review"]

    for name, payload in events:
        if name == "node":
            assert payload["summary"], f"{payload['node']} 节点未推送摘要"
    done = dict(events)["done"]
    assert done["status"] == "awaiting_human"
    assert done["pending"], "中断点必须由 done 携带 pending，前端才能渲染审批"
    assert done["thread_id"] == events[0][1]["thread_id"]


def test_stream_requires_login(monkeypatch):
    monkeypatch.setattr(server, "graph", build_graph(MemorySaver()))
    client = TestClient(server.app)
    response = client.post("/api/run/stream", json={"objective": "未登录调用"})
    assert response.status_code == 401
