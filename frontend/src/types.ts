export type Confidence = "high" | "medium" | "low";

export interface Evidence {
  url: string;
  title: string | null;
  quote: string;
}

export interface DimensionConfidence {
  auth: Confidence;
  access: Confidence;
  api: Confidence;
  mcp: Confidence;
  verdict: Confidence;
}

export interface Report {
  canonical_name: string;
  vendor: string | null;
  homepage: string | null;
  docs_url: string | null;
  category: string;
  one_liner: string;
  auth_methods: string[];
  auth_notes: string;
  access_tier: string;
  access_notes: string;
  api_styles: string[];
  api_breadth: string;
  api_notes: string;
  mcp_status: string;
  mcp_url: string | null;
  verdict: string;
  blocker: string | null;
  integration_notes: string;
  evidence: Evidence[];
  confidence: DimensionConfidence;
  unknowns: string[];
  human_review_needed: boolean;
  human_review_reason: string | null;
}

export interface FieldIssue {
  field: string;
  issue: string;
  severity: Confidence;
}

export interface Verification {
  passed: boolean;
  issues: FieldIssue[];
  followup_queries: string[];
  summary: string;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  calls: number;
  cost_usd: number;
  by_model: Record<string, number>;
}

export interface ToolStats {
  tool_calls: number;
  cache_hits: number;
  distinct_urls_seen: number;
}

export interface RunResult {
  run_id: string;
  app_name: string;
  status?: string;
  served_from_cache?: boolean;
  report: Report | null;
  verification: Verification | null;
  usage: Usage;
  tool_stats: ToolStats;
  retries: number;
  error?: string | null;
}

export interface AgentEvent {
  type: string;
  run_id: string;
  [key: string]: unknown;
}

export interface AgentConfig {
  orchestrator_model: string;
  worker_model: string;
  orchestrator_effort: string;
  max_tool_calls_per_lane: number;
  max_source_chars: number;
  max_verify_retries: number;
  report_cache_ttl_hours: number;
  toolkits: string[];
  lanes: { key: string; label: string }[];
}
