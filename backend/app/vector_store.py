"""向量检索层：可插拔 Embedding + 余弦相似度索引。

设计要点（面试可讲）：
1. 本地默认用 signed hashing trick（blake2b 稳定哈希）把中文 2-gram / 英文词
   映射到固定维度，无需下载模型、无需联网，结果确定可复现 —— 保证 CI 和
   Mock 模式能跑检索回归。
2. Embedder 是 Protocol 接口：换成 OpenAI / bge / 任意向量模型只需实现 embed()，
   索引与检索逻辑完全不变，这就是"RAG 可升级"的具体形态。
3. 向量库同理：这里用内存数组 + 余弦相似度，文档量级（几十~几千块）下足够；
   换 FAISS / Chroma 只需替换 VectorIndex 内部实现。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Protocol


class Embedder(Protocol):
    """向量化接口：任何返回等长稠密向量的实现都可接入。"""

    dimension: int

    def embed(self, text: str) -> list[float]: ...


def _features(text: str) -> list[str]:
    """中文 2-gram + 单字 + 英文数字词，兼顾短查询和长文档。"""
    lowered = text.lower()
    tokens = re.findall(r"[a-zA-Z0-9]+", lowered)
    zh = re.sub(r"[^\u4e00-\u9fa5]", "", lowered)
    tokens += [zh[i:i + 2] for i in range(len(zh) - 1)]
    tokens += list(zh)
    return tokens


class LocalHashEmbedder:
    """零依赖本地 Embedding：signed hashing + 亚线性 TF 权重 + L2 归一化。"""

    def __init__(self, dimension: int = 384):
        if dimension <= 0:
            raise ValueError("dimension 必须为正整数")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        counts = Counter(_features(text))
        for feature, count in counts.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            hashed = int.from_bytes(digest, "big")
            index = hashed % self.dimension
            sign = 1.0 if (hashed >> 63) & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class OpenAIEmbedder:
    """远程 Embedding（OpenAI 兼容 /v1/embeddings）。默认不启用，需显式配置。"""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "",
        api_key: str = "",
        dimension: int = 1024,
    ):
        from openai import OpenAI

        self.model = model
        self.dimension = dimension
        self.client = OpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url or None,
        )

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(model=self.model, input=text)
        vector = list(response.data[0].embedding)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """输入已 L2 归一化，点积即余弦相似度。"""
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right))


class VectorIndex:
    """内存向量索引：暴力余弦检索，文档量小的时候比引入向量库更快更稳。"""

    def __init__(self, docs: list[dict], text_key: str = "text", embedder: Embedder | None = None):
        self.docs = docs
        self.text_key = text_key
        self.embedder = embedder or LocalHashEmbedder()
        self.vectors = [self.embedder.embed(doc[text_key]) for doc in docs]

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        query_vector = self.embedder.embed(query)
        scored = [
            (index, cosine_similarity(query_vector, vector))
            for index, vector in enumerate(self.vectors)
        ]
        ranked = sorted(scored, key=lambda item: -item[1])[:top_k]
        return [
            {**self.docs[index], "vector_score": round(score, 6)}
            for index, score in ranked
            if score > 0
        ]
