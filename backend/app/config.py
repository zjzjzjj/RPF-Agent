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
# 评审委员会（AI 评委独立评分 + 去尾平均）
# ---------------------------------------------------------------------------
def committee_size() -> int:
    """主观分评审 Agent 数（默认 1 = 单评委，直接采纳其建议分）。

    2026-10-14 用户指令：5 评委去尾平均的独立性收益有限（同模型采样分数雷同），
    改回单评委。设为 >=2 恢复委员会去尾平均（size>=3 才有去尾意义）。
    确定性项（否决/计数/价格）不组委员会（客观分必须一致原则）。
    """
    return max(1, int(os.environ.get("BID_EVAL_COMMITTEE_SIZE", 1)))


# ---------------------------------------------------------------------------
# OCR（扫描件兜底，rapidocr-onnxruntime）
# ---------------------------------------------------------------------------
OCR_PAGE_MIN_CHARS = 30  # 页字符数低于此值 → 该页走 OCR（低于抽样阈值，防"扫描封面+数字正文"误判）
OCR_RENDER_SCALE = 2.0  # OCR 页渲染倍率（≈144dpi，中文 12pt 可靠）


def ocr_enabled() -> bool:
    return os.environ.get("BID_EVAL_OCR", "1").strip().lower() not in ("0", "false", "no", "off")


def ocr_engine() -> str:
    """扫描页 OCR 引擎：rapidocr（默认，本地）| remote_paddle（mwb 自部署 PaddleOCR-VL）。

    remote_paddle 失败时解析层自动回退 rapidocr（见 pdf_pymupdf._ocr_page）。
    """
    return os.environ.get("BID_EVAL_OCR_ENGINE", "rapidocr").strip().lower()


def paddle_ocr_url() -> str:
    """PaddleOCR-VL vLLM 服务地址（mwb coll-det-gpu-ocr-proxy，OpenAI 兼容协议）。"""
    return os.environ.get("BID_EVAL_PADDLE_OCR_URL", "http://100.100.30.150:16102").strip()


def embed_url() -> str:
    """bge-m3 嵌入服务地址（mwb coll-det-gpu-embed-proxy，POST /embed 协议）。"""
    return os.environ.get("BID_EVAL_EMBED_URL", "http://100.100.30.150:16400").strip()


def selection_mode() -> str:
    """主观分选段方式：keyword（默认，关键词分层）| vector（bge-m3 向量检索 + 关键词兜底）。"""
    return os.environ.get("BID_EVAL_SELECTION", "keyword").strip().lower()


def llm_disable_thinking() -> bool:
    """关闭 Qwen3 等推理模型的 <think> 输出（经 chat_template_kwargs 下发）。

    本地 vLLM 部署 Qwen3 默认思考模式：回答前生成大段推理，拖慢且曾把长题
    顶到 max_tokens 截断。硅基流动等云端端点不识别该参数——默认关。
    """
    return os.environ.get("BID_EVAL_LLM_DISABLE_THINKING", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def llm_seed() -> int | None:
    """请求级随机种子（vLLM 支持 seed 参数）：同输入同输出，消除跨轮判档跳变。"""
    raw = os.environ.get("BID_EVAL_LLM_SEED", "42").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def llm_transient_retries() -> bool:
    """瞬态失败（超时/限流/5xx）重试开关（默认开，重试 2 次）。"""
    return os.environ.get("BID_EVAL_LLM_TRANSIENT_RETRIES", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def review_clusters() -> int:
    """分层评审的证据簇数 K（默认 3）：每（投标人×评分项）取 K 个不相交证据簇
    分别深评后聚合。设 1 退化为单次评审。"""
    return max(1, int(os.environ.get("BID_EVAL_REVIEW_CLUSTERS", 3)))


def review_mode() -> str:
    """主观评审模式：hierarchical（默认，分层证据评审）| legacy（单次选段）。"""
    return os.environ.get("BID_EVAL_REVIEW_MODE", "hierarchical").strip().lower()


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
def llm_provider() -> str | None:
    """OpenAI 兼容 provider 名（openai/siliconflow/deepseek/qwen/glm/kimi）。

    未设置返回 None——registry 据此 fail-fast 报 LLM_NOT_CONFIGURED，不静默降级。
    """
    return os.environ.get("BID_EVAL_LLM_PROVIDER") or None


def llm_base_url() -> str | None:
    return os.environ.get("BID_EVAL_LLM_BASE_URL")  # 如 https://api.deepseek.com/v1


def llm_api_key() -> str | None:
    return os.environ.get("BID_EVAL_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")


def llm_model() -> str:
    return os.environ.get("BID_EVAL_LLM_MODEL", "deepseek-chat")


def llm_timeout_s() -> float:
    return float(os.environ.get("BID_EVAL_LLM_TIMEOUT_S", 300))


def llm_max_output_tokens() -> int:
    """compile 等大 JSON 输出的 max_tokens。None 时 provider 默认输出上限
    （DeepSeek deepseek-chat 默认 4K）会截断长规则包 JSON——编译真实标书的
    已实证失败原因；8192 覆盖 87 条级规则包。"""
    return int(os.environ.get("BID_EVAL_LLM_MAX_TOKENS", 8192))


def llm_retry_backoff_s() -> float:
    """规则抽取两次尝试之间的退避秒数（限流/超时后的第二次尝试缓冲）。"""
    return float(os.environ.get("BID_EVAL_LLM_RETRY_BACKOFF_S", 2))


def llm_max_concurrency() -> int:
    return int(os.environ.get("BID_EVAL_LLM_CONCURRENCY", 4))


def llm_daily_call_budget() -> int:
    """单进程每日调用上限（0 = 不限）。超限时 run 失败并明确报错，不静默降级。"""
    return int(os.environ.get("BID_EVAL_LLM_DAILY_BUDGET", 0))


def llm_prompt_version() -> str:
    """提示词版本号；进入缓存键与审计记录。改 prompt 必须同步改它。"""
    return os.environ.get("BID_EVAL_PROMPT_VERSION", "2026-10-19.1")


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
JOB_MAX_ATTEMPTS = 3  # 僵尸重领上限；超过则判 failed，不再无限重复执行
