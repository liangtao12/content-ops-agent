# GitHub 同类项目对照与改进记录

检索时间：2026-11-02。Stars 和活跃时间会持续变化，这里记录当时用于筛选的依据。

## 对照项目

### 1. Microsoft Content Generation Solution Accelerator

[microsoft/content-generation-solution-accelerator](https://github.com/microsoft/content-generation-solution-accelerator)

- 约 259 Stars，Python，MIT，仓库已归档。
- 用多个专业 Agent 处理 creative brief、产品资料检索、文本/图片生成和品牌合规校验。
- 值得借鉴：合规反馈分为 Error、Warning、Info；生成内容必须由产品资料和品牌规则 grounding；Agent 编排之外还有监控和部署模板。
- 本项目采纳：品牌语气、禁用表达和 CTA 进入创作与审核提示；工作台分别展示合规风险与品牌约束。
- 未采纳：Azure Search、Cosmos DB、Blob Storage 和图片生成。当前项目不适合为了技术栈完整度引入云依赖。

### 2. SocialFlow

[inbharatai/SocialFlow](https://github.com/inbharatai/SocialFlow)

- 约 40 Stars，Python，FastAPI、SQLite、APScheduler。
- 采用 Scout、Planner、Creator、Reviewer、Publisher、Analyst 六段内容流水线，提供审批队列、品牌套件、审计日志和平台账号管理。
- 值得借鉴：审批是产品的一等对象；品牌 kit 应独立于账号人设；所有发布动作需要操作记录。
- 本项目采纳：运营概览、审批队列、任务详情、审计时间线和品牌约束。
- 未采纳：浏览器会话保存和直接登录平台发布。平台自动化涉及账号凭据、平台条款和较高安全风险，当前只做安全排期与发布适配边界。

### 3. qiuxchao/content-agent

[qiuxchao/content-agent](https://github.com/qiuxchao/content-agent)

- 约 26 Stars，Python，MIT，LangGraph + LangChain。
- 支持多平台文章、Tavily 搜索、Chroma RAG、Critic 自动重写、SQLite 文章管理、FastAPI SSE 和 LangSmith。
- 值得借鉴：Critic 结果必须回到重写节点；RAG 不是只存在于 prompt，而是应提供可查看的素材依据；运行状态应反馈到界面。
- 本项目采纳：新增 `research_evidence`，在任务详情中展示热榜与爆款案例检索证据；保留规则 + LLM 双层审核和最多两轮复审。
- 未采纳：图片生成和微信公众号发布。当前核心问题是内容运营闭环与可靠性，不是多模态素材生产。

### 4. Social Media Content Moderation Quality Control

[aniket-work/How-I-Built-Social-Media-Content-Moderation-Quality-Control-Using-AI-Agents](https://github.com/aniket-work/How-I-Built-Social-Media-Content-Moderation-Quality-Control-Using-AI-Agents)

- 约 7 Stars，Python，Apache-2.0，更接近教程型项目。
- 将质量检查拆成文档相关性、事实幻觉和回答质量三个评分维度。
- 值得借鉴：合规不是一个二元标签，应保留风险类型、严重性和修改建议。
- 本项目采纳：风险明细继续保留类别、等级和 advice；工作台将合规结论与具体风险项分开显示。
- 未采纳：完全用 LLM 多 Agent 自由协作。教程项目的验证链较短，本项目仍使用确定性状态路由。

### 5. Multi-Agentic Blog Generation

[KalyanM45/Multi-Agentic-Blog-Generation](https://github.com/KalyanM45/Multi-Agentic-Blog-Generation)

- 约 34 Stars，Python，MIT，LangGraph + Groq。
- 将文章生成、格式化、发布和 GitHub Actions 自动化串成持续内容流水线。
- 值得借鉴：排期和发布应有独立、可追踪的终态，不应混在生成节点里。
- 本项目采纳：排期继续使用审批令牌、幂等键和独立业务记录；GitHub Actions 增加前端语法检查和评测。
- 未采纳：无人审批直接发布。内容合规场景保留 human-in-the-loop，不能为了自动化绕过人工责任。

## 本轮实际改动

1. 新增企业运营工作台：概览、审批队列、任务详情、发布日程和质量治理。
2. 新增 SQLite 业务库：任务、状态和审计事件独立于 LangGraph checkpoint。
3. 新增品牌约束结构化输入：品牌语气、禁用表达、必含信息和 CTA 进入创作与审核。
4. 新增检索证据：热榜和爆款案例结果保存到任务，并在审批工作区展示。
5. 新增 API：概览、任务列表、任务详情、审计、日程。
6. 新增写接口 API Key 保护和安全响应头。
7. 测试扩展到 46 条，覆盖账号登录、管理员成员管理、业务库、角色权限、审计和原有 Agent 链路。
8. 使用真实 Chromium 检查桌面与移动布局，页面无横向溢出、无控制台错误，新建任务和审批抽屉可完成交互。

## 本轮实际改动（检索与流式）

上一轮把检索写成"BM25，接口化可换向量库"，但接口后面是空的——这是简历上最容易被追问穿的一类写法。
本轮把它补成真实现，并补上长链路最影响体感的流式输出：

1. 新增 `app/vector_store.py`：`Embedder` 用 Protocol 定义，本地 `LocalHashEmbedder`（blake2b signed hashing + 亚线性 TF + L2 归一化）与远程 `OpenAIEmbedder` 互为实现；`VectorIndex` 做内存余弦检索。零依赖保证 Mock/CI 能跑检索回归。
2. `app/rag.py` 改为三路可切换（`RAG_MODE=bm25|vector|hybrid`）：RRF 按排名融合避免两路分数量纲不可比；融合权重按 BM25 top1 分数做置信度自适应（0.15~0.80），解决固定权重"术语查询和跨词表查询两头不讨好"。
3. 新增 `expand_query` 领域同义词表，只作用于向量路，BM25 路保持原查询——对照实验仍然公平。
4. 重排策略换过一次：先用"查询词覆盖率"重排，实测把 BM25 的噪声又顶一次（跨词表 MRR 0.722）；改成**双路一致性加成**后升到 0.806。
5. 新增 `eval/compare_rag.py`：口语化改写 / 跨词表 / 案例库三组查询 × 三种模式，输出 `eval/rag_comparison.md`。结论是"没有单一模式在所有场景最优"，因此默认 hybrid。
6. 新增 `POST /api/run/stream`：`graph.stream(stream_mode="updates")` 逐节点推 SSE，事件为 `start / node / awaiting_human / done / error`；`node` 只推摘要不推正文大对象。前端 `streamRun` 用 `ReadableStream` 解析，不支持时回退同步接口。
7. 测试从 46 条扩到 57 条：新增 11 条覆盖模式归一化与非法值兜底、同义词扩展、三路召回不变量、双路一致性标记、权重自适应、embedding 确定性与归一化、向量排序、SSE 事件序列与鉴权——刻意不断言具体文档顺序，只锁"必须召回正确答案"这类不变量。

## 后续优先级

1. 接入真实热榜和平台数据接口，并记录来源更新时间。
2. 把内容、审计和排期迁移到 PostgreSQL，支持多实例部署。
3. 接入企业 SSO、组织架构和多租户隔离；当前本地账号体系适合内部试用。
4. 为不同平台生成同一选题的多版本内容，并增加平台发布适配器。
5. 接入 OpenTelemetry 或 Langfuse，补全跨服务 trace 与模型成本统计。
