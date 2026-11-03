# ContentOps Agent — 面向 MCN 内容运营的多智能体系统

> 一个面向企业内容团队的 production-minded Agent 工作台：针对选题依赖经验、合规审核容易漏检、
> 人工审批中断后难以恢复等问题，用 LangGraph 编排「选题 → 创作 → 合规 → 人工确认
> → 编辑复审 → 排期 → 数据复盘」闭环。项目重点不仅是生成内容，还包括审批持久化、
> 失败恢复、幂等排期、可观测 trace 与可重复评测。

## 业务背景

一个 3-5 人小 MCN 团队运营多平台账号时的典型问题：

| 痛点 | 现状 | 系统方案 |
|---|---|---|
| 选题 | 运营个人经验，命中率低 | 选题 Agent 结合热榜数据 + 历史爆款案例库（RAG）给出量化候选 |
| 合规 | 人工通读广告法/平台规范，漏检率高 | 程序护栏（违禁词正则）+ LLM 语义风险评估双层审核 |
| 审核 | 全靠人，人是瓶颈 | AI 初审 + 打回自动重写（反思循环），人只做最终确认 |
| 排期 | Excel 手动维护 | 自动选择平台流量高峰时段写入排期表 |
| 复盘 | 不做或做得粗糙 | 发布后数据回流，生成复盘报告反哺选题决策 |

## 架构

```
                     ┌────────────────────────────────────────────────┐
                     │              LangGraph 工作流编排               │
                     │                                                │
  运营目标 ──────▶ ① 选题研究员 ──▶ ② 创作Agent ──▶ ③ 合规审核 ──┐    │
                     │   │tool calling│  │RAG grounding│  │护栏+LLM │    │
                     │   │ 热榜API     │  │ 平台规范库   │  │ 双层审核│    │
                     │   │ 爆款案例RAG │  └─────────────┘  └───┬─────┘    │
                     │   └───────────┘          ▲   未通过:打回重写│        │
                     │                          └────────────────┘        │
                     │                                     ▼              │
                     │                          ④ 人工确认 ✋ (interrupt)  │
                     │                                     │              │
                     │                    编辑稿强制复审 ──▶ 审批令牌         │
                     │                                     │              │
                     │                          ⑤ 排期发布 ──▶ ⑥ 数据复盘 │
                     └────────────────────────────────────────────────┘
                        工具层: 热榜/违禁词/排期/数据回流  +  MCP Server
                        观测层: 节点级 JSONL trace (LLM延迟/token/成本)
                        评测层: golden 数据集 + LLM-as-judge + 回归基线
```

**关键机制**：
- **反思循环（reflection loop）**：合规审核打回 → 创作Agent带着修改意见重写 → 最多 2 轮，仍不通过转人工兜底（防止无限循环烧 token）
- **Human-in-the-loop**：LangGraph `interrupt` 暂停工作流，运营在 CLI/API/网页上确认（通过/编辑/拒绝）后 `Command(resume)` 恢复；SQLite checkpoint 支持进程重启后继续审批
- **编辑后复审**：运营修改标题、正文或标签后，必须重新执行双层合规检查；复审失败会再次中断，不能直接排期
- **人工覆盖留痕**：合规失败后如确需放行，必须填写 `override_reason`；审批令牌会记录该覆盖
- **双层护栏**：违禁词是确定性问题 → 正则零成本解决；语义风险（"暗示疗效但没踩词"）→ LLM 评估。各干各擅长的事
- **安全 MCP 排期**：外部 Agent 只能携带与标题、正文、标签、平台绑定的短期审批令牌排期；内容变更或令牌过期后不能发布
- **幂等与并发隔离**：排期写入使用文件锁、原子替换和幂等键；trace 使用 ContextVar，避免并发请求串号

## 技术栈与 JD 对照

| 技术 | 用在哪 | 为什么 |
|---|---|---|
| LangGraph | 六节点状态图编排、条件路由、interrupt | JD 高频要求；状态机让"打回重写"循环天然可表达 |
| Function Calling | 选题 Agent 决定调用热榜/案例检索工具 | 真实 tool calling 协议（OpenAI tools 格式），非硬编码 |
| MCP | `app/mcp_server.py` 暴露双层合规检查与受审批保护的排期工具 | 工具即服务，可被外部 Agent 客户端复用 |
| RAG | 平台规范知识库 + 爆款案例库，BM25 / 向量 / 混合三路可切换（`RAG_MODE`） | 创作前检索规范做 grounding；三路对照评测用数据决定默认策略，而不是凭感觉选框架 |
| 向量检索 | 可插拔 `Embedder` 接口 + 内存余弦索引（默认本地 hashing，可切远程 embedding） | 本地模式零依赖、结果可复现，CI 能跑检索回归；换 bge/OpenAI 只需实现 `embed()` |
| 流式输出 | `POST /api/run/stream` 用 SSE 逐节点推送进度与摘要 | 长链路不必等全部结束；interrupt 语义与同步接口一致，前端可实时展示跑到哪一步 |
| 评测 | 10 条流程用例 + 44 条规则护栏用例 + RAG 三路对照 + 可选 LLM-as-judge | 同时验证编排可靠性、确定性合规规则和检索策略；Mock 模式不伪装内容质量 |
| 可观测 | 每节点/每次 LLM 调用 JSONL trace（延迟、token、轮次） | 并发执行使用 ContextVar 隔离 run context |
| 工程化 | FastAPI + SQLite checkpoint/业务库 + Docker + pytest 65 个测试 | 无 Key 时 MockLLM 离线回归；有 Key 时显式调用 DeepSeek |

GitHub Actions 会执行依赖安装、Python 编译、65 个测试和完整评测，并上传 `eval/report.md`。

GitHub 同类项目的筛选、取舍与本轮改进记录见
[docs/GITHUB_BENCHMARK.md](docs/GITHUB_BENCHMARK.md)。

项目目录职责见 [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)。

企业工作台 API：

| 接口 | 用途 |
|---|---|
| `GET /api/overview` | 工作台指标、最近任务和评测基线 |
| `GET /api/jobs` | 任务队列，支持状态筛选 |
| `GET /api/jobs/{id}` | 任务详情、审批载荷和审计记录 |
| `GET /api/schedules` | 已审批的发布日程 |
| `GET /api/audit/{id}` | 单任务操作时间线 |
| `GET /api/auth/bootstrap` | 判断是否首次初始化、是否需要邀请码 |
| `POST /api/auth/register` | 注册账号 |
| `POST /api/auth/login` | 登录并创建会话 |
| `POST /api/auth/logout` | 撤销当前会话 |
| `GET /api/admin/users` | 管理员查看成员 |
| `POST /api/admin/users` | 管理员创建成员 |
| `PATCH /api/admin/users/{id}` | 调整角色、显示名称或启停状态 |
| `POST /api/admin/users/{id}/reset-password` | 重置密码并撤销旧会话 |
| `POST /api/run` | 创建并执行内容任务 |
| `POST /api/run/stream` | 同上，但以 SSE 逐节点推送进度（start / node / awaiting_human / done / error） |
| `POST /api/human/{id}` | 提交通过、编辑或拒绝决策 |

## 企业运营工作台

前端已经从“运行流水线”演示页升级为任务型运营工作台：

- **运营概览**：待审批、运行中任务、完成率、已排期数量、最近任务和合规质量基线。
- **内容与审批队列**：按状态和关键词筛选任务，展示账号、平台、风险、负责人和更新时间。
- **审批工作区**：并排查看候选选题、正文编辑、合规风险、处理记录和审批动作。
- **发布日程**：展示通过审批并写入排期的内容、平台和发布时间。
- **质量与治理**：明确 Agent 负责语言决策，程序负责审批恢复、复审、幂等和审计。
- **业务数据层**：任务与审计事件写入 `backend/data/workspace.sqlite`，工作流 checkpoint 独立保存。
- **账号与会话**：支持注册、登录、退出、可撤销 Token、登录失败锁定和密码哈希。
- **管理员成员管理**：管理员可以新增成员、分配角色、停用账号和重置密码。
- **安全响应头**：默认添加 `X-Content-Type-Options`、`X-Frame-Options` 和 `Referrer-Policy`。

### 角色与权限

| 角色 | 查看内容和日程 | 创建任务 | 审批、编辑和拒绝 | 系统治理 |
|---|---:|---:|---:|---:|
| 内容运营 Operator | 是 | 是 | 否 | 否 |
| 内容审核员 Reviewer | 是 | 否 | 是 | 否 |
| 管理员 Admin | 是 | 是 | 是 | 是 |
| 只读观察者 Viewer | 是 | 否 | 否 | 否 |

首个注册用户自动成为管理员；后续注册默认成为内容运营，生产环境可要求邀请码。
角色只能由管理员在成员管理中分配，客户端不能通过 Header 自己提权。

### 管理员与账号救援（运维 CLI）

注册接口**故意不提供"管理员"选项**——允许自助注册管理员是最典型的提权漏洞。
管理员只能由现有管理员在「成员管理」里分配。

但这会带来一个死锁：第一个管理员的密码丢了、或者部署后库里没有任何 admin，
就没人能进「成员管理」。`app/admin_cli.py` 就是这条救援通道（只能在服务器本机执行，
不暴露 HTTP 接口）：

```bash
cd backend
python -m app.admin_cli list                          # 看现有账号与角色
python -m app.admin_cli promote aaa                   # 把 aaa 提升为管理员
python -m app.admin_cli reset-password admin          # 重置密码（随机生成 16 位）
python -m app.admin_cli create-admin ops --display 运维  # 直接建管理员
python -m app.admin_cli demote aaa --role operator    # 降级（最后一个管理员受保护）
```

角色是每次请求从库里实时读的，改完刷新页面即生效，不需要重新登录。
`demote` 不允许降级最后一个管理员，避免把自己锁在门外（有测试覆盖）。

## 快速开始

```bash
# 1. 安装后端环境
cd backend
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置（可选——不配置自动进入 Mock 模式，离线可跑全流程）
cp ../.env.example ../.env     # 配置模型 Key、审批 Secret 和注册邀请码

# 3. 测试与评测（继续在 backend 目录执行）
pytest --basetemp=../.pytest-tmp       # 65 个测试
python -m eval.run_eval                # 10 条流程用例 + 44 条护栏用例
python -m eval.compare_rag             # 三路检索对照（BM25 / 向量 / 混合）

# 4. 三种运行方式
python run_cli.py "秋冬敏感肌选题，目标涨粉"   # 命令行交互
uvicorn app.server:app --port 8000            # API + 企业运营工作台 (http://localhost:8000)
python -m app.mcp_server                      # MCP 工具服务（stdio）

# 5. Docker
docker compose -f ../infra/docker-compose.yml up --build
```

## 评测基线（Mock 模式，作为回归参照）

```
全流程完成率        10/10      合规拦截率        3/3
误伤率(不应拦而拦)   0/4       平均审核轮次      1.5
内容质量分            N/A       断言通过          10/10

规则护栏（44 条）:
Precision 0.833   Recall 1.000   F1 0.909   Accuracy 0.909
```

接入真实 DeepSeek 后重跑 `python -m eval.run_eval` 即得真实基线；
任何提示词/代码改动后重跑，**Recall 下降或误伤率上升即判定回归**。

> 诚实说明：Mock 基线证明的是**工程链路**（编排/中断/复审/恢复/断言）正确，
> 不代表 DeepSeek 的内容生成质量。规则护栏的 4 个假阳性来自“不建议使用处方药”
> 等否定或警示语境，这正说明生产环境还需要上下文判定或人工复核；报告不会隐藏分类错误。

## 检索策略：为什么不是一开始就上向量库

`app/rag.py` 保留三路可切换检索，`RAG_MODE` 控制默认值（默认 `hybrid`）：

| 模式 | 机制 | 适用 |
|---|---|---|
| `bm25` | 中文 2-gram 词项匹配 | 术语密集短文档（"极限词""报备""蒲公英"），零依赖、毫秒级 |
| `vector` | hashing embedding + 余弦相似度，附领域同义词扩展 | 用户说法与文档用词不一致（"能不能说绝对安全" → "广告法极限词"） |
| `hybrid` | 两路各召回候选 → RRF 融合 → 双路一致性重排 | 默认值，不挑场景 |

`python -m eval.compare_rag` 的实测结果（`eval/rag_comparison.md`）：

| 场景 | BM25 MRR@3 | 向量 MRR@3 | hybrid MRR@3 |
|---|---:|---:|---:|
| 口语化改写（有词项重叠） | 1.000 | 0.917 | **1.000** |
| 跨词表（几乎零字面重叠） | 0.417 | **0.889** | 0.806 |
| 历史爆款案例库 | 1.000 | 1.000 | 1.000 |

**没有单一模式在所有场景最优**，这正是不该拍脑袋选框架的原因：

- 术语一致时 BM25 已接近上限，向量单独用反而略低（0.917）；
- 跨词表时 BM25 明显失效（0.417），hybrid 比 BM25 高 +0.389 MRR，且三者 Recall@3 都是 1.000 —— 正确答案不会漏，只是排序位置不同；
- 融合权重不是固定值：按 BM25 top1 分数做置信度自适应（0.15~0.80），术语型查询偏向 BM25，跨词表查询把权重让给向量；
- 曾用"查询词覆盖率"做重排，实测反而把 BM25 的噪声又顶一次，最终改为**双路一致性加成**（两路都召回的文档才加分），hybrid 跨词表 MRR 从 0.722 提升到 0.806。

> 边界：本地 hashing embedding 没有真实语义能力，跨词表收益来自 `expand_query` 领域同义词表。
> 配置 `EMBEDDING_PROVIDER=remote` 接真实 embedding 后可去掉同义词表再对比，那才是语义模型的上限。
> 这些指标只用于横向比较三路策略，不代表业务绝对效果。

## 流式进度

`POST /api/run/stream` 用 `graph.stream(..., stream_mode="updates")` 逐节点产出 SSE 帧：

```
event: start          → {thread_id, status}
event: node           → {node, status, summary}   # summary 只推摘要，不推正文大对象
event: awaiting_human → {thread_id, requires_override_reason}
event: done           → {thread_id, status, job, pending?, draft, review_result}
event: error          → {message}
```

前端（`frontend/app.js` 的 `streamRun`）用 `fetch` + `ReadableStream` 解析，逐个点亮进度条；
浏览器不支持流式或响应无 body 时自动回退到同步 `/api/run`，不影响功能可用性。

## 可靠性与安全边界

- `human` 之前的内容和人工编辑后的内容使用同一套合规检查。
- 人工在合规失败后直接放行，必须填写 `override_reason`，trace 会记录该决策。
- checkpoint 默认写入 `data/checkpoints.sqlite`，重启服务后可通过 `/api/status/{thread_id}` 查询并恢复审批。
- `/api/human/{thread_id}` 验证线程确实处于等待审批状态，避免重复恢复或错误线程。
- MCP `schedule_publish` 没有有效审批令牌会拒绝执行；令牌绑定标题、正文、标签和平台，默认 30 分钟过期。
- 生产部署必须设置非默认 `APPROVAL_SECRET`；`/api/health` 会暴露是否仍在使用开发密钥。
- 已实现四类角色和 Token 映射；正式多租户部署仍需补充 SSO、组织管理和租户级数据隔离。

## 目录结构

```
backend/
  app/              # FastAPI、LangGraph、合规、RAG（BM25/向量/混合）、MCP、权限与数据层
  tests/            # 65 个后端测试
  eval/             # golden、规则样本与评测报告
  data/             # 模拟数据和运行数据库
  traces/           # 节点级运行日志
  run_cli.py        # 命令行入口
  requirements.txt  # 后端依赖
frontend/
  index.html        # 工作台页面
  app.js            # API 与交互逻辑
  styles.css        # 视觉与响应式布局
infra/
  Dockerfile
  docker-compose.yml
docs/               # 架构、GitHub 对照和目录说明
.github/            # CI
```

## 升级路线（README 的诚实部分）

1. **检索升级**：接入真实 embedding（`EMBEDDING_PROVIDER=remote`）+ Chroma/FAISS 替换内存索引（`VectorIndex` 内部实现），并在真实语料上重跑 `eval/compare_rag.py` 校准权重
2. **观测升级**：`tracing.record_llm` 换成 Langfuse SDK，获得可视化 trace 树
3. **Judge 升级**：评测 judge 换 deepseek-reasoner，避免同模型自我偏好
4. **企业身份与租户隔离**：接入企业 SSO、组织架构和多租户数据隔离
5. **排期存储升级**：文件排期表迁移到数据库，支持多实例部署和事务回滚
6. **真实数据源**：热榜/数据回流对接真实平台 API（当前为模拟数据，接口签名已定型）
