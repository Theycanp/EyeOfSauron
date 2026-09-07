import { useEffect, useMemo, useState } from 'react'
import { Check, Cpu, Database, FileClock, History, KeyRound, Network, Save, Send, Settings2, ShieldCheck, Sparkles, WandSparkles } from 'lucide-react'
import type { AdminStatus, AnalysisConfig, ConfigRevision, DigestConfig, HealthSummary, PromptTemplate } from '../../shared/types'
import { formatDate, pageSlice } from '../../shared/utils'
import { Pagination } from '../../shared/ui/Pagination'

interface SettingsPageProps {
  status: AdminStatus; health: HealthSummary; revisions: ConfigRevision[]; revisionTotal?: number; busy: boolean
  analysis?: AnalysisConfig; prompts: PromptTemplate[]; analysisError?: string | null
  onSaveAnalysis: (value: AnalysisConfig) => void
  digest?: DigestConfig; onSaveDigest: (value: DigestConfig) => void
  onSavePrompt: (value: { prompt_id: string; version: number; system_text: string }) => void
  advancedKind: 'sources' | 'rules'; advancedJson: string
  onAdvancedKind: (kind: 'sources' | 'rules') => void; onAdvancedJson: (value: string) => void
  onLoadExample: () => void; onSaveAdvanced: () => void; onRollback: (revision: ConfigRevision) => void
}

const DEFAULT_ANALYSIS: AnalysisConfig = {
  enabled: false, shadow_mode: true, local_enabled: false, api_enabled: false,
  local_base_url: 'http://127.0.0.1:11434/v1', local_model: '', api_base_url: '', api_model: '', api_key_env: '',
  prompt_id: 'triage', prompt_version: 1, timeout_seconds: 20, max_input_chars: 12000,
  max_response_bytes: 65536, max_tokens: 300, max_items_per_run: 50, daily_api_budget: 2,
  send_full_text: false, region_weights: { CN: 5, JP: 4, US: 5, GLOBAL: 3, OTHER: 3 },
}
const DEFAULT_DIGEST: DigestConfig = { enabled: false, timezone: 'Asia/Shanghai', daily_time: '20:00', item_limit: 30, observation_limit: 5000, notify: true, public_base_url: '' }

export function SettingsPage({ status, health, revisions, revisionTotal, busy, analysis, prompts, analysisError, onSaveAnalysis, digest, onSaveDigest, onSavePrompt, advancedKind, advancedJson, onAdvancedKind, onAdvancedJson, onLoadExample, onSaveAdvanced, onRollback }: SettingsPageProps) {
  const [page, setPage] = useState(1)
  const [draft, setDraft] = useState<AnalysisConfig>(DEFAULT_ANALYSIS)
  const [analysisDirty, setAnalysisDirty] = useState(false)
  const [selectedPrompt, setSelectedPrompt] = useState<PromptTemplate | null>(null)
  const [promptId, setPromptId] = useState('triage')
  const [promptVersion, setPromptVersion] = useState(1)
  const [promptText, setPromptText] = useState('')
  const [digestDraft, setDigestDraft] = useState<DigestConfig>(DEFAULT_DIGEST)
  const [digestDirty, setDigestDirty] = useState(false)
  const visible = pageSlice(revisions, page, 10)
  const desired = health.desiredRevision
  const applied = health.appliedRevision
  const deadLetters = Number(status.outbox?.dead || 0)
  const retrying = Number(status.outbox_metrics?.retrying || 0)
  const pendingAge = Number(status.outbox_metrics?.oldest_pending_age_seconds || 0)
  const deliveryTone = deadLetters ? 'negative' : retrying || pendingAge > 900 ? 'warning' : 'neutral'
  const deliveryLabel = deadLetters ? `${deadLetters} 条死信需处理` : retrying ? `${retrying} 条正在重试` : pendingAge > 900 ? '队列存在滞留' : '队列无异常 · 未主动探测'

  useEffect(() => {
    if (analysisDirty) return
    const timer = window.setTimeout(() => setDraft({ ...DEFAULT_ANALYSIS, ...(analysis || {}), region_weights: { ...DEFAULT_ANALYSIS.region_weights, ...(analysis?.region_weights || {}) } }), 0)
    return () => window.clearTimeout(timer)
  }, [analysis, analysisDirty])
  useEffect(() => {
    if (digestDirty) return
    const timer = window.setTimeout(() => setDigestDraft({ ...DEFAULT_DIGEST, ...(digest || {}) }), 0)
    return () => window.clearTimeout(timer)
  }, [digest, digestDirty])

  const editAnalysis = (patch: Partial<AnalysisConfig>) => { setAnalysisDirty(true); setDraft((current) => ({ ...current, ...patch })) }
  const editDigest = (patch: Partial<DigestConfig>) => { setDigestDirty(true); setDigestDraft((current) => ({ ...current, ...patch })) }
  const openPrompt = (prompt?: PromptTemplate) => {
    const item = prompt || prompts.find((entry) => entry.prompt_id === draft.prompt_id && entry.version === Number(draft.prompt_version)) || prompts[0]
    const editing = item || { prompt_id: draft.prompt_id || 'triage', version: Number(draft.prompt_version || 1), system_text: '' }
    setSelectedPrompt(editing); setPromptId(editing.prompt_id)
    setPromptVersion(Number(editing.version)); setPromptText(editing.system_text)
  }
  const promptDirty = selectedPrompt ? promptId !== selectedPrompt.prompt_id || promptVersion !== selectedPrompt.version || promptText !== selectedPrompt.system_text : Boolean(promptText)
  const regionWeights = useMemo(() => ({ ...DEFAULT_ANALYSIS.region_weights, ...(draft.region_weights || {}) }), [draft.region_weights])

  return <>
    <div className="page-actions"><div><h2>设置</h2><p>控制分析策略、来源分层和通知基础设施。</p></div></div>
    <section className="settings-grid">
      <article className="settings-card"><div className="settings-icon"><Send size={21} /></div><div><h3>通知渠道</h3><p>ntfy / <strong>eos</strong>；待发 {Number(status.outbox?.pending || 0)} 条，重试 {retrying} 条，死信 {deadLetters} 条。</p><span className={`status-pill ${deliveryTone}`}>{deliveryLabel}</span></div></article>
      <article className="settings-card"><div className="settings-icon"><Database size={21} /></div><div><h3>状态数据库</h3><p>SQLite schema {status.database_schema || '—'}，共 {Number(status.observations || 0).toLocaleString('zh-CN')} 条观察记录。</p><span className="status-pill positive">通过仓储接口访问</span></div></article>
      <article className="settings-card"><div className="settings-icon"><ShieldCheck size={21} /></div><div><h3>访问保护</h3><p>管理端点由服务认证保护，凭据只保存在当前会话。</p><span className="status-pill positive">已认证</span></div></article>
      <article className="settings-card config-state-card"><div className="settings-icon"><History size={21} /></div><div><h3>配置应用状态</h3><p>期望修订 {desired ?? '—'}；引擎已应用 {applied ?? '后端未报告'}。</p><span className={`status-pill ${health.configPending ? 'warning' : applied !== null ? 'positive' : 'neutral'}`}>{health.configPending ? '等待 Argus 应用' : applied !== null ? '已验证应用' : '无法验证'}</span></div></article>
    </section>

    <section className="panel settings-panel channel-panel"><div className="panel-header"><div><h2><Send size={17} />通知适配器</h2><p>业务只提交标准通知，渠道负责实际投递。</p></div></div><div className="channel-list"><div><span className="channel-icon"><Send size={17} /></span><span><strong>ntfy</strong><small>当前默认渠道 · 复用 outbox、重试与死信审计</small></span><span className="status-pill positive">已接入</span></div><div><span className="channel-icon"><Network size={17} /></span><span><strong>邮件 / Webhook</strong><small>适配接口已预留，尚未配置发送端</small></span><span className="status-pill neutral">未配置</span></div></div></section>

    <section className="panel settings-panel analysis-settings">
      <div className="panel-header"><div><h2><Sparkles size={17} />信息分析策略</h2><p>算法作为底线；模型只辅助，不能单独提升即时通知等级。</p></div><span className={`status-pill ${draft.enabled ? 'positive' : 'neutral'}`}>{draft.enabled ? draft.local_enabled || draft.api_enabled ? '模型已启用' : '仅算法模式' : '已关闭'}</span></div>
      {analysisError && <div className="inline-error"><ShieldCheck size={16} />{analysisError}</div>}
      <div className="settings-form">
        <label className="switch-field prominent"><input type="checkbox" checked={Boolean(draft.enabled)} onChange={(event) => editAnalysis({ enabled: event.target.checked })} /><span><strong>启用语义分析</strong><small>关闭时仍保留原始观察和确定性规则。</small></span></label>
        <label className="switch-field"><input type="checkbox" checked={draft.shadow_mode !== false} disabled={!draft.enabled} onChange={(event) => editAnalysis({ shadow_mode: event.target.checked })} /><span><strong>影子模式</strong><small>只记录模型建议，不改变当前通知行为；建议首次启用时保持开启。</small></span></label>
        <div className="analysis-modes"><label className="switch-field"><input type="checkbox" checked={Boolean(draft.local_enabled)} disabled={!draft.enabled} onChange={(event) => editAnalysis({ local_enabled: event.target.checked })} /><span><strong><Cpu size={15} />本地小模型</strong><small>仅允许回环地址，适合高频预处理。</small></span></label><label className="switch-field"><input type="checkbox" checked={Boolean(draft.api_enabled)} disabled={!draft.enabled} onChange={(event) => editAnalysis({ api_enabled: event.target.checked })} /><span><strong><Network size={15} />远程 API</strong><small>用于低频处理，受每日预算限制。</small></span></label></div>
        <div className="form-grid two"><label><span>本地模型地址</span><input value={draft.local_base_url || ''} onChange={(event) => editAnalysis({ local_base_url: event.target.value })} placeholder="http://127.0.0.1:11434/v1" /></label><label><span>本地模型名称</span><input value={draft.local_model || ''} onChange={(event) => editAnalysis({ local_model: event.target.value })} placeholder="例如 qwen2.5:3b" /></label><label><span>API 地址</span><input value={draft.api_base_url || ''} onChange={(event) => editAnalysis({ api_base_url: event.target.value })} placeholder="https://api.example.com/v1" /></label><label><span>API 模型</span><input value={draft.api_model || ''} onChange={(event) => editAnalysis({ api_model: event.target.value })} placeholder="模型标识" /></label><label><span>API 凭据环境变量</span><span className="input-with-icon"><KeyRound size={15} /><input value={draft.api_key_env || ''} onChange={(event) => editAnalysis({ api_key_env: event.target.value.toUpperCase() })} placeholder="API_KEY_ENV" /></span><small>只保存变量名，不保存 Token。</small></label><label><span>分析 Prompt</span><select value={`${draft.prompt_id || 'triage'}@${draft.prompt_version || 1}`} onChange={(event) => { const [id, version] = event.target.value.split('@'); editAnalysis({ prompt_id: id, prompt_version: Number(version) }) }}>{prompts.length ? prompts.map((prompt) => <option key={`${prompt.prompt_id}@${prompt.version}`} value={`${prompt.prompt_id}@${prompt.version}`}>{prompt.prompt_id} · v{prompt.version}</option>) : <option value="triage@1">triage · v1（内置）</option>}</select></label></div>
        <div className="form-grid four"><label><span>超时（秒）</span><input type="number" min="1" max="120" value={draft.timeout_seconds ?? 20} onChange={(event) => editAnalysis({ timeout_seconds: Number(event.target.value) })} /></label><label><span>每次最多条目</span><input type="number" min="1" max="500" value={draft.max_items_per_run ?? 50} onChange={(event) => editAnalysis({ max_items_per_run: Number(event.target.value) })} /></label><label><span>API 每日预算</span><input type="number" min="0" max="1000" value={draft.daily_api_budget ?? 2} onChange={(event) => editAnalysis({ daily_api_budget: Number(event.target.value) })} /></label><label><span>最大输出 Token</span><input type="number" min="1" max="4096" value={draft.max_tokens ?? 300} onChange={(event) => editAnalysis({ max_tokens: Number(event.target.value) })} /></label></div>
        <fieldset className="region-weights"><legend>地区重要度权重</legend><div className="weight-grid">{Object.entries(regionWeights).map(([region, value]) => <label key={region}><span>{region}</span><input type="number" min="1" max="5" value={value} onChange={(event) => editAnalysis({ region_weights: { ...regionWeights, [region]: Number(event.target.value) } })} /></label>)}</div></fieldset>
        <label className="switch-field"><input type="checkbox" checked={Boolean(draft.send_full_text)} onChange={(event) => editAnalysis({ send_full_text: event.target.checked })} /><span><strong>发送全文给模型</strong><small>关闭可降低隐私暴露和费用，默认只发送标题与摘要。</small></span></label>
        <div className="dialog-actions"><button className="button primary" disabled={busy || !analysisDirty} onClick={() => { onSaveAnalysis(draft); setAnalysisDirty(false) }}><Save size={16} />保存分析配置</button></div>
      </div>
    </section>

    <section className="panel settings-panel prompt-panel"><div className="panel-header"><div><h2><WandSparkles size={17} />Prompt 版本</h2><p>Prompt 集中保存并版本化；新闻正文始终按不可信数据处理。</p></div><button className="button subtle" onClick={() => openPrompt()}><FileClock size={16} />编辑版本</button></div><div className="prompt-list">{prompts.length ? prompts.map((prompt) => <button key={`${prompt.prompt_id}@${prompt.version}`} className={`prompt-row ${selectedPrompt?.prompt_id === prompt.prompt_id && selectedPrompt.version === prompt.version ? 'active' : ''}`} onClick={() => openPrompt(prompt)}><span><strong>{prompt.prompt_id}</strong><small>v{prompt.version} · {prompt.actor || 'system'}</small></span><Check size={16} /></button>) : <div className="empty-compact"><WandSparkles size={20} />尚未加载 Prompt，使用内置 triage@1</div>}</div>{selectedPrompt !== null && <div className="prompt-editor"><div className="form-grid two"><label><span>Prompt ID</span><input value={promptId} onChange={(event) => setPromptId(event.target.value)} pattern="[a-z][a-z0-9_-]{1,63}" /></label><label><span>版本号</span><input type="number" min="1" value={promptVersion} onChange={(event) => setPromptVersion(Number(event.target.value))} /></label></div><label><span>系统 Prompt</span><textarea rows={8} value={promptText} onChange={(event) => setPromptText(event.target.value)} maxLength={32000} /></label><div className="dialog-actions"><button className="button primary" disabled={busy || !promptId.trim() || !promptText.trim() || !promptDirty} onClick={() => onSavePrompt({ prompt_id: promptId.trim(), version: promptVersion, system_text: promptText })}><Save size={16} />保存 Prompt 版本</button></div></div>}</section>

    <section className="panel settings-panel digest-panel"><div className="panel-header"><div><h2><FileClock size={17} />日报与阅读服务</h2><p>聚合普通和重点信息，生成可追溯的版本化摘要。</p></div><span className={`status-pill ${digestDraft.enabled ? 'positive' : 'neutral'}`}>{digestDraft.enabled ? '已启用' : '已关闭'}</span></div><div className="settings-form"><div className="analysis-modes"><label className="switch-field prominent"><input type="checkbox" checked={Boolean(digestDraft.enabled)} onChange={(event) => editDigest({ enabled: event.target.checked })} /><span><strong>生成每日日报</strong><small>按设定时区和时间生成并发布。</small></span></label><label className="switch-field"><input type="checkbox" checked={Boolean(digestDraft.notify)} disabled={!digestDraft.enabled} onChange={(event) => editDigest({ notify: event.target.checked })} /><span><strong>发布后发送通知</strong><small>通知包含可直接打开阅读页的链接。</small></span></label></div><div className="form-grid two"><label><span>生成时间</span><input type="time" value={digestDraft.daily_time || '20:00'} onChange={(event) => editDigest({ daily_time: event.target.value })} /></label><label><span>时区</span><input value={digestDraft.timezone || ''} list="timezone-list" onChange={(event) => editDigest({ timezone: event.target.value })} /></label><label><span>每期最多条目</span><input type="number" min="1" max="100" value={digestDraft.item_limit ?? 30} onChange={(event) => editDigest({ item_limit: Number(event.target.value) })} /></label><label><span>候选观察上限</span><input type="number" min="10" max="10000" value={digestDraft.observation_limit ?? 5000} onChange={(event) => editDigest({ observation_limit: Number(event.target.value) })} /></label></div><label><span>公开阅读服务地址</span><input type="url" value={digestDraft.public_base_url || ''} onChange={(event) => editDigest({ public_base_url: event.target.value })} placeholder="https://eos.example.com" /><small>通知链接使用此地址；留空时使用当前服务地址。</small></label><div className="dialog-actions"><button className="button primary" disabled={busy || !digestDirty} onClick={() => { onSaveDigest(digestDraft); setDigestDirty(false) }}><Save size={16} />保存日报配置</button></div></div></section>

    <section className="panel settings-panel"><div className="panel-header"><div><h2>配置历史</h2><p>回滚会创建新修订；“已保存”不等于 Argus 已加载。</p></div><span className="revision-chip">期望修订 {desired || 0}</span></div>{visible.length ? <div className="revision-list">{visible.map((revision) => <article key={revision.revision} className="revision-row"><div className="revision-line"><span className="revision-node">{revision.revision === desired && <Check size={14} />}</span></div><div><strong>修订 {revision.revision} {revision.revision === desired && <span className="status-pill info">期望</span>}{revision.revision === applied && <span className="status-pill positive">已应用</span>}</strong><p>{revision.reason || '配置更新'}</p><small>{formatDate(revision.created_at)} · {revision.actor || 'admin'}</small></div>{revision.revision !== desired && <button className="button subtle" disabled={busy} onClick={() => onRollback(revision)}><History size={16} />回滚</button>}</article>)}</div> : <div className="empty-compact"><History size={21} /><span>还没有自定义配置修订</span></div>}<Pagination page={page} pageSize={10} loaded={revisions.length} total={revisionTotal} onPage={setPage} noun="条修订" /></section>
    <details className="advanced-panel"><summary><span><Settings2 size={19} /><span><strong>高级 JSON</strong><small>仅在普通表单无法表达配置时使用</small></span></span></summary><div className="advanced-content"><div className="form-grid two"><label><span>对象类型</span><select value={advancedKind} onChange={(event) => onAdvancedKind(event.target.value as 'sources' | 'rules')}><option value="sources">监测来源</option><option value="rules">判断与通知规则</option></select></label><div className="field-action"><button className="button subtle" type="button" onClick={onLoadExample}><FileClock size={16} />载入示例</button></div></div><label><span>单个 JSON 对象</span><textarea value={advancedJson} onChange={(event) => onAdvancedJson(event.target.value)} className="code-input" rows={14} spellCheck={false} placeholder="在这里粘贴单个来源或规则对象" /></label><div className="dialog-actions"><button className="button primary" disabled={busy || !advancedJson.trim()} onClick={onSaveAdvanced}>保存高级配置</button></div></div></details>
  </>
}
