# ContentOps Agent 目录与角色说明

## 顶层结构

```text
content-ops-agent/
├── backend/          # Python API、Agent、权限、数据和测试
├── frontend/         # HTML、CSS、JavaScript 工作台
├── infra/            # Docker 构建与编排
├── docs/             # 项目说明、GitHub 对照、目录说明
├── .github/          # GitHub Actions
├── .env.example      # 环境变量模板
├── .gitignore
├── .dockerignore
└── README.md
```

## backend

```text
backend/
├── .venv/            # 后端 Python 虚拟环境，不提交
├── app/              # 应用源码
├── tests/            # pytest 测试
├── eval/             # 评测集、Judge 和报告
├── data/             # 模拟数据与运行数据库
├── traces/           # JSONL 运行日志
├── run_cli.py        # 命令行入口
├── requirements.txt  # Python 依赖
└── pytest.ini        # 测试配置
```

### backend/app

| 文件 | 作用 |
|---|---|
| `server.py` | FastAPI 路由、角色依赖、任务和审批 API |
| `auth.py` | 四类角色、权限矩阵与 Token 身份解析 |
| `admin_cli.py` | 账号运维：查看、提升管理员、重置密码（无 admin 时的救援通道） |
| `graph.py` | LangGraph 节点、条件路由、重写循环和人工中断 |
| `state.py` | 工作流共享状态 |
| `llm.py` | DeepSeek 客户端与离线 MockLLM |
| `review.py` | 正则规则与 LLM 语义合规联合检查 |
| `guardrails.py` | 违禁词、结构校验和自动修复 |
| `rag.py` | 平台规范与爆款案例检索：BM25 / 向量 / 混合三路（RRF 融合 + 自适应权重 + 同义词扩展） |
| `vector_store.py` | 可插拔 `Embedder`（本地 hashing / OpenAI 兼容）与内存余弦向量索引 |
| `tools.py` | 热榜、排期、指标等工具 |
| `approvals.py` | 内容绑定的短期审批令牌 |
| `persistence.py` | LangGraph SQLite checkpoint |
| `store.py` | 任务、审计事件和运营指标 |
| `tracing.py` | 并发安全的运行 trace |
| `mcp_server.py` | MCP 工具服务 |
| `config.py` | 路径和环境变量 |

## frontend

| 文件 | 作用 |
|---|---|
| `index.html` | 工作台页面结构和业务视图 |
| `app.js` | 登录注册、管理员成员管理、审批、搜索和日程 |
| `styles.css` | 视觉体系与响应式布局 |

前端不包含 Python 依赖、数据库或 Agent 逻辑，只通过 FastAPI 的 `/api/*` 接口访问后端。

## infra

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 构建 Python 服务和前端静态资源 |
| `docker-compose.yml` | 映射 8000 端口、环境变量、数据与 trace 卷 |

启动命令：

```powershell
docker compose -f infra/docker-compose.yml up --build
```

## 运行数据

| 路径 | 作用 |
|---|---|
| `backend/data/hot_topics.json` | 各平台热榜模拟数据 |
| `backend/data/hit_cases.jsonl` | 历史爆款案例 RAG 语料 |
| `backend/data/platform_rules.md` | 平台规范与内容质量规则 |
| `backend/data/banned_words.json` | 广告、医疗、金融和导流风险词 |
| `backend/data/schedule.json` | 发布排期 |
| `backend/data/checkpoints.sqlite` | 人工中断与恢复状态 |
| `backend/data/workspace.sqlite` | 任务与审计事件 |
| `backend/traces/*.jsonl` | 每次工作流的运行日志 |

数据库、trace、排期运行文件和 `.env` 都不进入 Git。

## 角色模型

| 角色 | 权限 |
|---|---|
| 内容运营 Operator | 查看、创建内容任务 |
| 内容审核员 Reviewer | 查看、通过、编辑、拒绝 |
| 管理员 Admin | 创建、审批、治理和全部权限 |
| 只读观察者 Viewer | 仅查看 |

创建与审批故意分离，避免运营自己创建并自己批准。开发环境可在页面切换角色；
首个注册用户自动成为管理员；其他账号由管理员创建或使用邀请码注册。
登录会话由服务端验证，客户端无法通过 Header 修改角色。
