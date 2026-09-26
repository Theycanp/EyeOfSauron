export type JsonRecord = Record<string, unknown>

export type SourceKind = 'rss' | 'official_list' | 'youtube' | 'x' | 'market' | 'imap' | 'host'
export type IncidentStatus = 'open' | 'recovered' | 'recorded'
export type Tone = 'positive' | 'warning' | 'negative' | 'info' | 'neutral'
export type AdminJobStatus = 'queued' | 'running' | 'succeeded' | 'completed' | 'failed' | 'cancelled' | 'expired'
export type OutboxStatus = 'pending' | 'sending' | 'delivered' | 'dead' | 'cancelled'
export type AdminRole = 'admin' | 'operator' | 'viewer'

export interface AdminIdentity extends JsonRecord {
  id: number | null
  username: string
  display_name: string
  role: AdminRole
  permissions: string[]
  emergency?: boolean
}

export interface AdminUser extends JsonRecord {
  id: number
  username: string
  display_name: string
  role: AdminRole
  enabled: boolean | number
  created_at: number
  updated_at: number
  last_login_at?: number | null
  active_sessions?: number
}

export interface AdminAuthAudit extends JsonRecord {
  id: number
  user_id?: number | null
  username?: string | null
  action: string
  actor: string
  details: JsonRecord
  created_at: number
}

export interface AuthResponse { user: AdminIdentity }
export interface AdminUserResponse { users?: AdminUser[] }
export interface AdminAuthAuditResponse { audit?: AdminAuthAudit[] }

export interface ManagedSource extends JsonRecord {
  id: string
  kind: string
  publisher?: string
  section?: string
  enabled?: boolean
  poll_interval_seconds?: number
  settings?: JsonRecord
  url?: string
  allowed_hosts?: string[]
}

export interface RulePattern extends JsonRecord {
  label?: string
  regex?: string
  title_weight?: number
  summary_weight?: number
}

export interface ManagedRule extends JsonRecord {
  id: string
  kind: string
  source_ids: string[]
  patterns?: RulePattern[]
}

export interface SourceState extends JsonRecord {
  source_id: string
  initialized?: boolean
  last_attempt_at?: number | null
  last_success_at?: number | null
  consecutive_failures?: number
  outage_alerted?: boolean
  last_error?: string | null
  last_duration_ms?: number | null
  last_error_kind?: string | null
  last_http_status?: number | null
  poll_count?: number
  success_count?: number
  failure_count?: number
  last_observation_count?: number | null
  last_alert_count?: number | null
  last_warning?: string | null
  kind?: string | null
  configured_enabled?: boolean | number | null
  runtime_status?: 'disabled' | 'configured' | 'starting' | 'active' | 'degraded' | 'invalid' | 'stale' | null
  expected_interval_seconds?: number | null
  config_revision?: number | null
  registered_at?: number | null
  heartbeat_at?: number | null
  runtime_updated_at?: number | null
  runtime_error?: string | null
}

export interface SourceHealth {
  source_id: string
  as_of: number
  current: SourceState
  polling: {
    basis: 'cumulative_persisted_counters'
    attempts: number
    successes: number
    failures: number
    success_rate: number | null
    window_success_rate: null
  }
  evidence: {
    basis: 'retained_observations_by_ingestion_time'
    since: number
    until: number
    observations_24h: number
    observations_7d: number
    with_full_text: number
    full_text_coverage: number | null
    content_jobs: Record<string, number>
    content_failure_kinds: Record<string, number>
  }
}

export interface SourceQualityAudit {
  id: number
  source_id: string
  action: string
  actor: string
  details: Record<string, unknown>
  created_at: number
}

export interface Reminder extends JsonRecord {
  id: string
  title: string
  message: string
  schedule_kind: 'once' | 'daily'
  run_at?: number | null
  daily_time?: string | null
  timezone?: string | null
  next_run_at?: number | null
  enabled: boolean
  priority: number
  completed_at?: number | null
  last_delivery_status?: string | null
  last_delivery_error?: string | null
}

export interface Incident extends JsonRecord {
  id: string | number
  status: IncidentStatus
  source_ids?: string[]
  latest_alert_id?: number | null
  latest_title?: string | null
  latest_message?: string | null
  latest_click_url?: string | null
  evidence?: string[]
  observation_count?: number
  confidence?: number
  last_seen_at?: number | null
}

export interface AlertDetailRecord extends JsonRecord {
  id: number
  observation_id?: number | null
  incident_id?: number | null
  title: string
  message: string
  priority: number
  confidence: number
  evidence?: string[]
  tags?: string[]
  source_url?: string | null
  status: OutboxStatus
  created_at: number
  delivered_at?: number | null
}

export interface ObservationDetail extends JsonRecord {
  id: number
  source_id: string
  publisher: string
  published_at: number
  fetched_at: number
  title: string
  summary: string
  url: string
  attributes?: JsonRecord
  importance: number
  urgency: number
  relevance: number
  confidence: number
  region: string
  topic: string
  source_tier: string
  information_type: string
  handling: string
}

export type ContentLevel = 'metadata' | 'excerpt' | 'full_text' | 'document' | 'analysis'

export interface ContentDocument extends JsonRecord {
  id: number
  observation_id: number
  level: ContentLevel
  source_method: string
  body: string
  media_type: string
  canonical_url: string
  content_hash: string
  rights_policy: string
  language?: string
  metadata?: JsonRecord
  fetched_at: number
  created_at: number
}

export interface ContentFetchState extends JsonRecord {
  status: 'pending' | 'leased' | 'retry' | 'completed' | 'dead'
  attempts: number
  next_attempt_at?: number | null
  last_error?: string | null
  failure_kind?: string | null
  updated_at: number
  completed_at?: number | null
  dead_at?: number | null
}

export interface ContentAvailability {
  level: Exclude<ContentLevel, 'analysis'>
  fetch_outcome: 'not_requested' | 'available' | 'fetching' | 'retrying' | 'queued' | 'deferred' | 'unavailable' | 'blocked' | 'parser_failed' | 'unsupported'
  has_full_text: boolean
}

export interface AlertDetailResponse {
  alert: AlertDetailRecord
  observation?: ObservationDetail | null
  incident?: Incident | null
  documents?: ContentDocument[]
  content_fetch?: ContentFetchState | null
  content_availability?: ContentAvailability
}

export interface ManualEventDraft {
  title: string
  summary: string
  importance: number
  region: string
  topic: string
  source_url: string
}

export interface ConfigRevision extends JsonRecord {
  id?: number
  revision: number
  actor?: string
  reason?: string
  created_at?: number
  active?: boolean
}

export interface SourceTestResult extends JsonRecord {
  observations: number
  elapsed_ms: number
  not_modified?: boolean
  warnings?: string[]
}

export interface AdminJob extends JsonRecord {
  id: string
  kind?: string
  status: AdminJobStatus
  actor?: string
  created_at?: number
  started_at?: number | null
  completed_at?: number | null
  expires_at?: number | null
  result?: SourceTestResult | null
  error?: string | null
}

export interface NewsCatalogFeed extends JsonRecord {
  id: string
  label: string
  section: string
  url: string
  allowed_hosts: string[]
  max_content_age_seconds?: number
  article_url_prefixes?: string[]
}

export interface NewsCatalogEntry extends JsonRecord {
  id: string
  publisher: string
  homepage_url: string
  access_model: 'public' | 'mixed' | 'subscription' | 'licensed'
  integration_mode: 'verified_rss' | 'verified_official_list' | 'user_confirmed_official_url' | 'licensed_provider'
  source_kind?: SourceKind
  region?: string
  source_tier?: string
  default_importance?: number
  topic?: string
  feeds: NewsCatalogFeed[]
  evidence_url: string
  notes: string
  verified_on: string
  default_enabled: false
  requires_user_confirmation: true
  content_policy: string
}

export interface OutboxAlert extends JsonRecord {
  id: number
  observation_id?: number | null
  incident_id?: number | null
  reminder_id?: string | null
  rule_id: string
  topic: string
  title: string
  message: string
  priority: number
  confidence?: number
  evidence?: string[]
  tags?: string[]
  click_url?: string
  status: OutboxStatus
  attempts: number
  next_attempt_at?: number | null
  lease_until?: number | null
  last_error?: string | null
  failure_kind?: string | null
  created_at: number
  delivered_at?: number | null
  dead_at?: number | null
}

export interface RuntimeStatus extends JsonRecord {
  heartbeat_at?: number | null
  last_heartbeat_at?: number | null
  applied_revision?: number | null
  started_at?: number | null
  instance_id?: string | null
  state?: 'offline' | 'starting' | 'running' | 'degraded' | 'stopping' | 'failed' | null
  heartbeat_age_seconds?: number | null
  applied_at?: number | null
  code_version?: string | null
  configured_sources?: number
  active_sources?: number
  last_error?: string | null
}

export interface AdminStatus extends JsonRecord {
  database_schema?: number
  observations?: number
  sources?: SourceState[]
  outbox?: Record<string, number | null | undefined>
  outbox_metrics?: {
    oldest_pending_age_seconds?: number | null
    retrying?: number | null
  }
  incidents?: Record<string, number | null | undefined>
  reminders?: Record<string, number | null | undefined>
  config_revision?: { revision?: number; created_at?: number; active?: boolean } | null
  desired_revision?: number | null
  runtime?: RuntimeStatus
  engine?: RuntimeStatus
  applied_revision?: number | null
  heartbeat_at?: number | null
}

export interface ConfigResponse {
  managed: { sources?: ManagedSource[]; rules?: ManagedRule[]; analysis?: AnalysisConfig; digest?: DigestConfig }
  revision?: { revision?: number; updated_at?: number | null; updated_by?: string | null; reason?: string | null }
  status?: AdminStatus
}

export interface DigestConfig extends JsonRecord {
  enabled?: boolean
  timezone?: string
  daily_time?: string
  item_limit?: number
  observation_limit?: number
  notify?: boolean
  public_base_url?: string
  api_summary?: boolean
  prompt_id?: string
  prompt_version?: number
}

export interface AnalysisConfig extends JsonRecord {
  enabled?: boolean
  shadow_mode?: boolean
  local_enabled?: boolean
  local_base_url?: string
  local_model?: string
  local_model_fallbacks?: string[]
  api_enabled?: boolean
  api_triage_enabled?: boolean
  api_base_url?: string
  api_model?: string
  api_model_fallbacks?: string[]
  api_key_env?: string
  prompt_id?: string
  prompt_version?: number
  timeout_seconds?: number
  max_input_chars?: number
  max_response_bytes?: number
  max_tokens?: number
  max_items_per_run?: number
  daily_api_budget?: number
  send_full_text?: boolean
  region_weights?: Record<string, number>
}

export interface PromptTemplate extends JsonRecord {
  prompt_id: string
  version: number
  system_text: string
  created_at?: number | null
  actor?: string | null
}

export interface PromptResponse {
  prompts?: PromptTemplate[]
}

export interface DigestSummary extends JsonRecord {
  digest_key: string
  version: number
  period_start: number
  period_end: number
  timezone: string
  title: string
  summary: string
  generation_kind: string
  status: 'published' | 'draft' | 'superseded'
  created_at: number
  published_at?: number | null
  item_count: number
  source_count: number
  web_path?: string
}

export interface DigestProviderAttempt {
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: string | number
  prompt_hash?: string
  status?: string
  error?: string
  elapsed_ms?: string | number
}

export interface DigestGenerationAttempt {
  id: number
  digest_key: string
  status: 'running' | 'succeeded' | 'failed' | 'interrupted'
  started_at: number
  finished_at?: number | null
  error?: string | null
  providers: DigestProviderAttempt[]
}

export interface DigestRun {
  digest_key: string
  state: 'ai_published' | 'generating' | 'retry_exhausted' | 'ai_retrying' | 'algorithm_published' | 'preparation_failed' | 'unpublished'
  published_version: number | null
  generation_kind: string | null
  published_at: number | null
  preparation?: {
    stage: string
    last_error: string
    first_failed_at: number
    last_failed_at: number
    failure_count: number
  } | null
  retry: {
    status: 'pending' | 'succeeded' | 'failed'
    attempts: number
    next_attempt_at: number | null
    retry_deadline_at: number
    last_error?: string | null
    started_at: number
    updated_at: number
  } | null
  attempts: DigestGenerationAttempt[]
  reserved_attempts: number
  attempt_history_available: boolean
  can_retry_now: boolean
}

export interface DigestItem extends JsonRecord {
  cluster_key: string
  /** Stable event identity when the event-centric digest projection is enabled. */
  event_key?: string
  event_id?: string
  title: string
  summary: string
  score: number
  importance: number
  urgency: number
  relevance: number
  confidence: number
  published_at?: number | null
  regions?: string[]
  topics?: string[]
  source_ids?: string[]
  observation_ids?: number[]
  links?: string[]
  source_tiers?: ('primary' | 'secondary' | 'social')[]
  reports?: DigestReport[]
  handling?: 'digest' | 'immediate'
}

export interface DigestReport extends JsonRecord {
  report_id?: string
  observation_id?: number
  source_id?: string
  publisher?: string
  source_tier?: 'primary' | 'secondary' | 'social'
  relation?: string
  match_score?: number
  score?: number
  is_representative?: boolean
  contradicts?: boolean
  published_at?: number | null
  title?: string
  summary?: string
  url?: string
}

export type NewsEventSort = 'newest' | 'importance'

export interface NewsEventReport extends JsonRecord {
  report_id?: number | null
  observation_id: number
  source_id: string
  publisher?: string
  source_tier: 'primary' | 'secondary' | 'social'
  relation: string
  match_score: number
  is_representative: boolean
  published_at: number
  title: string
  summary: string
  url: string
  created_at?: number
}

export interface NewsEvent extends JsonRecord {
  event_key: string
  fingerprint?: string
  title: string
  summary: string
  score: number
  importance: number
  urgency: number
  relevance: number
  confidence: number
  first_seen_at: number
  last_seen_at: number
  regions: string[]
  topics: string[]
  status: 'active' | 'quiet' | 'closed'
  independent_source_count: number
  reports: NewsEventReport[]
  report_count: number
  reports_truncated: boolean
  handling?: 'digest' | 'immediate'
  created_at?: number
  updated_at?: number
  workspace?: EventWorkspaceState
}

export interface EventWorkspaceState {
  read: boolean
  followed: boolean
  ignored: boolean
  digest_choice: 'auto' | 'include' | 'exclude'
  digest_reason?: string
}

export interface EventMatchEvidence {
  score: number
  title_similarity: number
  shared_entities: string[]
  shared_numbers: string[]
  time_distance_hours: number
  semantic_identity: boolean
  generic_title: boolean
  possible_numeric_conflict: boolean
}

export interface EventReviewDetail {
  event_key: string
  requested_key: string
  reports: Array<NewsEventReport & { evidence: EventMatchEvidence }>
  reports_truncated: boolean
  audit: Array<{ id: number; action: string; actor: string; reason: string; created_at: number }>
  state: EventWorkspaceState
  claims?: Array<{
    claim_key: string
    text: string
    status: 'active' | 'superseded' | 'disputed'
    confidence: number
    supersedes_claim_key?: string | null
  }>
  claim_evidence?: Array<{
    claim_key: string
    report_id: number
    stance: 'supports' | 'refutes' | 'context'
    note: string
  }>
  timeline?: Array<{
    timeline_id: number
    occurred_at: number
    kind: string
    text: string
    report_id?: number | null
  }>
  notifications?: Array<{
    id: number
    observation_id: number
    title: string
    status: string
    created_at: number
    delivered_at?: number | null
  }>
  history_truncated?: Record<string, boolean>
}

export interface EventRepairRequest {
  action: 'merge' | 'split'
  event_keys: string[]
  observation_ids?: number[]
}

export interface EventRepairPreview extends EventRepairRequest {
  revision: string
  reports: NewsEventReport[]
  warning: string
  comparisons: Array<EventMatchEvidence & { observation_id: number }>
}

export interface EventQuality {
  event_count: number
  report_count: number
  single_publisher_events: number
  multi_publisher_events: number
  generic_title_events: number
  sample_truncated: boolean
  interpretation: string
  repeated_titles: Array<{ title: string; events: number }>
}

export interface NewsEventResponse {
  events?: NewsEvent[]
  pagination?: PaginationMeta & { has_more?: boolean }
}

export interface SourceQualityProfile extends JsonRecord {
  source_id: string
  weight: number
  automatic_weight?: number
  score: number
  effective_samples: number
  positive_count: number
  negative_count: number
  neutral_count: number
  first_feedback_at?: number | null
  last_feedback_at?: number | null
  evidence_span_days: number
  eligible: boolean
  manual_override?: number | null
  override_reason?: string | null
  updated_at?: number | null
  policy?: JsonRecord
}

export interface SourceQualityResponse {
  profiles?: SourceQualityProfile[]
  logic?: JsonRecord
}

export interface DigestCoverage extends JsonRecord {
  source_id: string
  status: string
  observation_count: number
  last_attempt_at?: number | null
  last_success_at?: number | null
  consecutive_failures?: number
}

export interface DigestDetail extends DigestSummary {
  items?: DigestItem[]
  coverage?: DigestCoverage[]
}

export interface DigestListResponse {
  digests?: DigestSummary[]
  pagination?: PaginationMeta
}

export interface DigestDetailResponse {
  digest: DigestDetail
}

export interface ReminderResponse {
  reminders?: Reminder[]
  server_time?: number
  pagination?: PaginationMeta
}

export interface IncidentResponse {
  incidents?: Incident[]
  pagination?: PaginationMeta
}

export interface RevisionResponse {
  revisions?: ConfigRevision[]
  pagination?: PaginationMeta
}

export interface NewsCatalogResponse {
  sources?: NewsCatalogEntry[]
}

export interface OutboxResponse {
  alerts?: OutboxAlert[]
  pagination?: PaginationMeta
}

export interface JobResponse {
  job: AdminJob
}

export interface PaginationMeta {
  total?: number
  next_cursor?: string | number | null
  truncated?: boolean
}

export interface MutationResponse extends JsonRecord {
  revision?: number
  restart_required?: boolean
}

export interface ResourceState<T> {
  data: T | null
  error: string | null
  updatedAt: number | null
  loading: boolean
}

export type AdminResourceName = 'config' | 'reminders' | 'revisions' | 'incidents' | 'newsCatalog' | 'prompts' | 'sourceQuality'

export interface AdminResources {
  config: ResourceState<ConfigResponse>
  reminders: ResourceState<ReminderResponse>
  revisions: ResourceState<RevisionResponse>
  incidents: ResourceState<IncidentResponse>
  newsCatalog: ResourceState<NewsCatalogResponse>
  prompts: ResourceState<PromptResponse>
  sourceQuality: ResourceState<SourceQualityResponse>
}

export interface HealthSummary {
  level: 'healthy' | 'attention' | 'unknown'
  headline: string
  detail: string
  engineState: 'online' | 'offline' | 'unknown'
  heartbeatAt: number | null
  desiredRevision: number | null
  appliedRevision: number | null
  configPending: boolean | null
  staleSourceIds: string[]
  deadLetters: number
  oldestPendingAgeSeconds: number
}
