"""全局配置：集中管理环境变量与路径，避免魔法字符串散落各处。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
TRACE_DIR = BASE_DIR / "traces"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
load_dotenv(PROJECT_ROOT / ".env")

# LLM 配置 -------------------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
# 无 Key 时自动降级为 MockLLM，保证测试/评测流水线离线可跑
USE_MOCK = (not DEEPSEEK_API_KEY) or os.getenv("FORCE_MOCK", "") == "1"

# 业务参数 ---------------------------------------------------------------
MAX_REVIEW_ROUNDS = 2      # 合规打回最多重写次数，超过转人工
MAX_HUMAN_EDIT_ROUNDS = 3  # 人工改稿最多重新复核次数，防止无限循环

# RAG 检索配置 -----------------------------------------------------------
# bm25 / vector / hybrid（默认混合：BM25 精确匹配 + 向量语义召回，RRF 融合后重排）
RAG_MODE = os.getenv("RAG_MODE", "hybrid").strip().lower()
if RAG_MODE not in {"bm25", "vector", "hybrid"}:
    RAG_MODE = "hybrid"
RAG_CANDIDATE_K = int(os.getenv("RAG_CANDIDATE_K", "6"))
# local = 本地 hashing embedding（零依赖离线可跑）；remote = OpenAI 兼容 /v1/embeddings
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "384"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")

SCHEDULE_FILE = DATA_DIR / "schedule.json"
CHECKPOINT_FILE = Path(
    os.getenv("CHECKPOINT_FILE", str(DATA_DIR / "checkpoints.sqlite"))
)
if not CHECKPOINT_FILE.is_absolute():
    CHECKPOINT_FILE = BASE_DIR / CHECKPOINT_FILE
WORKSPACE_DB = Path(
    os.getenv("WORKSPACE_DB", str(DATA_DIR / "workspace.sqlite"))
)
if not WORKSPACE_DB.is_absolute():
    WORKSPACE_DB = BASE_DIR / WORKSPACE_DB

# 可选服务账号 Key，仅用于自动化调用；用户工作台使用登录会话。
CONTENTOPS_API_KEY = os.getenv("CONTENTOPS_API_KEY", "")
AUTH_SESSION_HOURS = int(os.getenv("AUTH_SESSION_HOURS", "12"))
REGISTRATION_CODE = os.getenv("REGISTRATION_CODE", "")

# MCP 排期审批令牌。未配置时仍可本地演示，但会在健康检查中标记为不安全。
APPROVAL_SECRET = os.getenv("APPROVAL_SECRET", "contentops-dev-only-secret")
APPROVAL_SECRET_IS_DEV = APPROVAL_SECRET == "contentops-dev-only-secret"
APPROVAL_TTL_MINUTES = int(os.getenv("APPROVAL_TTL_MINUTES", "30"))

# 知识库文件 -------------------------------------------------------------
RULES_FILE = DATA_DIR / "platform_rules.md"
BANNED_WORDS_FILE = DATA_DIR / "banned_words.json"
HIT_CASES_FILE = DATA_DIR / "hit_cases.jsonl"
HOT_TOPICS_FILE = DATA_DIR / "hot_topics.json"
