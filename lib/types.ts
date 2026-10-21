export type Nullable = number | null;
export interface UpdateStatus {
  busy: boolean;
  can_stop?: boolean;
  job: { id: string; status: string; started_at: string; finished_at: string | null;
    details: { active_index?: string; indices?: string[]; trigger?: string; stop_requested_at?: string;
      progress?: { stage: string; stopped_stage?: string; label: string; completed?: Nullable; total?: Nullable; unit?: string | null;
        updated_at: string; completed_bytes?: Nullable; total_bytes?: Nullable; legacy?: boolean; };
      publication?: { status: "publishing" | "published" | "unchanged" | "failed" | "skipped" | "disabled" | "interrupted" | "cancelled";
        export_id?: string; published_at?: string; finished_at?: string; error?: string; };
      results?: { index: string; status: string; last_date?: string; rows: number; warnings?: string[] }[];
      errors?: { index?: string; error: string }[]; }; } | null;
}
export interface Coverage {
  first_date: string; last_date: string; rows: number; launch_date: string; base_date: string;
  backcast_rows: number; volume_rows: number; amount_rows: number; valuation_rows: number;
  cross_source_checked: boolean; calendar_verified: boolean; provider_errors: string[]; notes: string[];
  bond_provider_errors?: string[]; bond_series?: string;
  factor_coverage: Record<string, FactorCoverage>;
}
export interface FactorCoverage { total: number; valid: number; warmup: number; missing: number; undefined: number; first_valid: string | null; last_valid: string | null; }
export interface IndexInfo { code: string; name: string; short_name: string; description: string; ready: boolean; coverage: Coverage | null; }
export interface Evaluation {
  horizon: number;
  full_ic: Nullable; oos_ic: Nullable; recent_ic?: Nullable; samples: number; oos_samples: number;
  ci: { lower: number; upper: number; repetitions: number; block_length: number } | null;
  yearly: { year: string; ic: Nullable; samples: number }[];
  buckets: { bucket: number; count: number; mean_return: Nullable; median_return: Nullable; win_rate: Nullable }[];
  lag: { lag: number; ic: Nullable }[]; direction_consistency: Nullable;
  last_signal_date: string | null; labels_complete_through: string | null;
  predictive_diagnostics?: {
    version: number; status: string; samples: number; lags: number; target_horizon: number;
    forward: { p_value: Nullable; q_value?: Nullable } | null;
    reverse: { p_value: Nullable; q_value?: Nullable } | null;
    oos: { incremental_r2: Nullable; samples: number } | null;
    stationarity: { factor_change_adf_p: number; return_adf_p: number } | null;
    fdr_family_tests?: number;
  };
}
export interface Factor { id: string; name: string; category: string; unit: string; formula: string; description: string; version: number; coverage: FactorCoverage; evaluation: Evaluation; horizons?: number[]; assessment_enabled?: boolean; min_percentile_samples?: number; }
export interface Evidence { factor_id: string; name: string; category: string; current: Nullable; percentile: Nullable; direction_ic: Nullable; oos_ic: Nullable; eligible: boolean; score: Nullable; stance: string; }
export interface Price { date: string; close: number; open: Nullable; high: Nullable; low: Nullable; volume: Nullable; amount: Nullable; history_type: string; }
export interface Dashboard {
  publication_id?: string;
  code: string; snapshot_id: string; updated_at: string; coverage: Coverage; prices: Price[]; factors: Factor[];
  assessment: { label: string; headline: string; text: string; last_price: number; day_change: Nullable;
    relative_value?: { current: Nullable; percentile: Nullable; unit: string; text: string } | null;
    score: Nullable; categories: Record<string, { stance: string; count: number; score: Nullable }>;
    evidence: Evidence[]; highlights: Evidence[]; limitations: string[]; };
}
export interface FactorDetail {
  snapshot_id: string; definition: Factor; coverage: FactorCoverage; evaluation: Evaluation;
  analysis?: FactorAnalysisData | null;
  series: { date: string; value: Nullable; status: string; forward_return: Nullable }[];
  rolling: { date: string; ic: Nullable; samples: number }[];
  components?: SpreadComponent[];
}
export interface ReturnSummary {
  count: number; mean: Nullable; median: Nullable; win_rate: Nullable; p10: Nullable; p90: Nullable;
}
export interface PositionOutcome {
  horizon: number; all_matches: ReturnSummary; non_overlapping: ReturnSummary;
  baseline: ReturnSummary; baseline_non_overlapping: ReturnSummary;
  mean_difference: Nullable; win_rate_difference: Nullable; non_overlapping_mean_difference: Nullable;
  sample_status: "no_matches" | "limited" | "descriptive";
  first_signal: string | null; last_signal: string | null; years: string[];
  relationship: string; interpretation: string;
  examples: { signal_date: string; end_date: string; value: number; percentile: number; return: number }[];
}
export interface FactorAnalysisData {
  analysis_version: string; factor_id: string; as_of: string; current: Nullable; percentile: Nullable;
  percentile_samples: number; minimum_samples: number; constant_window: boolean;
  match_bounds: [number, number] | null; mechanism: string; current_state: string;
  method: { window_sessions: number; match_band_pp: number; minimum_reference_periods: number };
  outcomes: PositionOutcome[];
}
export interface SpreadComponent {
  date: string; observation_date: string | null; available_date: string | null;
  pe_ttm: Nullable; earnings_yield: Nullable; bond_yield: Nullable;
  valuation_source: { source: string; availability_assumed?: boolean } | null;
  bond_source: { source: string; path?: string; fetched_at?: string; sha256?: string; availability_assumed?: boolean } | null;
}
