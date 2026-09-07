export type JsonRecord = Record<string, unknown>

export type SourceKind = 'rss' | 'youtube' | 'x' | 'market' | 'imap'
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
  latest_title?: string | null
  latest_message?: string | null
  latest_click_url?: string | null
  evidence?: string[]
  observation_count?: number
  confidence?: number
  last_seen_at?: number | null
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
}

export interface NewsCatalogEntry extends JsonRecord {
  id: string
  publisher: string
  homepage_url: string
  access_model: 'public' | 'mixed' | 'subscription' | 'licensed'
  integration_mode: 'verified_rss' | 'user_confirmed_official_url' | 'licensed_provider'
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
}

export interface AnalysisConfig extends JsonRecord {
  enabled?: boolean
  shadow_mode?: boolean
  local_enabled?: boolean
  local_base_url?: string
  local_model?: string
  api_enabled?: boolean
  api_base_url?: string
  api_model?: string
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

export interface DigestItem extends JsonRecord {
  cluster_key: string
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
  handling?: 'digest' | 'immediate'
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
