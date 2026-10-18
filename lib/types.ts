export type Nullable = number | null;
export interface Coverage {
  first_date: string; last_date: string; rows: number; launch_date: string; base_date: string;
  backcast_rows: number; volume_rows: number; amount_rows: number; valuation_rows: number;
  cross_source_checked: boolean; calendar_verified: boolean; provider_errors: string[]; notes: string[];
  factor_coverage: Record<string, FactorCoverage>;
}
export interface FactorCoverage { total: number; valid: number; warmup: number; missing: number; undefined: number; first_valid: string | null; last_valid: string | null; }
export interface IndexInfo { code: string; name: string; short_name: string; description: string; ready: boolean; coverage: Coverage | null; }
export interface Evaluation {
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
export interface Factor { id: string; name: string; category: string; unit: string; formula: string; description: string; version: number; coverage: FactorCoverage; evaluation: Evaluation; }
export interface Evidence { factor_id: string; name: string; category: string; current: Nullable; percentile: Nullable; direction_ic: Nullable; oos_ic: Nullable; eligible: boolean; score: Nullable; stance: string; }
export interface Price { date: string; close: number; open: Nullable; high: Nullable; low: Nullable; volume: Nullable; amount: Nullable; history_type: string; }
export interface Dashboard {
  code: string; snapshot_id: string; updated_at: string; coverage: Coverage; prices: Price[]; factors: Factor[];
  assessment: { label: string; headline: string; text: string; last_price: number; day_change: Nullable;
    score: Nullable; categories: Record<string, { stance: string; count: number; score: Nullable }>;
    evidence: Evidence[]; highlights: Evidence[]; limitations: string[]; };
}
export interface FactorDetail {
  snapshot_id: string; definition: Factor; coverage: FactorCoverage; evaluation: Evaluation;
  series: { date: string; value: Nullable; status: string; forward_return: Nullable }[];
  rolling: { date: string; ic: Nullable; samples: number }[];
}
