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


def import_dir() -> Path:
    """项目包（zip）导入的解压暂存目录。"""
    return Path(os.environ.get("BID_EVAL_IMPORT_DIR", data_dir() / "imports"))


def import_max_zip_mb() -> int:
    """项目包（zip）大小上限 MB。真实标书包（含数十份扫描件 PDF）可达数 GB，
    默认 8GB（2026-11-17 从 500MB 上调；3.5GB 利群商标条盒项目包实证）。"""
    return int(os.environ.get("BID_EVAL_IMPORT_MAX_ZIP_MB", "8192"))


def import_max_uncompressed_mb() -> int:
    """解压总量上限 MB（zip 炸弹护栏）。与 import_max_zip_mb 同步上调至 16GB。"""
    return int(os.environ.get("BID_EVAL_IMPORT_MAX_UNCOMPRESSED_MB", "16384"))


def import_max_entries() -> int:
    return int(os.environ.get("BID_EVAL_IMPORT_MAX_ENTRIES", "20000"))


def render_dir() -> Path:
    return Path(os.environ.get("BID_EVAL_RENDER_DIR", data_dir() / "renders"))


def static_dir() -> Path | None:
    """前端构建产物目录（部署模式：同源伺服 frontend/dist）。

    未设置 = 纯 API 模式（本地开发默认，行为不变）；设置了但缺 index.html
    则 fail-fast——静态配置错误必须在启动期暴露，而不是让所有页面请求
    拿到 404 静默难查。
    """
    raw = os.environ.get("BID_EVAL_STATIC_DIR", "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not (p / "index.html").is_file():
        raise RuntimeError(f"BID_EVAL_STATIC_DIR={raw} 缺 index.html，拒绝启动")
    return p


# ---------------------------------------------------------------------------
# 全自动模式
# ---------------------------------------------------------------------------
def auto_mode() -> bool:
    """全自动模式（默认开）：模糊项由 LLM 带原文证据裁决，全链无人工环节。

    新运行使用 managed 执行器逐项评审并事务保存分数、证据及终态。按所选投标人
    与应评条款的完整矩阵严格检查，全部完成才发布正式排名和报告。技术故障补评后
    仍未解决则运行失败，保留成功项供续评。confirm/freeze 的自动采纳保留审计。
    置 0 仅用于历史人工模式兼容；已有运行沿各自冻结配置执行。
    """
    return os.environ.get("BID_EVAL_AUTO_MODE", "1").strip().lower() not in ("0", "false", "no", "off")


def qualification_mode_default() -> str:
    """新运行默认资格审核模式（运行级冻结，后续可在 config_json 查看）。

    light：确定性预检 + 每家投标人一次合并快审，不 OCR、不逐条款 LLM；
           结论只报告，不阻断计分/排名/价格基准价。
    full：旧的逐条款 LLM 深评（历史运行缺省）。
    """
    raw = os.environ.get("BID_EVAL_QUALIFICATION_MODE_DEFAULT", "light").strip().lower()
    if raw not in ("light", "full"):
        raise ValueError("BID_EVAL_QUALIFICATION_MODE_DEFAULT 只能为 light 或 full")
    return raw


def count_deterministic_default() -> str:
    """新运行默认客观计数确定性快路径模式（off/shadow/on）。"""
    raw = os.environ.get("BID_EVAL_COUNT_DETERMINISTIC", "on").strip().lower()
    if raw not in ("off", "shadow", "on"):
        raise ValueError("BID_EVAL_COUNT_DETERMINISTIC 只能为 off/shadow/on")
    return raw


def qualification_quick_chars() -> int:
    """资格轻量快审的页面文本预算（字符）。

    二值化否决点筛查（决策 0017）后快审的职责是找"确定性反面事实"，
    预算过小会漏掉反面证据；默认从 8000 提到 24000（可通过环境变量覆盖）。
    """
    try:
        return max(4000, int(os.environ.get("BID_EVAL_QUALIFICATION_QUICK_CHARS", "24000")))
    except ValueError:
        return 24000


def score_closure_enabled() -> bool:
    """Every scoring cell must settle even when the model/evidence path fails.

    Production default is on: a terminal cell failure is written as an explicit
    provisional score (basis=fallback) instead of leaving final_score NULL.
    Tests that guard the historical execution_failed semantics set this to 0.
    """
    return os.environ.get("BID_EVAL_SCORE_CLOSURE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def subjective_direct_enabled() -> bool:
    """One-shot direct subjective scoring (no citation/subitem blocking)."""
    return os.environ.get("BID_EVAL_SUBJECTIVE_DIRECT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def auto_match_enabled() -> bool:
    """招标文件解析成功后自动匹配/更新规则（默认开）。

    关闭后上传只解析、不自动编译；用户仍可在规则页手动「重新匹配」。
    未配置 LLM 时即使开启也不会触发（见 services/auto_merge_service.py）。
    """
    return os.environ.get("BID_EVAL_AUTO_MATCH", "1").strip().lower() not in ("0", "false", "no", "off")


def scoring_skeleton_enabled() -> bool:
    """评标细则表格骨架对账（默认开）。

    编译后用确定性表格骨架核对 LLM 抽取结果，补齐缺口行/校正分值
    （多媒体维保项目实证：LLM 碎行抽漏致总分 91≠100）。骨架解析失败
    自动回退纯 LLM 抽取；置 0 完全关闭对账。
    """
    return os.environ.get("BID_EVAL_SCORING_SKELETON", "1").strip().lower() not in ("0", "false", "no", "off")


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
OCR_MIXED_PAGE_MAX_CHARS = 800  # 「标题+整页扫描件」混排页阈值（合同/发票/验收页）
OCR_RENDER_SCALE = 3.0  # 整页渲染倍率（≈216dpi）。2.0 实测读不出扫描件里的小字
OCR_RENDER_MIN_CHARS = 20  # 整页渲染 OCR 产出低于此值且页内有大图 → 记 degraded（防静默垃圾）
OCR_IMAGE_MIN_RATIO = 0.35  # 页内图面积占比低于此值不单独 OCR（logo/印章/水印）
OCR_IMAGE_MIN_SIDE = 300  # 页内图短边低于此值不单独 OCR（像素）


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


def paddle_ocr_model() -> str:
    """PaddleOCR-VL 的模型名（vLLM /v1/models 返回的 id，不匹配会被 404 拒绝）。"""
    return os.environ.get("BID_EVAL_PADDLE_OCR_MODEL", "PaddleOCR-VL-1.6-0.9B").strip()


def paddle_ocr_prompt() -> str:
    """PaddleOCR-VL 的整页识别提示词。"""
    return os.environ.get("BID_EVAL_PADDLE_OCR_PROMPT", "OCR:").strip() or "OCR:"


def paddle_ocr_timeout_s() -> int:
    """单页远程 OCR 超时（秒）。默认 60——几百页文档下 300s 会让任务长时间挂死。"""
    return max(5, int(os.environ.get("BID_EVAL_PADDLE_OCR_TIMEOUT_S", 60)))


def paddle_ocr_probe_timeout_s() -> int:
    """远程 OCR 可用性探针超时（秒）。探针只做 /v1/models，毫秒级。"""
    return max(1, int(os.environ.get("BID_EVAL_PADDLE_OCR_PROBE_TIMEOUT_S", 3)))


def paddle_ocr_max_tokens() -> int:
    """远程 OCR 单页输出上限。默认 4096。

    服务端 max_model_len=8192，整页图约占 2.5-3k prompt token；取 8192 会直接
    400（输出预算超过上下文）。4096 对单页文本足够，且真被截断时
    `finish_reason=length` 会抛错而不是静默丢内容。
    """
    return max(512, int(os.environ.get("BID_EVAL_PADDLE_OCR_MAX_TOKENS", 4096)))


def ocr_max_failures() -> int:
    """远程 OCR 连续失败次数上限（熔断）：超过后本文档余下页不再调用远程，
    避免"连得上但不返回"时 742 页 × 超时把任务挂死。"""
    return max(1, int(os.environ.get("BID_EVAL_OCR_MAX_FAILURES", 3)))


def embed_url() -> str:
    """bge-m3 嵌入服务地址（mwb coll-det-gpu-embed-proxy，POST /embed 协议）。"""
    return os.environ.get("BID_EVAL_EMBED_URL", "http://100.100.30.150:16400").strip()


def selection_mode() -> str:
    """主观分选段方式：keyword（默认，关键词分层）| vector（bge-m3 向量检索 + 关键词兜底）。"""
    return os.environ.get("BID_EVAL_SELECTION", "keyword").strip().lower()


def llm_disable_thinking() -> bool:
    """关闭 Qwen3 等推理模型的 <think> 输出（经 chat_template_kwargs 下发）。

    本地 vLLM 部署 Qwen3 默认思考模式：回答前生成大段推理，拖慢且曾把长题
    顶到 max_tokens 截断。**该参数只对 vLLM 有效**——云端端点不识别
    （2026-10-21 实测：硅基流动不认），云端请用 `llm_thinking()`。
    """
    return os.environ.get("BID_EVAL_LLM_DISABLE_THINKING", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def llm_thinking() -> str:
    """推理模型的思考开关：auto（默认，不下发）| disabled | enabled。

    部分模型（如硅基流动的 zai-org/GLM-5.3-Flash）是推理模型：先输出
    reasoning_content 再给 content，思考会吃掉 max_tokens，预算不足时
    **content 直接为空**。`thinking: {"type": "disabled"}` 是该服务商支持的
    通用写法（实测有效），与 vLLM 专用的 chat_template_kwargs 互不干扰。
    """
    raw = os.environ.get("BID_EVAL_LLM_THINKING", "auto").strip().lower()
    return raw if raw in ("auto", "disabled", "enabled") else "auto"


def llm_default_max_tokens() -> int:
    """调用方未显式指定 max_tokens 时的输出预算（默认 8192）。

    推理模型的思考也算在 max_tokens 里：不给预算会被思考耗尽 → content 为空
    （2026-10-21 实测 GLM-5.3-Flash：max_tokens=16 时 content 为空）。
    """
    return max(256, int(os.environ.get("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", 8192)))


def llm_seed() -> int | None:
    """请求级随机种子（vLLM 支持 seed 参数）：同输入同输出，消除跨轮判档跳变。"""
    raw = os.environ.get("BID_EVAL_LLM_SEED", "42").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def llm_transient_retries() -> bool:
    """瞬态失败（超时/限流/5xx）重试开关（默认开）。"""
    return os.environ.get("BID_EVAL_LLM_TRANSIENT_RETRIES", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def llm_transient_attempts() -> int:
    """瞬态失败的总尝试次数（含首次），默认 4。

    云端「Flash」档模型高峰期会间歇返回 503 System is too busy
    （2026-10-21 实测 GLM-5.3-Flash：约 1/3 成功），只重试 1 次会让评分大面积
    降级为人工。退避 5s、10s、15s（见 provider 层）。
    """
    return max(1, int(os.environ.get("BID_EVAL_LLM_TRANSIENT_ATTEMPTS", 4)))


def llm_sdk_max_retries() -> int:
    """OpenAI SDK 内置重试次数，默认关闭。

    provider 层已经统一处理 timeout/限流/5xx 重试；再叠加 SDK 默认的 2 次
    隐藏重试会把一次调用放大成多层退避，且难以从日志判断真实请求数。
    """
    return max(0, int(os.environ.get("BID_EVAL_LLM_SDK_MAX_RETRIES", 0)))


def review_clusters() -> int:
    """分层评审的证据簇数 K（默认 3）：每（投标人×评分项）取 K 个不相交证据簇
    分别深评后聚合。设 1 退化为单次评审。"""
    return max(1, int(os.environ.get("BID_EVAL_REVIEW_CLUSTERS", 3)))


def review_mode() -> str:
    """主观评审模式：hierarchical（默认，分层证据评审）| legacy（单次选段）。"""
    return os.environ.get("BID_EVAL_REVIEW_MODE", "hierarchical").strip().lower()


def job_workers() -> int:
    """任务 worker 并发数（默认 1 = 串行队列）。

    部署设 2：评分与规则编译并行（2026-10-27 实证：评分 job 被 28min 编译任务
    排队阻塞 18min）。`_claim_next_job` 是条件 UPDATE 原子领取，多 worker 安全。
    """
    return max(1, int(os.environ.get("BID_EVAL_JOB_WORKERS", 1)))


def benchmark_exclude_suspect() -> bool:
    """确定性资格硬 fail（date_valid/结构化事实类）是否剔出价格基准价（默认开）。

    「AI 不否决」不变：投标人照常评分/排名/展示，只影响基准价口径（对齐人工
    "有效标均价"实践，2026-10-27 训练集测试2 实证：无效标进基准 → 价格分 +4~7）。
    """
    return os.environ.get("BID_EVAL_BENCHMARK_EXCLUDE_SUSPECT", "1").strip() not in ("0", "false", "")


def calibration_profile() -> dict:
    """主观档位校准参数（data/calibration/<name>.json，2026-10-27 训练集校准）。

    env `BID_EVAL_CALIBRATION_PROFILE` 选文件名（默认 "default"）。文件缺失/坏
    JSON → 空 profile（纯细则文本驱动，不注入任何尺度提示）。返回
    {"version":..., "scale_text":...}，version 落 ai_raw_json 供审计还原。
    """
    import json as _json

    name = (os.environ.get("BID_EVAL_CALIBRATION_PROFILE") or "default").strip() or "default"
    path = data_dir() / "calibration" / f"{name}.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 缺失/坏文件都不阻断评分
        return {"version": "", "scale_text": ""}
    return {
        "version": str(data.get("version") or ""),
        "scale_text": str(data.get("scale_text") or ""),
    }


def ocr_max_pages() -> int:
    """单本文档 OCR 页数上限（护栏）；超限的缺字页跳过并记警告。

    2026-10-29 run 60 实证：默认 60 使云锦汇智 334 页标书"超限跳过 92 页"
    扫描件（案例/证书/截图页全部不可见 → 计数项系统性全 0）。10-16 曾验证
    350 可行；60 是 10-21 重建 .env 时的回归值。护栏值非校准参数。
    """
    return int(os.environ.get("BID_EVAL_OCR_MAX_PAGES", 350))


# ---------------------------------------------------------------------------
# 业绩案例证据抽取（索引+页脚定位 / PyMuPDF 表提取 / 材料三态）
# ---------------------------------------------------------------------------
def case_section_max_pages() -> int:
    """无索引页时，表头页向前扩展的最大页数（章节边界优先）。"""
    return int(os.environ.get("BID_EVAL_CASE_SECTION_MAX_PAGES", 120))


def case_table_scan_pages() -> int:
    """业绩表解析只扫证据段前几页（表通常在段首）。

    2026-10-30 阶段3a 曾默认 4→8 + 多起点重试，run 69 实测表路径命中率
    8→8 毫无收益、count 墙钟 11→14.8min 反而恶化（纯加 PyMuPDF 解析开销）
    ——表解析失败根因是表格结构解析不出，不是扫描窗太小，故回退默认 4。
    """
    return int(os.environ.get("BID_EVAL_CASE_TABLE_SCAN_PAGES", 4))


def case_evidence_pages_per_material() -> int:
    """每案例每材料送 LLM 裁决的最大页数。"""
    return int(os.environ.get("BID_EVAL_CASE_EVIDENCE_PAGES_PER_MATERIAL", 2))


def case_digest_chars() -> int:
    """案例证据摘要总字符预算（送 LLM 裁决）。"""
    return int(os.environ.get("BID_EVAL_CASE_DIGEST_CHARS", 12000))


def case_max_batches() -> int:
    """无表回退（section-aware LLM 选页）时的批次数下限（自适应送审的地板）。"""
    return int(os.environ.get("BID_EVAL_CASE_MAX_BATCHES", 4))


def case_max_batches_ceiling() -> int:
    """自适应送审的批次数硬顶（2026-10-30 窄版+送审截断修复）。

    旧实现按固定上限截断候选页（run 72 实证：87 候选页只送审 71 页、16 页
    从没给核验模型看，模型"没找到材料"实为没看到）。改为按内容算所需批数、
    只在超过此硬顶才截断；12 批 ≈ 覆盖绝大多数标书证据段。
    """
    return max(1, int(os.environ.get("BID_EVAL_CASE_MAX_BATCHES_CEILING", 12)))


def case_unverified_count_enabled() -> bool:
    """窄版材料闸门开关（2026-10-30；2026-11-02 排序收紧；2026-11-24 严口径）。

    关（默认，2026-11-24 run158 放水修正）：材料三态=None（机器看不清/没绑上/
    页没送审）的案例**严口径不计入**——只按 verified 计分，未核验案例
    zero_reason=material_unverified + need_human，交 VL 看图翻案/人工复核
    （run158 实证"未核实计入"=无据给分：c43 两家人工 0→AI 4 放水；人工对
    缺/不合格材料也是严判）。
    开（legacy 回滚通道）：未核验案例**计入分**并标注"未核验"+进人工队列；
    只有三态=False（可读文本确认缺失）才维持 0。机器"没找到"混着"真没有"和
    "管线故障"的双向防线（用友 材料在不许清零 + 德勤 材料不可读不许满额）
    现由 VL 翻案承担。2026-11-02 排序收紧（run77 c41 反向实证）：计入的未核验
    案例排 verified 之后，max_items 封顶先砍未核验；warnings 附"剔除后为 X 分"
    双分值可复算，含未核验的格 machine_status=need_human。
    """
    return os.environ.get("BID_EVAL_CASE_UNVERIFIED_COUNT", "0").strip().lower() not in (
        "0", "false", "no", "off",
    )


def leader_table_proof_enabled() -> bool:
    """负责人业绩 work_proof 宽松口径开关（2026-11-02）。

    开（默认）：认通用「项目负责人情况表/人员情况表」（正式表格，表头+明细列+
    含该人员姓名）作 leader 佐证——work_proof=False/None 升 True；prompt 同步
    加第④种 true 形态。用友 c47 实证：吴丽君在 p73 情况表、人工给 4、LLM 严判
    0。真实评委认这种形态，与 member「项目团队人员情况表」放宽（prompts :201）
    对称。关=回退纯 LLM 严判（overlay 早退 + prompt 不拼 CLAUSE），供 eval_suite
    锚点对比与回滚。词表窄（表头+明细列+姓名三条件）防先数/西川虚高。
    """
    return os.environ.get("BID_EVAL_LEADER_TABLE_PROOF", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def objective_route_enabled() -> bool:
    """客观分路由开关（2026-11-03 run78 取证）。

    开（默认）：score_type=subjective 但细则是明示「X得N分」档表（≥2 档且
    客观标注/闭合档表）的格，评分时路由到客观引擎（value_tier/attribute_tier/
    count_tier）——run78 实证这类格被编译成 subjective 后丢确定性引擎：
    服务承诺「4小时内到达得4分…」走证据簇均分只得 2。歧义（档表不闭合/
    无客观信号）仍回退 subjective，不猜。关=全部走主观路径（锚点对比用）。
    """
    return os.environ.get("BID_EVAL_OBJECTIVE_ROUTE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def window_extend_enabled() -> bool:
    """索引指针窗口延伸开关（2026-11-03 run78 取证）。

    开（默认）：单页指针「见投标文件（39）页」落在标题页上时，窗口延伸到
    下一章节边界（封顶 15 页）——p39 只有 41 字符标题、证书扫描件在 p40+，
    死区间 (39,39) 让材料永远进不了送审窗口（ITSS 中科 0 分根因）。关=回退
    单页死区间（locate_section 路径本就不受影响）。
    """
    return os.environ.get("BID_EVAL_WINDOW_EXTEND", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def cert_backing_per_cert_enabled() -> bool:
    """证书背书逐本定位开关（2026-11-03 run78 取证）。

    开（默认）：classify_cert_backing 按证书名/编号定位复印件页做**逐本**三态
    （可读→True/低证据→None/可读确认无→False），section 级信号只作先验——
    文档级一票让 5 本证书共享同一判定（云平台证书 5/5 全计满根因）。
    关=回退 section 级一票（旧口径）。
    """
    return os.environ.get("BID_EVAL_CERT_BACKING_PER_CERT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def material_ocr_max_pages() -> int:
    """单个投标文档材料页 OCR 救援的页数上限。

    保留防打爆远程 OCR 的初衷（限量救援而非全量重扫）；2026-10-29 run 60
    实证默认 20 过紧（大标书案例材料页成批落在救援名单尾部被截断），放宽
    至 60。远程过载时可经 .env 单键 BID_EVAL_MATERIAL_OCR_MAX_PAGES 回退。
    """
    return int(os.environ.get("BID_EVAL_MATERIAL_OCR_MAX_PAGES", 60))


def material_index_enabled() -> bool:
    """材料分类索引总开关（0=全链路退化为无标签现状）。"""
    return os.environ.get("BID_EVAL_MATERIAL_INDEX", "1") not in ("0", "false", "False")


def classify_llm_max_pages() -> int:
    """单文档 LLM 兜底分类的页数上限（超出部分落 other）。"""
    return int(os.environ.get("BID_EVAL_CLASSIFY_LLM_MAX_PAGES", 200))


def case_name_prefix_min() -> int:
    """案例名前缀锚的最短长度（交错表全名常断裂）。"""
    return int(os.environ.get("BID_EVAL_CASE_NAME_PREFIX_MIN", 8))


def case_tag_prior_enabled() -> bool:
    """材料索引页级强标签接入业绩三态（tag_signal）开关（阶段4，默认开，2026-10-30）。

    扫描/低证据页的文本层判不了合同/发票/验收，但 material_tags 页级标签
    （rule 层 + CONF_STRONG）已在入库时对同类页盖过章——借用为三态先验：
    unreadable/scan_unverified 候选页命中标签 → True(reason=tag_signal)。
    absent（全页可读且无信号）分支不受影响、永不翻转。
    置 0（BID_EVAL_CASE_TAG_PRIOR=0）回退无标签旧口径（tag_pages=None）。
    """
    return os.environ.get("BID_EVAL_CASE_TAG_PRIOR", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def max_upload_bytes() -> int:
    return int(os.environ.get("BID_EVAL_MAX_UPLOAD_MB", MAX_UPLOAD_MB)) * 1024 * 1024


def max_parse_file_bytes() -> int:
    """解析期单文件上限，与 HTTP 上传限额解耦。

    背景（2026-11-18 run129 实证）：项目包 zip 一键导入按
    BID_EVAL_IMPORT_MAX_ZIP_MB（默认 8GB）放行，但真实主投标文件 PDF 单份
    160–426MB，解析期再按上传限额 200MB 拒绝（TOO_LARGE）会让每家的主标书
    "存在但不可见"——评分只剩报价表，技术分全 0。解析已按页流式处理，
    单文件大小不构成内存风险；这里单独放宽到 4GB，上传接口限额保持不变。
    """
    return int(os.environ.get("BID_EVAL_MAX_PARSE_FILE_MB", "4096")) * 1024 * 1024


def bid_text_min_chars() -> int:
    """单个投标人可读投标文本量的告警下限（字符）。

    低于该值说明主标书大概率没解析进来（run129 实证：只剩报价表时每家
    836–3340 字符；正常解析后为数十万字符），结果页必须显著告警。
    """
    return int(os.environ.get("BID_EVAL_BID_TEXT_MIN_CHARS", "5000"))


def ocr_page_concurrency() -> int:
    """解析期同一文档内并行 OCR 的页数。

    服务器有 6 台 PaddleOCR 容器，串行逐页调用是解析慢的主因（单页 2–7 秒）；
    页间无共享可变状态（各自独立缓存目录 + 原子锁），可安全并行。
    """
    return min(8, max(1, int(os.environ.get("BID_EVAL_OCR_PAGE_CONCURRENCY", "4"))))


def ocr_prioritize_material() -> bool:
    """OCR 页预算内材料页优先：命中合同/发票/证书/人员等关键词的扫描页先识别。

    OCR 页数上限（BID_EVAL_OCR_MAX_PAGES）截断时，优先保证明材料而不是
    按页序吃满预算；未截断的文档行为不变（全部页都会被 OCR）。
    """
    return os.environ.get("BID_EVAL_OCR_PRIORITIZE_MATERIAL", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def paddle_ocr_concurrency() -> int:
    """远程 OCR 并发槽位（进程内）。默认 4，可用 BID_EVAL_PADDLE_OCR_CONCURRENCY 调整。"""
    return min(8, max(1, int(os.environ.get("BID_EVAL_PADDLE_OCR_CONCURRENCY", "4"))))


def scoring_ocr_enabled() -> bool:
    """评分期是否对扫描页做 OCR/视觉补读（默认开；置 0 = 只按现有文本评审）。

    2026-11-19 用户指令：扫描件不做内容 OCR，只保留"扫描页"标签——评分直接
    使用解析期已有的文字层/OCR 文本，缺内容的扫描页按读取存疑处理。
    """
    return os.environ.get("BID_EVAL_SCORING_OCR", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def presence_route_enabled() -> bool:
    """材料存在性评分（existence/items）是否启用（默认开）。

    2026-11-19：目录/索引定位 + 页级图像信号判定"有没有材料"，不解析内容、
    不做 OCR。置 0 可一键回退到旧的计分路径。
    """
    return os.environ.get("BID_EVAL_PRESENCE_ROUTE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def ocr_supplement_min_chars() -> int:
    """评分补检只对"几乎没有文字"的页做 OCR（默认 80 字符）。

    旧实现把 source!=text / 无文字层 / 字符数<800 都当作需重识别——大扫描标书
    在评分阶段被逐格重复 OCR（2026-11-19 run137 实证：2.8 万次 OCR 请求、
    10 小时未出分）。文字层已有≥80 字符的页按原文评审，不再重复识别。
    """
    return max(0, int(os.environ.get("BID_EVAL_OCR_SUPPLEMENT_MIN_CHARS", "80")))


def supplement_ocr_max_pages() -> int:
    """每家投标人评分前补检 OCR 的页数上限（默认 40，材料页优先）。"""
    return max(0, int(os.environ.get("BID_EVAL_SUPPLEMENT_OCR_MAX_PAGES", "40")))


def visual_max_pages() -> int:
    """签章类视觉核验最多渲染的原页数（默认 12，签章关键词页优先）。"""
    return min(60, max(1, int(os.environ.get("BID_EVAL_VISUAL_MAX_PAGES", "12"))))


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
    # 2026-11-23 用户拍板：云端模型统一收口 DeepSeek-V4.1-Flash（文本+视觉）
    return os.environ.get("BID_EVAL_LLM_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash")


def llm_timeout_s() -> float:
    return float(os.environ.get("BID_EVAL_LLM_TIMEOUT_S", 300))


def llm_max_output_tokens() -> int:
    """compile 等大 JSON 输出的 max_tokens。None 时 provider 默认输出上限
    （DeepSeek deepseek-chat 默认 4K）会截断长规则包 JSON——编译真实标书的
    已实证失败原因；8192 覆盖 87 条级规则包。"""
    return int(os.environ.get("BID_EVAL_LLM_MAX_TOKENS", 8192))


def llm_context_tokens() -> int:
    """模型上下文窗口（token）。0 = 未知，不做输入裁剪。

    本地小上下文模型必须显式配置（如 Qwen3-8B 的 24576）：否则长招标文件的
    输入 + max_tokens 会超窗——vLLM 直接 400（2026-10-20 实证），
    即使 provider 层钳制 max_tokens，剩余输出预算也不足以写完规则 JSON。
    """
    return int(os.environ.get("BID_EVAL_LLM_CONTEXT_TOKENS", 0))


def llm_retry_backoff_s() -> float:
    """规则抽取两次尝试之间的退避秒数（限流/超时后的第二次尝试缓冲）。"""
    return float(os.environ.get("BID_EVAL_LLM_RETRY_BACKOFF_S", 2))


def compile_chunk_chars() -> int:
    """规则抽取的分块大小（字符）。<=0 表示不分块（整份选段一次抽取）。

    长招标文件的规则集合会超过单次输出上限（99 页文档实测 12000 tokens 仍被
    截断）。分块后每块输入更小、输出空间充足，避免 JSON 中途截断。
    """
    return int(os.environ.get("BID_EVAL_COMPILE_CHUNK_CHARS", 12000))


def llm_max_concurrency() -> int:
    return int(os.environ.get("BID_EVAL_LLM_CONCURRENCY", 4))


def llm_concurrency_ceiling() -> int:
    """进程内并发硬上限（旧代码多处 min(8, ...) 把并发静默锁死在 8）。

    2026-11-19 用户要求"每家公司几分钟"：云端模型单次 ~2.4 秒，8 路并发
    是评分慢的主闸门。默认放宽到 32，可经 BID_EVAL_LLM_CONCURRENCY_CEILING 调整。
    """
    return max(1, int(os.environ.get("BID_EVAL_LLM_CONCURRENCY_CEILING", "32")))


def llm_daily_call_budget() -> int:
    """单进程每日调用上限（0 = 不限）。超限时 run 失败并明确报错，不静默降级。"""
    return int(os.environ.get("BID_EVAL_LLM_DAILY_BUDGET", 0))


def llm_disk_cache_enabled() -> bool:
    """LLM 磁盘缓存开关。显式置 0 关、置 1 开；未设置时生产开、pytest 进程内关。

    2026-10-30 阶段 1：chat_json 校验通过的结果落 data/llm_cache/，重启/新
    Gateway 实例复用同 prompt 同参数的已验证输出，省调用与配额。
    pytest 内默认关的实证（2026-10-30）：test_llm_json_repair.py 三个 chat_json
    用例共用同一 (system,user,prompt_version) → 同一缓存键，且不隔离 data_dir；
    缓存开时 test 1 落盘、test 2/3 磁盘命中同一份已验证文本，
    `pytest.raises(LLMError)` 直接 DID NOT RAISE。需要磁盘缓存语义的用例
    显式置 1（见 test_llm_disk_cache.py，配合临时 BID_EVAL_DATA_DIR）。
    """
    raw = os.environ.get("BID_EVAL_LLM_DISK_CACHE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return not os.environ.get("PYTEST_CURRENT_TEST")


def llm_prompt_version() -> str:
    """提示词版本号；进入缓存键与审计记录。改 prompt 必须同步改它。

    2026-10-30 阶段 2：新增 human_only 批量裁决 prompt（auto_judge
    AUTO_JUDGE_BATCH_SYSTEM_PROMPT）——批量 prompt 是新常量，但版本号统一升
    以作废既有磁盘缓存键（新旧 prompt 不得共享缓存命中）。
    2026-11-05：围串标功能下线，<facts> 摘要删除 cross_bidder 两键（提示词内容
    变更），升版作废旧缓存。
    2026-11-18：规则抽取 prompt 修 match 措辞（"满足条件得分"误导计分行标 match，
    利群 7 条实证）+ 权重表禁抽 + 主/客观列最终依据声明，升版作废旧缓存。
    2026-11-19：新增承诺值残差抽取 prompt（VALUE_EXTRACT_*，包A 抽取失败≠结论0），
    升版作废旧缓存（新 prompt 不与旧键共享命中）。
    """
    return os.environ.get("BID_EVAL_PROMPT_VERSION", "2026-11-19.1")


def value_extract_llm_enabled() -> bool:
    """承诺值残差抽取 LLM 轮开关（包A 第 2 步，BID_EVAL_VALUE_EXTRACT_LLM）。

    送审窗口/全文正则都抽不到、但锚词确实命中的格子，把命中文本片段（纯文本，
    零 OCR/零页图）交给 LLM 抽一次值——带逐字引文与数值复算校验。默认开；
    置 0 回退纯确定性路径（抽取失败直接落暂定兜底 value_extraction_unresolved）。
    """
    return os.environ.get("BID_EVAL_VALUE_EXTRACT_LLM", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def veto_batch_size() -> int:
    """human_only 否决格组批裁决的每批条款数（阶段 2 主杠杆，2026-10-30）。

    同一投标人的多条 human_only 条款共享一次选段与 <bid> 块，N 条裁决合成
    一次 LLM 调用（N 次 15k 上下文 → 1 次）。默认 6：单批 6 条 JSON 输出在
    默认 4K 输出预算内稳定不截断（更大批次顶到 max_tokens 上限会整批降级）。
    """
    try:
        n = int(os.environ.get("BID_EVAL_VETO_BATCH_SIZE", 6))
    except ValueError:
        return 6
    return max(1, n)


def veto_human_only_batch_enabled() -> bool:
    """human_only 组批开关（阶段 2，2026-10-30）。默认**关**。

    run 69 实测：组批把 N 条 human_only 并成一次调用——调用数降了（省配额）
    但单次输出 token ×N、并行变串行，veto 墙钟 6.5→9.3min 反而恶化（并发 4 下
    N 个单条并行 < 1 个 N 倍长的批量）。提速目标下默认走单格并行；省 API
    配额场景可置 BID_EVAL_VETO_HUMAN_ONLY_BATCH=1 开启组批。
    """
    return os.environ.get("BID_EVAL_VETO_HUMAN_ONLY_BATCH", "0").strip().lower() not in (
        "0", "false", "no", "off",
    )


def math_skip_llm_enabled() -> bool:
    """历史人工模式 math_relation 冲突跳过 LLM 开关。默认开。

    算术冲突（fail+need_human）由结构化事实比对确定（大小写不一致/超限价/
    分项合计不符均来自 facts 数值比较），LLM 读同一文本无法改变结构化结论
    ——历史路径跳过 LLM 后进入复核；未抽到报价的 unknown 保留 LLM。
    置 0（BID_EVAL_MATH_SKIP_LLM=0）回退冲突格逐格 LLM 旧路径。
    新 managed 自动执行器不采用此开关，冲突和未抽取报价均须自动补证后形成终态，
    无法解决时保留进度并明确失败。
    """
    return os.environ.get("BID_EVAL_MATH_SKIP_LLM", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def math_scope_filter_enabled() -> bool:
    """math_relation 按条款原文范围选择检查项（R45-B，默认开）。

    限价条款只比限价、条款原文明文涉及算术才比大小写/分项合计——
    消除"分项合计冲突挂到最高限价条款"的归因错（测试2/6 误否决根因）。
    置 0（BID_EVAL_MATH_SCOPE_FILTER=0）回退旧无条件三查（限价/大小写/分项
    合计全跑），供 A/B 对照与紧急回退。
    """
    return os.environ.get("BID_EVAL_MATH_SCOPE_FILTER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def veto_llm_enabled() -> bool:
    """资格与符合性 AI 深评默认开启；关闭时不能宣称全自动评审完成。"""
    return os.environ.get("BID_EVAL_VETO_LLM", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def auto_review_rounds() -> int:
    """初评后的业务补评轮数；重检、OCR 和视觉复核共享有界预算。"""
    try:
        return max(0, min(5, int(os.environ.get("BID_EVAL_AUTO_REVIEW_ROUNDS", "2"))))
    except ValueError:
        return 2


def vision_model() -> str | None:
    """视觉评审必须显式指定模型，不把文本模型当成视觉模型。"""
    return os.environ.get("BID_EVAL_VISION_MODEL", "").strip() or None


def vision_base_url() -> str | None:
    return os.environ.get("BID_EVAL_VISION_BASE_URL", "").strip() or llm_base_url()


def vision_api_key() -> str | None:
    return os.environ.get("BID_EVAL_VISION_API_KEY", "").strip() or llm_api_key()


def local_endpoint_hosts() -> list[str]:
    """数据分级：confidential 数据允许出网的端点主机白名单（默认仅本地）。"""
    raw = os.environ.get("BID_EVAL_LOCAL_LLM_HOSTS", "localhost,127.0.0.1,::1")
    return [h.strip() for h in raw.split(",") if h.strip()]


def allow_remote_confidential() -> bool:
    """显式放开"涉密数据只准本地端点"的硬约束（默认关）。

    开启后 confidential 数据可以发往任意外部模型端点（例如硅基流动），
    网关会记一次性告警。这是**合规决策**而不是技术开关：默认必须关，
    由部署方在明确接受"招标/投标原文会离开本机"之后才置 1。
    """
    return os.environ.get("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# 材料审核视觉大模型兜底（2026-11-23 优化②：失败格长图过 VL，页级视觉核验）
# ---------------------------------------------------------------------------
def vl_fallback_enabled() -> bool:
    """VL 兜底总开关（kill switch，回滚置 0）。仅文本链路失败格触发，不碰 OCR。"""
    return os.environ.get("BID_EVAL_VL_FALLBACK", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def vl_facts_enabled() -> bool:
    """VL facts 通道（2026-11-24「VL 报事实、代码算分」）安全阀。

    关（置 0）：计分格 VL 通道降级失败（维持严口径分+疑问），一键止血；
    开（默认）：VL 只报逐案例结构化事实，算分由确定性代码执行。
    """
    return os.environ.get("BID_EVAL_VL_FACTS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def vl_fallback_max_per_cell() -> int:
    """每格 VL 调用次数上限（默认 2）。"""
    try:
        return max(1, int(os.environ.get("BID_EVAL_VL_FALLBACK_MAX_PER_CELL", "2")))
    except ValueError:
        return 2


def vl_fallback_max_per_bidder() -> int:
    """每家 VL 调用总次数上限（默认 30，2026-11-25 翻案通道收口 10→30：
    run160 每家 5+ 翻案格恰打满 10；30=15 格×2 封顶余量，每格 2 不变）。"""
    try:
        return max(1, int(os.environ.get("BID_EVAL_VL_FALLBACK_MAX_PER_BIDDER", "30")))
    except ValueError:
        return 30


def vl_fallback_max_pages_per_segment() -> int:
    """长图分段页数上限（默认 30）。"""
    try:
        return max(1, int(os.environ.get("BID_EVAL_VL_FALLBACK_MAX_PAGES_PER_SEGMENT", "30")))
    except ValueError:
        return 30


def vl_fallback_min_pages() -> int:
    """长图粒度下限（默认 5）——尾段不足时并入前段。"""
    try:
        return max(1, int(os.environ.get("BID_EVAL_VL_FALLBACK_MIN_PAGES", "5")))
    except ValueError:
        return 5


def vl_fallback_max_images() -> int:
    """一页一图多图单请求：每次 VL 调用最多携带的页图数（默认 8）。

    2026-11-23 run 151 实证：整本长图十几 MB 被服务商拒收（HTTP 400）——
    改逐页渲染、限页图数后单请求体积可控（文字仍可读，分辨率不降）。
    """
    try:
        return max(1, int(os.environ.get("BID_EVAL_VL_FALLBACK_MAX_IMAGES", "8")))
    except ValueError:
        return 8


def vl_timeout_s() -> float:
    """VL 兜底单次调用超时秒（默认 90）——不陪跑 llm_timeout_s 的长默认值。"""
    try:
        v = float(os.environ.get("BID_EVAL_VL_TIMEOUT", "90"))
        return v if v > 0 else 90.0
    except ValueError:
        return 90.0


def material_guard_enabled() -> bool:
    """漏评分护栏（2026-11-24 PR4）：目录列有材料页而绑定未闭合时禁止按
    缺件判 0（计入+疑问+护栏报警）。独立开关便于单独回退报警层。"""
    return os.environ.get("BID_EVAL_MATERIAL_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def material_locator_mode() -> str:
    """目录驱动材料页定位（2026-11-24 PR2）：shadow=只写审计 / on=生效 / off=关。

    定位结果只用于存在性信号与护栏（材料页必须被核验到），不做内容判定；
    目录失败不阻断（关键词+material_tags 兜底）。
    """
    raw = os.environ.get("BID_EVAL_MATERIAL_LOCATOR", "shadow").strip().lower()
    return raw if raw in ("shadow", "on", "off") else "shadow"


# ---------------------------------------------------------------------------
# 模型目录（硅基流动，OpenAI 兼容）
# ---------------------------------------------------------------------------
DEFAULT_MODEL_PRESETS: list[dict] = [
    {
        "id": "deepseek-ai/DeepSeek-V4.1-Flash",
        "label": "DeepSeek V4.1 Flash",
        "family": "DeepSeek",
        "vision": True,
        "note": "唯一允许的云端模型（2026-11-23 用户拍板收口）：文本+视觉统一用它，轻推理、JSON 稳定",
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
JOB_STALE_S = 300  # 心跳超时视为 worker 已死，任务可被重新拾取（可经环境变量覆盖）
JOB_MAX_ATTEMPTS = 3  # 僵尸重领上限；超过则判 failed，不再无限重复执行


def job_stale_s() -> int:
    """僵尸判定阈值（秒）。大标书解析等长任务可经 BID_EVAL_JOB_STALE_S 放宽。"""
    return max(60, int(os.environ.get("BID_EVAL_JOB_STALE_S", JOB_STALE_S)))
