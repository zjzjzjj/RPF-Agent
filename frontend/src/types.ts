/** 与后端契约一致的 TS 类型（对应 backend/app/schemas 与 api 层）。 */

export interface Project {
  id: string;
  name: string;
  project_code: string | null;
  purchaser: string | null;
  method: string;
  status: string;
  max_price: string | null;
  currency: string;
  created_at: string;
  updated_at: string;
}

export interface StageState {
  key: string;
  label: string;
  state: "done" | "active" | "blocked" | "pending";
  detail: string;
  todo_count: number;
}

export interface Workbench {
  project: Project;
  stages: StageState[];
  blockers: string[];
}

export interface DocumentOut {
  id: number;
  project_id: string;
  bidder_id: number | null;
  kind: string;
  original_filename: string;
  sha256: string;
  page_count: number | null;
  parse_status: string;
  parse_engine: string | null;
  warnings_json: { code: string; message: string }[] | null;
  data_class: string;
  created_at: string;
}

export interface JobOut {
  id: number;
  project_id: string | null;
  kind: string;
  status: string;
  done_steps: number;
  total_steps: number;
  message: string | null;
  result_json: Record<string, unknown> | null;
  error: string | null;
  attempts: number;
}

export interface LevelOut {
  id: number;
  level_label: string;
  score_min: string;
  score_max: string;
  description: string;
  source_quote: string | null;
}

export interface CriterionOut {
  id: number;
  package_id: number;
  code: string;
  category: string;
  name: string;
  description: string | null;
  max_score: string | null;
  weight: string | null;
  score_type: string;
  is_veto: boolean;
  veto_consequence: string | null;
  required_evidence: string | null;
  source_node_id: string | null;
  source_page: number | null;
  source_quote: string | null;
  status: string;
  edit_json: Record<string, unknown> | null;
  confidence: number | null;
  sort_order: number;
  levels: LevelOut[];
  checks: { check_type: string; field: string | null; params: Record<string, unknown>; on_fail: string }[];
}

export interface ValidationOut {
  code: string;
  severity: string;
  subject_type: string | null;
  subject_id: number | null;
  message: string;
  hint: string | null;
}

export interface PackageDetail {
  id: number;
  project_id: string;
  version: number;
  status: string;
  content_hash: string | null;
  parent_version_id: number | null;
  source_document_id: number | null;
  model: string | null;
  compiled_by: string | null;
  confirmed_by: string | null;
  confirmed_at: string | null;
  frozen_by: string | null;
  frozen_at: string | null;
  criteria: CriterionOut[];
  validations: ValidationOut[];
}

export interface Bidder {
  id: number;
  project_id: string;
  name: string;
  unified_code: string | null;
  bid_price: string | null;
  bid_price_upper: string | null;
  price_breakdown_json: Record<string, unknown> | null;
  price_status: string;
  price_evidence_id: number | null;
  status: string;
}

export interface MatrixCell {
  score_record_id: number;
  criterion_code: string;
  criterion_name: string;
  category: string;
  score_type: string;
  max_score: string | null;
  final_score: string | null;
  machine_score: string | null;
  review_status: string;
  evidence_status: string;
  veto_flag: boolean;
  machine_status: string | null;
}

export interface Matrix {
  run_id: number;
  bidders: { bidder_id: number; name: string; status: string }[];
  criteria: {
    criterion_id: number;
    code: string;
    name: string;
    category: string;
    score_type: string;
    max_score: string | null;
  }[];
  cells: Record<string, Record<string, MatrixCell>>;
}

export interface ScoreOut {
  id: number;
  run_id: number;
  bidder_id: number;
  criterion_id: number;
  rule_version: number;
  package_content_hash: string | null;
  max_score: string | null;
  score_type: string | null;
  veto_flag: boolean;
  machine_score: string | null;
  machine_status: string | null;
  ai_suggested_score: string | null;
  ai_level_label: string | null;
  ai_reasoning: string | null;
  ai_confidence: number | null;
  human_score: string | null;
  human_level_label: string | null;
  final_score: string | null;
  evidence_status: string;
  review_status: string;
  ai_revealed_at: string | null;
  evaluator_actor: string | null;
  confirm_time: string | null;
  warnings_json: { message?: string; formula_explanation?: string }[] | null;
  /** 工作台展示用（只读关联信息） */
  criterion_code: string | null;
  criterion_name: string | null;
  category: string | null;
  required_evidence: string | null;
  source_quote: string | null;
  source_page: number | null;
  levels: { label: string; min: string; max: string; criteria: string }[];
  bidder_name: string | null;
}

export interface Citation {
  id: number;
  document_id: number | null;
  page_no: number | null;
  node_id: string | null;
  quote: string;
  bbox_json: number[][] | null;
  page_size_json: number[] | null;
  verified: boolean;
  verify_method: string | null;
  candidates_json: { page_no: number; quote: string }[] | null;
  citation_role: string;
  created_by: string;
}

export interface RankingRow {
  rank: number;
  bidder_id: number;
  bidder_name: string;
  bid_price: string | null;
  total: string;
  tech_total: string;
  commercial_total: string;
  price_score: string | null;
  status: string;
  veto_flag: boolean;
  tie_break_level: number | null;
  tie_explanations: string[];
  missing_final: string[];
  breakdown: {
    criterion: string;
    code: string;
    category: string;
    final_score: string | null;
    review_status: string;
    veto: boolean;
  }[];
}

export interface Ranking {
  run_id: number | null;
  rows: RankingRow[];
  warnings: string[];
}

/** 报告疑问条目：草稿报告把待处置项逐条标出，复核后刷新即为定稿。 */
export interface OpenQuestion {
  kind: string;
  severity: "high" | "medium" | "low";
  label: string;
  action: string;
  machine_message: string;
  bidder: string;
  bidder_id: number;
  score_id: number;
  code: string;
  criterion: string;
  category: string;
  max_score: string | null;
  final_score: string | null;
  machine_status: string | null;
  review_status: string;
  evidence_status: string;
  source_page: number | null;
  source_quote: string | null;
}

export interface ReportProgress {
  total: number;
  settled: number;
  open: number;
  blocking: number;
  by_kind: Record<string, number>;
  criteria_open: number;
}

/** 按评分项聚合的疑问：同一否决条款在每家投标人上各报一条，聚合后可批量裁决。 */
export interface OpenByCriterion {
  code: string;
  criterion: string;
  category: string;
  max_score: string | null;
  kind: string;
  severity: OpenQuestion["severity"];
  label: string;
  action: string;
  bidders: {
    bidder: string;
    bidder_id: number;
    score_id: number;
    final_score: string | null;
    machine_message: string;
  }[];
}

export interface ReportOut {
  status: "draft" | "final";
  status_label: string;
  progress: ReportProgress;
  open_questions: OpenQuestion[];
  open_by_criterion: OpenByCriterion[];
  project: { id: string; name: string };
  run: { id: number; status: string };
  rule_package: { version: number | null; content_hash: string | null };
  generated_at: string;
  ranking: Ranking;
}

export interface ReviewQueueItem {
  score_record_id: number;
  bidder_id: number;
  bidder_name: string;
  criterion: string;
  code: string;
  score_type: string;
  priority: number;
  review_status: string;
  machine_status: string | null;
  evidence_status: string;
  ai_confidence: number | null;
}

export interface ModelPreset {
  id: string;
  label: string;
  family: string;
  note?: string;
}

export interface ModelsOut {
  default_model: string;
  api_key_configured: boolean;
  models: ModelPreset[];
}

export const CATEGORY_LABELS: Record<string, string> = {
  qualification: "资格性审查",
  compliance: "符合性审查",
  price: "价格分",
  technical: "技术分",
  commercial: "商务分",
};

export const REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: "待复核",
  ai_suggested: "AI 已建议",
  human_confirmed: "已确认",
  human_overridden: "已改分",
  conflict_flagged: "冲突待裁决",
  not_applicable: "不适用",
};
