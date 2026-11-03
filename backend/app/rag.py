"""RAG 检索层：BM25、向量检索与混合检索（平台规范 + 历史爆款案例）。

三路可切换（config.RAG_MODE）：
1. bm25   —— 中文 2-gram 词项匹配。术语密集的短文档（"极限词""报备""蒲公英"）
             精确命中好、零依赖、毫秒级，作为基线。
2. vector —— hashing embedding + 余弦相似度。解决"用户说法和文档用词不一致"
             （问"能不能说绝对安全"要命中"广告法极限词"），可换远程 embedding。
3. hybrid —— BM25 与向量各召回候选，RRF（Reciprocal Rank Fusion）融合，
             再用查询词覆盖率重排。默认值，兼顾精确匹配与语义召回。

面试可讲：为什么不是一开始就上向量库 —— 先用 BM25 建立可复现基线，再用
eval/compare_rag.py 做三路对照，用数据决定默认策略，而不是凭感觉选框架。
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

from . import config
from .vector_store import LocalHashEmbedder, OpenAIEmbedder, VectorIndex


def _tokenize_zh(text: str) -> list[str]:
    """极简中文分词：英文单词 + 2-gram 滑窗。够 BM25 用，无重型依赖。"""
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    zh = re.sub(r"[^\u4e00-\u9fa5]", "", text)
    tokens += [zh[i:i + 2] for i in range(len(zh) - 1)]
    return tokens


class BM25Index:
    def __init__(self, docs: list[dict], text_key: str = "text"):
        self.docs = docs
        self.text_key = text_key
        self.doc_tokens = [_tokenize_zh(d[text_key]) for d in docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avgdl = sum(self.doc_len) / max(1, len(self.doc_len))
        self.df = Counter()
        for tokens in self.doc_tokens:
            for term in set(tokens):
                self.df[term] += 1
        self.N = len(docs)
        self.k1, self.b = 1.5, 0.75

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log((self.N - n + 0.5) / (n + 0.5) + 1.0)

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        q_tokens = _tokenize_zh(query)
        scores = []
        for i, tokens in enumerate(self.doc_tokens):
            tf = Counter(tokens)
            s = 0.0
            for term in q_tokens:
                if term in tf:
                    s += self._idf(term) * tf[term] * (self.k1 + 1) / (
                        tf[term] + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl))
            scores.append(s)
        ranked = sorted(range(self.N), key=lambda i: -scores[i])[:top_k]
        return [{**self.docs[i], "bm25_score": round(scores[i], 3)}
                for i in ranked if scores[i] > 0]


# ---------- 两个知识库索引（模块级懒加载单例） ------------------------

_rules_index: BM25Index | None = None
_cases_index: BM25Index | None = None
_rules_vector_index: VectorIndex | None = None
_cases_vector_index: VectorIndex | None = None
_embedder = None


def _chunk_markdown(path: Path) -> list[dict]:
    """按二级标题切分规范文档，每块带标题元数据。"""
    text = path.read_text(encoding="utf-8")
    chunks, cur_title, cur_lines = [], "前言", []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur_lines:
                chunks.append({"title": cur_title, "text": "\n".join(cur_lines).strip()})
            cur_title, cur_lines = line[3:].strip(), []
        else:
            cur_lines.append(line)
    if cur_lines:
        chunks.append({"title": cur_title, "text": "\n".join(cur_lines).strip()})
    return chunks


def get_rules_index() -> BM25Index:
    global _rules_index
    if _rules_index is None:
        docs = _chunk_markdown(config.RULES_FILE)
        _rules_index = BM25Index(docs)
    return _rules_index


def get_cases_index() -> BM25Index:
    global _cases_index
    if _cases_index is None:
        docs = [json.loads(l) for l in
                config.HIT_CASES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        for d in docs:
            # 检索文本 = 标题+钩子+结构+标签（metrics 不参与检索）
            d["text"] = f"{d['title']} {d['hook']} {d['structure']} {' '.join(d['tags'])} {d['category']}"
        _cases_index = BM25Index(docs)
    return _cases_index


def get_embedder():
    """Embedding 单例。默认本地 hashing；配置 remote 时走 OpenAI 兼容接口。"""
    global _embedder
    if _embedder is None:
        if config.EMBEDDING_PROVIDER == "remote":
            _embedder = OpenAIEmbedder(
                config.EMBEDDING_MODEL,
                base_url=config.EMBEDDING_BASE_URL,
                api_key=config.EMBEDDING_API_KEY,
                dimension=config.EMBEDDING_DIMENSION,
            )
        else:
            _embedder = LocalHashEmbedder(config.EMBEDDING_DIMENSION)
    return _embedder


def get_vector_rules_index() -> VectorIndex:
    global _rules_vector_index
    if _rules_vector_index is None:
        _rules_vector_index = VectorIndex(
            _chunk_markdown(config.RULES_FILE), embedder=get_embedder())
    return _rules_vector_index


def get_vector_cases_index() -> VectorIndex:
    global _cases_vector_index
    if _cases_vector_index is None:
        docs = get_cases_index().docs
        _cases_vector_index = VectorIndex(docs, embedder=get_embedder())
    return _cases_vector_index


# ---------- 混合检索：RRF 融合 + 覆盖率重排 ---------------------------

_RRF_K = 60
_BM25_WEIGHT_MIN = 0.15
_BM25_WEIGHT_MAX = 0.80
_BM25_CONFIDENCE_SCALE = 6.0  # BM25 top1 分数达到该值视为强匹配（经验阈值）

# 领域同义词扩展（query expansion）：中文合规场景里用户口语和规范用词差异很大
# （"天下无敌" vs "极限词/最高级"、"留个联系方式" vs "站外导流"）。
# 给向量路做同义词扩展，等价于让 embedding 具备领域语义泛化；BM25 路保持原样，
# 这样对照实验仍然公平——扩展只补偿本地 hashing embedding 缺的语义能力。
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("报备", "备案", "蒲公英", "官方渠道", "走平台", "商业合作"),
    ("极限词", "绝对化", "最高级", "最佳", "第一", "最", "唯一", "顶级", "天下无敌", "百分百"),
    ("疗效", "根治", "治愈", "包治", "药到病除", "治好", "见效", "承诺"),
    ("导流", "引流", "微信号", "私信", "外链", "淘宝", "联系方式", "加微", "站外"),
    ("搬运", "抄袭", "盗用", "转载", "剪一剪", "原封不动"),
    ("爆款", "高互动", "涨粉", "完播", "钩子", "收藏"),
)


def expand_query(query: str) -> str:
    """命中任一同义词组就把整组词追加进查询，补齐跨词表召回。"""
    expansions: list[str] = []
    for group in _SYNONYM_GROUPS:
        if any(word in query for word in group):
            expansions.extend(word for word in group if word not in query)
    if not expansions:
        return query
    return f"{query} {' '.join(dict.fromkeys(expansions))}"


def resolve_mode(mode: str | None = None) -> str:
    value = (mode or config.RAG_MODE or "hybrid").strip().lower()
    if value not in {"bm25", "vector", "hybrid"}:
        return "hybrid"
    return value


def _doc_key(item: dict) -> str:
    return item.get("id") or item.get("title") or item.get("text", "")[:60]


def _rrf_fuse(result_sets: list[list[dict]], labels: list[str],
              weights: dict[str, float]) -> list[dict]:
    """Reciprocal Rank Fusion：只用排名不用原始分数，避免两路分数量纲不可比。"""
    merged: dict[str, dict] = {}
    sources: dict[str, set[str]] = {}
    for hits, label in zip(result_sets, labels):
        weight = weights.get(label, 1.0)
        for rank, item in enumerate(hits, start=1):
            key = _doc_key(item)
            entry = merged.setdefault(key, {})
            entry.update(item)
            entry["hybrid_score"] = round(
                entry.get("hybrid_score", 0.0) + weight / (_RRF_K + rank), 6)
            sources.setdefault(key, set()).add(label)
    for key, entry in merged.items():
        entry["retrieval_sources"] = sorted(sources[key])
    return sorted(merged.values(), key=lambda item: -item.get("hybrid_score", 0.0))


def adaptive_weights(bm25_hits: list[dict]) -> dict[str, float]:
    """按 BM25 置信度动态分配融合权重。

    BM25 top1 分数高 → 查询与文档词项强匹配（术语型查询），偏向 BM25；
    分数低 / 无命中 → 大概率是跨词表说法，把权重让给向量路。
    固定权重无法同时服务这两类查询，这也是对照评测里最容易踩的坑。
    """
    if not bm25_hits:
        bm25_weight = _BM25_WEIGHT_MIN
    else:
        top_score = float(bm25_hits[0].get("bm25_score") or 0.0)
        confidence = min(1.0, max(0.0, top_score / _BM25_CONFIDENCE_SCALE))
        bm25_weight = _BM25_WEIGHT_MIN + (
            _BM25_WEIGHT_MAX - _BM25_WEIGHT_MIN) * confidence
    bm25_weight = round(bm25_weight, 3)
    return {"bm25": bm25_weight, "vector": round(1.0 - bm25_weight, 3)}


def _coverage(query: str, text: str) -> float:
    """查询词项被文档覆盖的比例（保留为诊断工具，不参与排序）。"""
    query_tokens = set(_tokenize_zh(query))
    if not query_tokens:
        return 0.0
    doc_tokens = set(_tokenize_zh(text))
    return len(query_tokens & doc_tokens) / len(query_tokens)


def _rerank(items: list[dict], top_k: int) -> list[dict]:
    """双路一致性加成：被 BM25 与向量同时召回的文档，等于两条独立信号互相确认。

    为什么不用查询词覆盖率重排：覆盖率本质仍是字面匹配，会把 BM25 的噪声
    再顶一次；一致性信号与两路的匹配方式都正交，才是有效的重排依据。
    """
    for item in items:
        sources = item.get("retrieval_sources") or []
        bonus = 0.25 if len(sources) > 1 else 0.0
        item["rerank_score"] = round(item.get("hybrid_score", 0.0) * (1.0 + bonus), 6)
    ranked = sorted(items, key=lambda item: -item.get("rerank_score", 0.0))
    return ranked[:top_k]


def _search(query: str, top_k: int, mode: str | None,
            bm25_index, vector_index) -> list[dict]:
    selected = resolve_mode(mode)
    if selected == "bm25":
        return bm25_index().retrieve(query, top_k)
    if selected == "vector":
        hits = vector_index().retrieve(expand_query(query), top_k)
        for item in hits:
            item["retrieval_sources"] = ["vector"]
        return hits
    candidates = max(top_k, config.RAG_CANDIDATE_K)
    bm25_hits = bm25_index().retrieve(query, candidates)
    vector_hits = vector_index().retrieve(expand_query(query), candidates)
    fused = _rrf_fuse(
        [bm25_hits, vector_hits], ["bm25", "vector"], adaptive_weights(bm25_hits))
    return _rerank(fused, top_k)


def search_platform_rules(query: str, top_k: int = 3, mode: str | None = None) -> list[dict]:
    """检索平台规范知识库。mode 缺省读 config.RAG_MODE（默认 hybrid）。"""
    return _search(query, top_k, mode, get_rules_index, get_vector_rules_index)


def search_hit_cases(query: str, top_k: int = 3, mode: str | None = None) -> list[dict]:
    """检索历史爆款案例库。mode 缺省读 config.RAG_MODE（默认 hybrid）。"""
    return _search(query, top_k, mode, get_cases_index, get_vector_cases_index)
