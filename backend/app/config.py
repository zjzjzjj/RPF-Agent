"""运行期配置。

约定（沿用 RPF 的教训）：所有取值都通过函数在**调用时**读环境变量，
绝不在导入时固化——否则测试与部署脚本无法在进程启动后覆盖配置。

.env 支持：导入时读取项目根目录的 .env（KEY=VALUE，# 注释），
以 setdefault 方式注入环境变量——显式设置的环境变量永远优先。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 项目根目录：backend/app/config.py -> backend/app -> backend -> RPF-Agent
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# 文档拒绝码（稳定机器码，面向用户另有中文消息，见 errors.py）
REJECT_NOT_A_PDF = "NOT_A_PDF"
REJECT_EMPTY_FILE = "EMPTY_FILE"
REJECT_TOO_LARGE = "TOO_LARGE"
REJECT_ENCRYPTED = "ENCRYPTED"
REJECT_NO_TEXT_LAYER = "NO_TEXT_LAYER"
REJECT_EXTRACTION_FAILED = "EXTRACTION_FAILED"
REJECT_NOT_SUPPORTED = "NOT_SUPPORTED"


def data_dir() -> Path:
    """运行数据根目录（上传件、渲染缓存、数据库）。"""
    return Path(os.environ.get("BID_EVAL_DATA_DIR", PROJECT_ROOT / "data"))


def db_path() -> Path:
    return Path(os.environ.get("BID_EVAL_DB_PATH", data_dir() / "bid_eval.db"))


def database_url() -> str:
    """SQLAlchemy 连接串。默认 SQLite；升 Postgres 时只改这里。"""
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    return f"sqlite:///{db_path().as_posix()}"


def law_kb_path() -> Path:
    """法条知识库 JSON（法律规则包 source_node_id 的核验源）。

    由 261010 法规库的 build_fatiao_kb.py 生成；服务进程与脚本进程读同一路径，
    脚本侧负责把制品 vendoring 到这里（见 scripts/import_law_rules.py）。
    """
    return Path(os.environ.get("BID_EVAL_LAW_KB_PATH", data_dir() / "law_kb.json"))


def upload_dir() -> Path:
    return Path(os.environ.get("BID_EVAL_UPLOAD_DIR", data_dir() / "uploads"))


def render_dir() -> Path:
    return Path(os.environ.get("BID_EVAL_RENDER_DIR", data_dir() / "renders"))


# ---------------------------------------------------------------------------
# 全自动模式
# ---------------------------------------------------------------------------
def auto_mode() -> bool:
    """全自动模式（默认开）：模糊项由 LLM 带原文证据裁决，全链无人工环节。

    开启时：human_only/冲突信号由 engine/auto_judge 裁决收敛终态；主观分 LLM
    直接写 final_score；confirm/freeze 的阻断项自动采纳（actor="auto" 全留痕）；
    报告"自动裁决记录"章节替代人工复核队列。
    置 0 回退人工模式（三态 + 复核队列），旧护栏测试在 conftest 里显式关闭。
    """
    return os.environ.get("BID_EVAL_AUTO_MODE", "1").strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = 200  # 单文件上限
MIN_CHARS_PER_PAGE = 100  # 抽样页平均字符数低于此值判定为无文字层（扫描件）
TEXT_PROBE_PAGES = 5  # 判定文字层时抽样的前 N 页
DOCX_APPROX_CHARS_PER_PAGE = 1800  # docx 虚拟分页的每页字符数


def pdf_engine() -> str:
    """PDF 主解析引擎：pymupdf（默认）| pdfplumber（MIT 回退，见 decisions/0001）。"""
    return os.environ.get("BID_EVAL_PDF_ENGINE", "pymupdf").strip().lower()


# ---------------------------------------------------------------------------
# OCR（扫描件兜底，rapidocr-onnxruntime）
# ---------------------------------------------------------------------------
OCR_PAGE_MIN_CHARS = 30  # 页字符数低于此值 → 该页走 OCR（低于抽样阈值，防"扫描封面+数字正文"误判）
OCR_RENDER_SCALE = 2.0  # OCR 页渲染倍率（≈144dpi，中文 12pt 可靠）


def ocr_enabled() -> bool:
    return os.environ.get("BID_EVAL_OCR", "1").strip().lower() not in ("0", "false", "no", "off")


def ocr_max_pages() -> int:
    """单本文档 OCR 页数上限（护栏）；超限的缺字页跳过并记警告。"""
    return int(os.environ.get("BID_EVAL_OCR_MAX_PAGES", 60))


def max_upload_bytes() -> int:
    return int(os.environ.get("BID_EVAL_MAX_UPLOAD_MB", MAX_UPLOAD_MB)) * 1024 * 1024


# ---------------------------------------------------------------------------
# 页面渲染（证据高亮）
# ---------------------------------------------------------------------------
DEFAULT_RENDER_SCALE = 2.0  # 相对 72dpi 的倍数


def render_scale() -> float:
    return float(os.environ.get("BID_EVAL_RENDER_SCALE", DEFAULT_RENDER_SCALE))


# ---------------------------------------------------------------------------
# 证据定位
# ---------------------------------------------------------------------------
MIN_QUOTE_CHARS = 12  # 引文短于此长度不予核验（沿用 RPF 阈值）
PREFIX_FALLBACK_LENGTHS = (60, 40, 25, 15)  # 前缀回退逐级尝试长度


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def llm_provider() -> str:
    """mock | openai（OpenAI 兼容端点，可指向 DeepSeek/Qwen 等）。"""
    return os.environ.get("BID_EVAL_LLM_PROVIDER", "mock")


def llm_base_url() -> str | None:
    return os.environ.get("BID_EVAL_LLM_BASE_URL")  # 如 https://api.deepseek.com/v1


def llm_api_key() -> str | None:
    return os.environ.get("BID_EVAL_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")


def llm_model() -> str:
    return os.environ.get("BID_EVAL_LLM_MODEL", "deepseek-chat")


def llm_timeout_s() -> float:
    return float(os.environ.get("BID_EVAL_LLM_TIMEOUT_S", 120))


def llm_max_concurrency() -> int:
    return int(os.environ.get("BID_EVAL_LLM_CONCURRENCY", 4))


def llm_daily_call_budget() -> int:
    """单进程每日调用上限（0 = 不限）。超限时 run 失败并明确报错，不静默降级。"""
    return int(os.environ.get("BID_EVAL_LLM_DAILY_BUDGET", 0))


def llm_prompt_version() -> str:
    """提示词版本号；进入缓存键与审计记录。改 prompt 必须同步改它。"""
    return os.environ.get("BID_EVAL_PROMPT_VERSION", "2026-10-09.1")


def local_endpoint_hosts() -> list[str]:
    """数据分级：confidential 数据允许出网的端点主机白名单（默认仅本地）。"""
    raw = os.environ.get("BID_EVAL_LOCAL_LLM_HOSTS", "localhost,127.0.0.1,::1")
    return [h.strip() for h in raw.split(",") if h.strip()]


# ---------------------------------------------------------------------------
# 模型目录（硅基流动，OpenAI 兼容）
# ---------------------------------------------------------------------------
DEFAULT_MODEL_PRESETS: list[dict] = [
    {
        "id": "deepseek-ai/DeepSeek-V4-Flash",
        "label": "DeepSeek V4 Flash",
        "family": "DeepSeek",
        "note": "均衡之选，JSON 抽取稳定（默认）",
    },
    {
        "id": "zai-org/GLM-5.2",
        "label": "GLM 5.2",
        "family": "GLM",
        "note": "中文理解强",
    },
    {
        "id": "Pro/moonshotai/Kimi-K2.6",
        "label": "Kimi K2.6",
        "family": "Kimi",
        "note": "推理模型：先思考再作答，更慢更贵但判断更细",
    },
    {
        "id": "Qwen/Qwen3.8-27B",
        "label": "Qwen3.8 27B",
        "family": "Qwen",
        "note": "通义最新代，速度快",
    },
]


def model_presets() -> list[dict]:
    """可选模型清单。可用 BID_EVAL_MODEL_PRESETS（JSON 数组）覆盖。"""
    raw = os.environ.get("BID_EVAL_MODEL_PRESETS")
    if raw:
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 —— 配置写错时退回默认，并在日志可见
            pass
    return DEFAULT_MODEL_PRESETS


def llm_api_key_configured() -> bool:
    return bool(llm_api_key())


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
JOB_HEARTBEAT_S = 5
JOB_STALE_S = 300  # 心跳超时视为 worker 已死，任务可被重新拾取
