export type Mode = "demo" | "live";

export interface AuthState {
  mode: Mode;
  live_publishing_enabled: boolean;
  needs_setup: boolean;
  authenticated: boolean;
  csrf?: string;
  user?: { id: number; name: string; email: string };
  workspace?: { id: number; name: string; timezone: string };
  role?: "owner" | "editor" | "viewer";
  invite?: { email: string; role: string };
}

export interface Brand {
  id: number;
  name: string;
  description: string;
  positioning: string;
  voice: string;
  tone: string;
  audience: string;
  location: string;
  goals: string;
  website: string;
  colors: string[];
  visual_guidelines: string;
  approved_claims: string[];
  restricted_claims: string[];
  prohibited_messaging: string[];
  constraints: string;
  permitted_sources: string[];
  logo_asset_id: number | null;
  is_synthetic: boolean;
  content_pillars: string[];
  languages: string[];
  marketing_objectives: string[];
  platform_preferences: string[];
  posting_constraints: { max_posts_per_day?: number; posting_times?: string[]; notes?: string; [k: string]: unknown };
  campaign_budget_usd: number | null;
  fonts: string[];
  profile_sources: { field: string; source_url: string | null; retrieved_at: string; note: string }[];
  catalogue_source: { kind?: string; label?: string; base_url?: string; last_synced_at?: string; last_status?: string; last_error?: string; snapshot?: boolean; products_seen?: number; catalogue_total?: number; image_audit?: { products_total: number; stock_photo_hosts: Record<string, number>; own_upload_hosts: Record<string, number> } };
  festivals: { name: string; nameKn: string | null; slug: string; importance: string; itemsNeeded: string[]; poojaVidhi: string }[];
  articles: { title: string; slug: string; excerpt: string }[];
  updated_at?: string;
}

export interface Freshness {
  state: "fresh" | "aging" | "stale" | "manual";
  age_hours: number | null;
  label: string;
}

export interface Persona {
  id: number;
  name: string;
  description: string;
  goals: string;
  pain_points: string;
  preferred_platforms: string[];
}

export interface Product {
  id: number;
  sku: string;
  name: string;
  category: string;
  collection: string;
  description: string;
  price: number | null;
  currency: string;
  dimensions: string;
  materials: string[];
  finishes: string[];
  verified_attributes: Record<string, string>;
  image_asset_id: number | null;
  is_active: boolean;
  is_synthetic: boolean;
  mrp: number | null;
  stock: number | null;
  allow_backorder: boolean | null;
  variants: { label: string; price: number | null; stock: number | null }[];
  image_urls: string[];
  image_kind: "product_photo" | "stock_photo" | "unverified" | "none";
  name_local: string;
  flags: Record<string, unknown>;
  delivery: { available?: boolean; est_days?: number };
  data_quality: { code: string; message: string }[];
  source_url: string | null;
  source_ref: string | null;
  retrieved_at: string | null;
  freshness: Freshness;
}

export interface RenderCheck {
  code: string;
  status: "pass" | "warn" | "fail";
  message: string;
  layer?: string;
}

export interface Asset {
  id: number;
  kind: string;
  source: string;
  source_label: string;
  filename: string;
  content_type: string;
  size_bytes?: number;
  width: number | null;
  height: number | null;
  alt_text: string;
  url: string;
  placeholder: boolean;
  is_synthetic: boolean;
  origin_url?: string | null;
  sha256?: string;
  campaign_id?: number | null;
  created_at?: string;
  template?: string | null;
  platform?: string | null;
  content_id?: number | null;
  photo_kind?: string | null;
  background?: string | null;
  checks_ok?: boolean | null;
  meta?: {
    template?: string;
    platform?: string;
    format?: { width: number; height: number; label: string };
    variation?: number;
    layers?: { kind: string; text: string; size: number; color: string; contrast: number | null }[];
    checks?: RenderCheck[];
    claim_checks?: Check[];
    notes?: string[];
    photo_kind?: string;
    background?: string;
    text_sources?: Record<string, string | null>;
    content_id?: number;
    content_version?: number;
    ok?: boolean;
    [k: string]: unknown;
  };
}

export interface Finding {
  id: number;
  topic: string;
  statement: string;
  classification: "verified_observation" | "source_reported" | "inference" | "hypothesis" | "unknown";
  source_type: string;
  source_url: string | null;
  source_title: string;
  evidence_excerpt: string;
  retrieved_at: string;
  is_synthetic: boolean;
  run_id: string | null;
}

export interface Observation {
  id: number;
  competitor_id: number;
  source_type: string;
  source_url: string | null;
  content_format: string;
  theme: string;
  excerpt: string;
  cta: string;
  product_categories: string[];
  visible_engagement: Record<string, number> | null;
  is_synthetic: boolean;
  observed_at: string;
  published_at: string | null;
}

export interface Competitor {
  id: number;
  name: string;
  website: string;
  instagram_url: string;
  notes: string;
  is_synthetic: boolean;
  observation_count: number;
  observations?: Observation[];
}

export interface Check {
  code: string;
  status: "pass" | "warn" | "fail";
  message: string;
  claim: string;
  evidence: string;
}

export interface Validation {
  version: number;
  checked_at: string;
  ok: boolean;
  failures: number;
  warnings: number;
  product_claims: Check[];
  brand: Check[];
  platform: Check[];
}

export interface Revision {
  version: number;
  source: string;
  note: string;
  snapshot: Record<string, unknown>;
  edited_by: number | null;
  created_at: string;
}

export interface Attempt {
  id: number;
  adapter: string;
  live: boolean;
  success: boolean;
  external_id: string | null;
  provider_status: string;
  summary: string;
  error: string | null;
  attempted_at: string;
}

export interface Metric {
  id: number;
  reach: number | null;
  views: number | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  saves: number | null;
  source: string;
  captured_at: string;
}

export type ContentStatus =
  | "draft"
  | "in_review"
  | "changes_requested"
  | "approved"
  | "scheduled"
  | "publishing"
  | "published"
  | "failed"
  | "cancelled";

export interface PlatformSpec {
  key: string;
  label: string;
  content_types: string[];
  caption_max: number;
  hashtags_max: number | null;
  carousel_min: number;
  carousel_max: number;
  aspect_min: number;
  aspect_max: number;
  image_types: string[];
  image_max_bytes: number;
  render: { width: number; height: number; label: string; ratio: number };
  requires_media: boolean;
  link_in_caption: boolean;
  tone: string;
  audience_note: string;
  auth: string;
  scopes: string[];
  eligibility: string;
  publish_ops: string;
  analytics: string[];
  rate_limits: string;
  live_adapter: string;
  needs_from_you: string[];
  verified: string;
  sources: string[];
  notes: string[];
}

export interface ContentItem {
  id: number;
  campaign_id: number | null;
  campaign_name: string | null;
  brand_name?: string | null;
  experiment_id: number | null;
  variant_label: string;
  run_id: string | null;
  platform: string;
  master_id: number | null;
  asset_url: string | null;
  approved_hash: string | null;
  content_type: string;
  pillar: string;
  title: string;
  hook: string;
  caption: string;
  hashtags: string[];
  cta: string;
  image_brief: string;
  carousel_outline: string[];
  video_script: string;
  product_ids: number[];
  asset_id: number | null;
  slide_asset_ids: number[];
  validation: Validation | null;
  canva_poster?: {
    job_id?: number;
    design_id?: string;
    asset_id?: number | null;
    mode?: "image-based" | "text-only" | "prompt-led" | "image-to-image";
    phase?: string;
    prompt?: string;
    request_key?: string;
    generation_job_id?: string;
    reference_asset_id?: number | null;
    selected_candidate_id?: string | null;
    candidates?: { id: string; title: string; preview_asset_id: number | null; preview_url?: string }[];
    image_trust?: "uploaded" | "none" | string;
    image_skip_reason?: string | null;
    edit_link_available?: boolean;
    error?: string;
    qa?: { passed: boolean; status: string; issues: string[]; checks?: Record<string, unknown> };
  } | null;
  validation_current: boolean;
  status: ContentStatus;
  connection_id: number | null;
  connection_name: string | null;
  connection_adapter: string | null;
  connection_live: boolean;
  generated_by: string;
  rationale: string;
  version: number;
  approved_version: number | null;
  approved_by: number | null;
  publish_authorized: boolean;
  review_note: string;
  external_id: string | null;
  external_url: string | null;
  publish_verified: boolean;
  last_error: string | null;
  is_synthetic: boolean;
  planned_at: string | null;
  scheduled_at: string | null;
  published_at: string | null;
  planned_local: string | null;
  scheduled_local: string | null;
  updated_at: string;
  // detail-only
  revisions?: Revision[];
  attempts?: Attempt[];
  metrics?: Metric[];
  assets?: Asset[];
  products?: Product[];
  final_caption?: string;
  platform_spec?: PlatformSpec | null;
  approval_intact?: boolean | null;
  family?: ContentItem[];
  // approval-only
  campaign?: { id: number; name: string; objective: string; success_criteria: Criterion[] };
  hypothesis?: Hypothesis;
  evidence?: Finding[];
  target_connections?: Connection[];
}

export interface Criterion {
  metric: string;
  comparator: string;
  target: number;
  variant?: string;
  baseline?: string;
}

export interface Hypothesis {
  id: number;
  campaign_id: number | null;
  statement: string;
  mechanism: string;
  evidence_ids: number[];
  uncertainty: string;
  validation_method: string;
  scores: Record<string, number>;
  total_score: number;
  rank: number;
  selected: boolean;
  status: string;
}

export interface Experiment {
  id: number;
  campaign_id: number | null;
  hypothesis_id: number | null;
  name: string;
  variants: { label: string; description: string }[];
  primary_metric: string;
  success_criteria: Criterion[];
  min_sample_per_variant: number;
  status: string;
  result: {
    per_variant?: Record<string, { n: number; mean_engagement_rate: number | null; mean?: number | null; posts: number[]; missing_metrics: number }>;
    lift?: number;
    p_value?: number;
    method?: string;
    alpha?: number;
    data_source?: string;
    platform?: string | null;
    metric?: string;
    confidence?: string;
    vs_historical_baseline?: { baseline_value: number; baseline_n: number; challenger_mean: number; lift: number; note: string };
  } | null;
  intervention: string;
  baseline: { metric?: string; value?: number | null; n?: number; source?: string; note?: string; filters?: Record<string, unknown> };
  evaluation_window_days: number;
  comparison_method: string;
  confidence: string;
  conclusion: string;
  learnings: string;
  next_recommendation: string;
  data_source: string;
  is_synthetic: boolean;
  created_at: string;
  evaluated_at: string | null;
  hypothesis?: Hypothesis | null;
  variant_posts?: Record<string, number>;
}

export interface Recommendation {
  id: number;
  campaign_id: number | null;
  experiment_id: number | null;
  run_id: string | null;
  title: string;
  detail: string;
  rationale: string;
  evidence: { type: string; ref: string | number; summary: string }[];
  uncertainty: string;
  priority: "high" | "medium" | "low";
  status: string;
  is_synthetic: boolean;
  created_at: string;
  proposed_change: { type?: string; detail?: string; value?: string };
  decided_by: number | null;
  decided_at: string | null;
}

export interface ApprovalRequest {
  id: number;
  run_id: string | null;
  kind: "budget" | "strategy_change" | "paid_promotion" | "account_change";
  title: string;
  detail: string;
  payload: { needed_usd?: number; scope?: string; budget_usd?: number; spent_usd?: number; tool?: string; message?: string };
  status: "pending" | "approved" | "rejected";
  decided_by: number | null;
  decision_note: string;
  requested_at: string;
  decided_at: string | null;
}

export interface ChannelChoice {
  platform: string;
  role: string;
  share: number;
  reason: string;
}

export interface Campaign {
  id: number;
  brand_id: number;
  run_id: string | null;
  name: string;
  objective: string;
  audience: string;
  geography: string;
  language: string;
  platforms: string[];
  pillars: string[];
  creative_direction: string;
  ctas: string[];
  budget_note: string;
  measurement_plan: string;
  success_criteria: Criterion[];
  risks: string[];
  assumptions: string[];
  product_ids: number[];
  status: string;
  is_synthetic: boolean;
  start_date: string | null;
  end_date: string | null;
  created_at: string;
  counts?: Record<string, number>;
  objective_metric: string;
  channel_mix: ChannelChoice[];
  budget_usd: number | null;
  budget_allocation: { item: string; usd: number; note: string }[];
  baseline: { metric?: string; value?: number | null; n?: number; note?: string };
  evaluation_window_days: number;
  strategy: Record<string, unknown>;
}

export interface Connection {
  id: number;
  adapter: string;
  platform: string;
  name: string;
  account_id: string;
  account_label: string;
  status: string;
  token_expires_at: string | null;
  last_verified_at: string | null;
  last_error: string | null;
  config: { url?: string };
  adapter_info: AdapterInfo | null;
  available_in_mode?: boolean;
}

export interface AdapterInfo {
  key: string;
  label: string;
  platform: string;
  external: boolean;
  maturity: string;
  capabilities: {
    oauth: boolean;
    identity: boolean;
    validation: boolean;
    publish: boolean;
    scheduling: string;
    status_verification: boolean;
    analytics: boolean;
    content_types: string[];
  };
}

export type RoleKey = "orchestrator" | "research" | "strategy" | "creative" | "verification" | "distribution" | "analyst";

export interface Step {
  seq: number;
  phase: "observe" | "plan" | "tool" | "evaluate" | "replan" | "decision" | "approval" | "finish" | "error" | "delegate";
  role: RoleKey;
  effect: string;
  cost_usd: number;
  evidence_ids: number[];
  tool: string | null;
  message: string;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  status: "ok" | "error" | "skipped" | "blocked";
  is_mock: boolean;
  attempt: number;
  duration_ms: number;
  tokens: number;
  created_at: string;
}

export interface PlanStep {
  id: string;
  tool: string;
  purpose: string;
  status: string;
  note?: string;
  inserted?: boolean;
  after_approval?: boolean;
  role?: RoleKey;
}

export interface Run {
  id: string;
  goal: string;
  intent: string;
  planner: string;
  status: "queued" | "running" | "paused" | "awaiting_approval" | "completed" | "blocked" | "failed" | "cancelled";
  cost_usd: number;
  cost_budget_usd: number;
  pause_requested: boolean;
  recoveries: number;
  approved_extra_usd?: number;
  plan: PlanStep[];
  step_count: number;
  max_steps: number;
  tokens_used: number;
  token_budget: number;
  cancel_requested: boolean;
  outcome_summary: string;
  completion_reason: string;
  blockers: string[];
  error: string | null;
  campaign_id: number | null;
  brand_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  outputs: Record<string, unknown>;
  params: Record<string, unknown>;
  steps?: Step[];
}

export interface Group {
  key: string;
  n: number;
  mean_engagement_rate: number;
  mean_saves_per_reach?: number | null;
  min: number;
  max: number;
  reach: number;
  interactions: number;
}

export interface Analytics {
  data_source: "synthetic" | "real" | "mixed" | "none";
  freshness: string | null;
  published: number;
  with_metrics: number;
  missing_metrics: number;
  totals: Record<string, number | null>;
  by_content_type: Group[];
  by_pillar: Group[];
  by_platform: Group[];
  by_campaign: Group[];
  daily: { date: string; reach: number; interactions: number; posts: number }[];
  items: {
    id: number;
    title: string;
    platform: string;
    content_type: string;
    pillar: string;
    variant: string;
    published_at: string | null;
    source: string | null;
    reach: number | null;
    interactions: number | null;
    engagement_rate: number | null;
    saves: number | null;
  }[];
  definitions: Record<string, { label: string; kind: string; definition: string }>;
}

export interface ReportSummary {
  id: number;
  campaign_id: number | null;
  run_id: string | null;
  title: string;
  data_source: string;
  created_at: string;
  markdown?: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  permission: string;
  implementation: string;
  role: RoleKey;
  role_label: string;
  effects: string[];
  input_schema: Record<string, unknown>;
}

export interface UsageSummary {
  month_start: string;
  month_cost_usd: number;
  monthly_budget_usd: number | null;
  run_budget_usd: number | null;
  approval_threshold_usd: number | null;
  search_requests_per_run: number | null;
  search_requests_per_day: number | null;
  by_provider: { provider: string; operation: string; calls: number; input_tokens: number; output_tokens: number; units: number; cost_usd: number; cached: number }[];
  prices: { checked: string; token_prices_per_million: Record<string, [number, number]>; unit_prices: Record<string, number> };
  note: string;
  recent: { provider: string; operation: string; model: string; run_id: string | null; input_tokens: number; output_tokens: number; units: number; cost_usd: number; cached: boolean; created_at: string }[];
  runs: { id: string; goal: string; status: string; cost_usd: number; budget_usd: number; tokens: number; created_at: string }[];
}

// ── campaign generation (research → strategy → captions → one image per day) ──
export type GenerationStatus = "queued" | "running" | "completed" | "partial" | "failed";
export type GenerationStage = "research" | "strategy" | "text" | "images" | "done";
export type DayStatus = "pending" | "generating" | "completed" | "failed";

export interface ResearchSource { id: string; title: string; url: string; snippet?: string; age?: string | null; query: string; cached?: boolean }

export interface GenerationDay {
  day: number;
  status: DayStatus;
  attempts: number;
  error: string;
  image_prompt: { subject?: string; style?: string; composition?: string; palette?: string; negative?: string; prompt?: string; visual_style?: string } | null;
  asset: Asset | null;
  ai_asset: Asset | null;
  content: {
    id: number; status: ContentStatus; version: number; approved_version: number | null; title: string; hook: string;
    caption: string; hashtags: string[]; cta: string; planned_at: string | null; review_note: string;
    validation_failures: number | null; validation_warnings: number | null;
  } | null;
}

export interface CampaignGeneration {
  id: number;
  campaign_id: number;
  brand_id: number;
  goal: string;
  days: number;
  platform: string;
  aspect_ratio: string;
  status: GenerationStatus;
  stage: GenerationStage;
  error: string;
  image_mode: "ai" | "local";
  image_model: string;
  budget_usd: number;
  created_at: string;
  updated_at: string;
  research: {
    status?: string; mode?: string; queries?: string[]; sources?: ResearchSource[]; errors?: { query: string; error: string }[];
    assumptions?: string[]; searched_at?: string; storage_rights?: boolean; questions: { question: string; query: string }[];
  };
  strategy: {
    title: string; objective: string; audience: string; theme: string; voice: string; positioning: string; platforms: string[];
    product_ids: number[]; findings: { finding: string; source_ids: string[] }[]; assumptions: string[];
    days: { day: number; objective: string; concept: string; caption_brief: string; cta: string; product_ids: number[]; source_ids: string[] }[];
  } | null;
  day_items: GenerationDay[];
  usage: { brave_requests: number; brave_cached: number; llm_requests: number; llm_tokens: number; image_requests: number; images_generated: number; cost_usd: number };
  result?: { approved_days: number[]; skipped: { day: number; reason: string }[] };
}

export interface GenerationEstimate {
  days: number; image_mode: "ai" | "local"; image_model: string; image_size: string; brave_requests: number; llm_requests: number;
  image_requests: number; max_image_requests: number; price_per_image_usd: number; expected_cost_usd: number; max_cost_usd: number;
  budget_usd: number; within_budget: boolean; brands: { id: number; name: string }[];
}
