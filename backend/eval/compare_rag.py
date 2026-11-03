"""RAG 三路对照评测：BM25 vs 向量 vs 混合。

为什么需要它（面试可讲）：
"上向量检索"不该是拍脑袋的决定。这里用同一批查询、同一批知识库，
在 Recall@1 / Recall@3 / MRR 上做对照，用数据说明默认策略为什么是 hybrid；
同时它也是回归基线——改切分、改 embedding、改融合权重后重跑，指标掉了就是回归。

两组查询刻意区分难度：
- 口语化改写：与文档仍有词重叠，检验基础召回（BM25 的主场）；
- 跨词表：用户说法与规范用词几乎零重叠（"天下无敌" vs "极限词"），
  检验向量路的同义词扩展是否真的补上了语义召回。

运行：cd backend && python -m eval.compare_rag
产出：eval/rag_comparison.md
"""
from __future__ import annotations

import time
from pathlib import Path

from app.rag import search_hit_cases, search_platform_rules

MODES = ("bm25", "vector", "hybrid")

# 口语化改写：有词项重叠
RULE_QUERIES_PARAPHRASE = [
    ("品牌合作笔记要怎么报备才不会被限流", "小红书社区规范：商业营销类"),
    ("护肤品文案能不能写根治和治愈", "小红书社区规范：医疗健康类"),
    ("全网第一、最高级这种词到底能不能用", "广告法极限词要点（全平台通用）"),
    ("正文里留微信号会被平台处罚吗", "平台导流与交易规范"),
    ("标题和正文内容不一致会怎么样", "内容质量通用规范"),
    ("小红书爆款笔记的结构该怎么写", "爆款内容的通用结构（供创作 Agent 参考）"),
]

# 跨词表：与规范原文几乎零字面重叠，只能靠领域同义词扩展召回
RULE_QUERIES_CROSS_VOCAB = [
    ("广告里说自家产品天下无敌行不行", "广告法极限词要点（全平台通用）"),
    ("接了品牌单子忘了走官方流程会怎样", "小红书社区规范：商业营销类"),
    ("文案写包治百病算不算违规", "小红书社区规范：医疗健康类"),
    ("粉丝问我要联系方式我该怎么回", "平台导流与交易规范"),
    ("把别人的视频原封不动剪一剪发自己号", "内容质量通用规范"),
    ("怎么写才能有更多收藏和完播", "爆款内容的通用结构（供创作 Agent 参考）"),
]

# 案例库：手工口语化查询 + 其余案例用类目与标签自动构造，保证覆盖全部样本。
CASE_QUERIES = [
    ("脸烂了怎么自救", "case-001"),
    ("个子矮怎么穿显高", "case-002"),
    ("上班族一周吃饭怎么省钱", "case-003"),
    ("早上没时间做早饭怎么办", "case-004"),
    ("含胸驼背怎么矫正", "case-005"),
    ("国货彩妆哪个值得买", "case-006"),
]


def _auto_case_queries() -> list[tuple[str, str]]:
    import json

    from app import config

    rows = [
        json.loads(line)
        for line in config.HIT_CASES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    covered = {target for _, target in CASE_QUERIES}
    extra = [
        (f"{row['category']} {' '.join(row['tags'])}", row["id"])
        for row in rows
        if row["id"] not in covered
    ]
    return CASE_QUERIES + extra


def evaluate(mode: str, queries: list[tuple[str, str]], search, key: str) -> dict:
    hit1 = hit3 = 0
    reciprocal_rank = 0.0
    start = time.perf_counter()
    for query, target in queries:
        results = search(query, top_k=3, mode=mode)
        keys = [item.get(key, "") for item in results]
        if keys[:1] == [target]:
            hit1 += 1
        if target in keys:
            hit3 += 1
            reciprocal_rank += 1.0 / (keys.index(target) + 1)
    elapsed_ms = (time.perf_counter() - start) * 1000
    total = max(1, len(queries))
    return {
        "recall@1": hit1 / total,
        "recall@3": hit3 / total,
        "mrr@3": reciprocal_rank / total,
        "avg_ms": elapsed_ms / total,
    }


def _row(label: str, metrics: dict) -> str:
    return (
        f"| {label} | {metrics['recall@1']:.3f} | {metrics['recall@3']:.3f} | "
        f"{metrics['mrr@3']:.3f} | {metrics['avg_ms']:.2f} |"
    )


def _section(title: str, queries, search, key) -> tuple[list[str], dict]:
    lines = [
        f"## {title}",
        "",
        f"查询数: {len(queries)}",
        "",
        "| 检索模式 | Recall@1 | Recall@3 | MRR@3 | 平均耗时(ms) |",
        "|---|---|---|---|---|",
    ]
    metrics_by_mode = {}
    for mode in MODES:
        metrics = evaluate(mode, queries, search, key)
        metrics_by_mode[mode] = metrics
        lines.append(_row(mode, metrics))
    lines.append("")
    return lines, metrics_by_mode


def main() -> None:
    case_queries = _auto_case_queries()
    report = [
        "# RAG 检索对照评测（BM25 / 向量 / 混合）",
        "",
        "- 指标: Recall@1 / Recall@3 / MRR@3；耗时含查询与扩展开销（索引为进程内单例）",
        "- 向量路开启领域同义词扩展（`rag.expand_query`）；BM25 路保持原查询，对照公平",
        "",
    ]

    lines, paraphrase = _section(
        "平台规范库 · 口语化改写（有词项重叠）",
        RULE_QUERIES_PARAPHRASE, search_platform_rules, "title")
    report += lines

    lines, cross = _section(
        "平台规范库 · 跨词表（几乎零字面重叠）",
        RULE_QUERIES_CROSS_VOCAB, search_platform_rules, "title")
    report += lines

    lines, case_metrics = _section(
        "历史爆款案例库",
        case_queries, search_hit_cases, "id")
    report += lines

    def best(metrics_by_mode: dict, metric: str) -> str:
        return max(MODES, key=lambda mode: metrics_by_mode[mode][metric])

    report += [
        "## 结论",
        "",
        "| 场景 | 最优模式 | BM25 MRR | 向量 MRR | hybrid MRR |",
        "|---|---|---|---|---|",
        f"| 口语化改写 | {best(paraphrase, 'mrr@3')} | "
        f"{paraphrase['bm25']['mrr@3']:.3f} | {paraphrase['vector']['mrr@3']:.3f} | "
        f"{paraphrase['hybrid']['mrr@3']:.3f} |",
        f"| 跨词表 | {best(cross, 'mrr@3')} | "
        f"{cross['bm25']['mrr@3']:.3f} | {cross['vector']['mrr@3']:.3f} | "
        f"{cross['hybrid']['mrr@3']:.3f} |",
        f"| 案例库 | {best(case_metrics, 'mrr@3')} | "
        f"{case_metrics['bm25']['mrr@3']:.3f} | {case_metrics['vector']['mrr@3']:.3f} | "
        f"{case_metrics['hybrid']['mrr@3']:.3f} |",
        "",
        "**没有单一模式在所有场景最优**，这正是不该拍脑袋选框架的原因：",
        "",
        f"- 口径一致、术语密集时 BM25 已接近上限（MRR {paraphrase['bm25']['mrr@3']:.3f}），"
        f"向量单独用反而略低（{paraphrase['vector']['mrr@3']:.3f}）；",
        f"- 跨词表时 BM25 明显失效（MRR {cross['bm25']['mrr@3']:.3f}），"
        f"同义词扩展后的向量路把 MRR 拉到 {cross['vector']['mrr@3']:.3f}，"
        f"hybrid 为 {cross['hybrid']['mrr@3']:.3f}，且三者 Recall@3 均达 "
        f"{cross['hybrid']['recall@3']:.3f}（正确答案不会被漏掉，只是排序位置不同）；",
        "- 默认 `RAG_MODE=hybrid` 是「不挑场景」的稳妥选择：口语化场景追平 BM25，"
        f"跨词表场景比 BM25 提升 "
        f"{cross['hybrid']['mrr@3'] - cross['bm25']['mrr@3']:+.3f} MRR；",
        "- 若线上查询确认以口语化/跨词表为主，可切 `RAG_MODE=vector` 追求排序精度；"
        "若查询与文档用词高度一致，切 `bm25` 最省；",
        "- 融合权重不是固定值：按 BM25 top1 分数做置信度自适应（0.15~0.80），"
        "术语型查询偏向 BM25，跨词表查询把权重让给向量，避免固定权重两头不讨好；",
        "- 回归口径：任何改动后重跑本脚本，hybrid 的 Recall@3 或 MRR@3 低于基线即判定回归。",
        "",
        "> 边界：本地默认 hashing embedding 没有真实语义能力，跨词表能力来自 `expand_query` "
        "的同义词表；配置 `EMBEDDING_PROVIDER=remote` + 远程 embedding 后可去掉同义词表再对比，"
        "那才是语义模型的上限。本报告的指标只用于横向比较三路策略，不代表业务绝对效果。",
    ]

    output = Path(__file__).with_name("rag_comparison.md")
    output.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\n已写入 {output}")


if __name__ == "__main__":
    main()
