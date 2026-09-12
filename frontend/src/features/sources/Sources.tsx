import { forwardRef, useRef, type ChangeEvent, type Dispatch, type SetStateAction } from 'react'
import { Activity, Check, Clock3, ExternalLink, LockKeyhole, Newspaper, Pencil, Plus, Radar, RefreshCw, ShieldAlert, ShieldCheck, Trash2, X } from 'lucide-react'
import type { ManagedSource, NewsCatalogEntry, NewsCatalogFeed, SourceKind, SourceState, Tone } from '../../shared/types'
import { knownSources, relativeTime } from '../../shared/utils'
import { Modal } from '../../shared/ui/Modal'
import { chooseProvider, providerList, providerRegistry, type SourceDraft } from './providers'

interface SourcesPageProps {
  sources: ManagedSource[]
  sourceStates: SourceState[]
  busy: boolean
  query: string
  onQuery: (value: string) => void
  onCreate: (kind: SourceKind | null, mode: 'generic' | 'typed') => void
  catalog: NewsCatalogEntry[]
  catalogError: string | null
  onCatalogFeed: (entry: NewsCatalogEntry, feed: NewsCatalogFeed) => void
  onEdit: (source: ManagedSource) => void
  onToggle: (source: ManagedSource) => void
  onDelete: (source: ManagedSource) => void
}

function sourceStateTone(item?: SourceState): Tone {
  if (!item || item.runtime_status === 'disabled') return 'neutral'
  if (['starting', 'configured'].includes(item.runtime_status || '')) return 'neutral'
  if (item.runtime_status === 'invalid' || item.outage_alerted) return 'negative'
  if (['degraded', 'stale'].includes(item.runtime_status || '') || Number(item.consecutive_failures || 0)) return 'warning'
  return 'positive'
}

function sourceStateLabel(item?: SourceState): string {
  if (!item) return '等待应用'
  if (item.runtime_status === 'disabled') return '已停用'
  if (item.runtime_status === 'starting') return '等待首次检查'
  if (item.runtime_status === 'configured') return '等待应用'
  if (item.runtime_status === 'invalid') return '配置无效'
  if (item.outage_alerted || item.runtime_status === 'degraded') return '异常'
  if (item.runtime_status === 'stale') return '数据过期'
  return Number(item.consecutive_failures || 0) ? '重试中' : '正常'
}

function catalogAccess(entry: NewsCatalogEntry): { label: string; tone: Tone } {
  if (entry.access_model === 'public') return { label: '公开来源', tone: 'positive' }
  if (entry.access_model === 'licensed') return { label: '需要授权', tone: 'warning' }
  if (entry.access_model === 'subscription') return { label: '订阅内容', tone: 'info' }
  return { label: '公开 / 订阅混合', tone: 'neutral' }
}

export function SourcesPage({ sources, sourceStates, busy, query, onQuery, onCreate, catalog, catalogError, onCatalogFeed, onEdit, onToggle, onDelete }: SourcesPageProps) {
  const state = (id: string) => sourceStates.find((item) => item.source_id === id)
  const catalogEntry = (id?: string) => catalog.find((entry) => entry.id === id)
  const tone = (source: ManagedSource): Tone => source.enabled === false ? 'neutral' : sourceStateTone(state(source.id))
  const label = (source: ManagedSource) => source.enabled === false ? '已停用' : sourceStateLabel(state(source.id))
  const filtered = sources.filter((source) => {
    const needle = query.trim().toLowerCase()
    return !needle || [source.publisher, source.id, source.kind, source.section].some((value) => String(value || '').toLowerCase().includes(needle))
  })
  const builtInStates = sourceStates.filter((item) => !sources.some((source) => source.id === item.source_id))
  return <>
    <div className="page-actions"><div><h2>监测来源</h2></div><button className="button primary" onClick={() => onCreate(null, 'generic')}><Plus size={18} />添加来源</button></div>
    <div className="source-kind-grid" aria-label="快速添加来源类型">{providerList.map((provider) => <button key={provider.kind} className="source-kind-card" onClick={() => onCreate(provider.kind, 'typed')}><span className="source-kind-icon"><provider.icon size={21} /></span><span><strong>{provider.label}</strong><small>{provider.description}</small></span><Plus size={17} aria-hidden="true" /></button>)}</div>
    <section className="source-section catalog-section"><div className="section-heading"><div><h3>新闻目录 <span className="section-count">{catalog.length || 0}</span></h3><p>这里是可添加的官方模板；生成草稿后仍需在弹窗中保存，保存前不会开始采集。</p></div><Newspaper size={18} /></div>
      {catalogError && <div className="inline-error" role="status"><ShieldAlert size={17} /><span>新闻目录暂时无法读取：{catalogError}</span></div>}
      {catalog.length ? <div className="catalog-grid">{catalog.map((entry) => {
        const access = catalogAccess(entry)
        return <article className="catalog-card" key={entry.id}><div className="catalog-card-head"><div><h4>{entry.publisher}</h4><a href={entry.homepage_url} target="_blank" rel="noreferrer">官网 <ExternalLink size={13} /></a></div><span className={`status-pill ${access.tone}`}>{access.label}</span></div><p>{entry.notes}</p>{entry.feeds.length ? <div className="catalog-feeds">{entry.feeds.map((feed) => {
          const managed = sources.find((source) => source.settings?.catalog_entry === entry.id && source.settings?.catalog_feed === feed.id) || sources.find((source) => source.id === `${entry.id}_${feed.id}`)
          const runtime = managed ? state(managed.id) : sourceStates.find((source) => source.source_id === `${entry.id}_${feed.id}`)
          const alreadyAdded = Boolean(managed || runtime)
          const statusText = managed?.enabled === false || runtime?.runtime_status === 'disabled' ? '已停用' : managed || runtime ? '已启用' : null
          return <div className="catalog-feed" key={feed.id}><div><strong>{feed.label}</strong><small>{feed.section}</small></div><div className="catalog-feed-action">{statusText && <span className={`status-pill ${statusText === '已启用' ? 'positive' : 'neutral'}`}>{statusText}</span>}<button className="button subtle" type="button" disabled={busy || alreadyAdded} onClick={() => onCatalogFeed(entry, feed)}>{alreadyAdded ? '已添加' : '生成草稿'}</button></div></div>
        })}</div> : <div className="catalog-unavailable"><ShieldAlert size={17} /><span>需要专用授权适配器，不能作为公开 RSS 直接添加。</span></div>}</article>
      })}</div> : !catalogError && <div className="empty-compact catalog-loading"><RefreshCw className="spin" size={19} /><span>正在读取新闻目录…</span></div>}
    </section>
    <div className="filter-row source-filter"><div className="search-field"><Radar size={17} /><input value={query} onChange={(event) => onQuery(event.target.value)} type="search" aria-label="搜索已管理的来源" placeholder="搜索已管理的来源" /></div><span className="result-count">{sources.length} 个自定义来源</span></div>
    <section className="source-section"><div className="section-heading"><div><h3>内置新闻源</h3><p>基础配置由服务器维护，后台不会误改。</p></div><LockKeyhole size={18} /></div><div className="source-cards">{builtInStates.map((item) => <article key={item.source_id} className="source-card"><div className="source-card-top"><div className="source-icon large"><Radar size={20} /></div><span className={`status-pill ${sourceStateTone(item)}`}>{sourceStateLabel(item)}</span></div><h3>{knownSources[item.source_id] || item.source_id}</h3><p>{item.last_success_at ? `${relativeTime(item.last_success_at)}完成检查` : '等待首次检查'}</p><div className="source-card-meta"><span><Clock3 size={14} />每 {Math.max(1, Math.round(Number(item.expected_interval_seconds || 120) / 60))} 分钟</span><span><LockKeyhole size={14} />内置</span></div></article>)}</div></section>
    <section className="source-section"><div className="section-heading"><div><h3>自定义来源</h3><p>股票、账号、频道、网站和邮箱。</p></div></div>{filtered.length ? <div className="source-cards">{filtered.map((source) => {
      const provider = source.kind in providerRegistry ? providerRegistry[source.kind as SourceKind] : null
      const Icon = provider?.icon || Radar
      const sourceCatalogId = typeof source.settings?.catalog_entry === 'string' ? source.settings.catalog_entry : undefined
      const disabledReason = source.enabled === false ? catalogEntry(sourceCatalogId)?.notes : undefined
      return <article key={source.id} className="source-card"><div className="source-card-top"><div className="source-icon large"><Icon size={20} /></div><span className={`status-pill ${tone(source)}`}>{label(source)}</span></div><h3>{source.publisher || source.id}</h3><p>{provider?.label || source.kind} · {source.section}</p>{disabledReason && <p className="source-card-note"><ShieldAlert size={14} />{disabledReason}</p>}<div className="source-card-meta"><span><Clock3 size={14} />每 {Math.round(Number(source.poll_interval_seconds || 300) / 60)} 分钟</span>{state(source.id)?.last_success_at && <span><Check size={14} />{relativeTime(state(source.id)?.last_success_at)}</span>}</div><div className="source-card-actions"><button className="button subtle" disabled={busy} onClick={() => onEdit(source)}><Pencil size={16} />编辑</button><button className="button subtle" disabled={busy} onClick={() => onToggle(source)}>{source.enabled === false ? '启用' : '停用'}</button><button className="icon-button danger" aria-label={`删除来源 ${source.publisher || source.id}`} disabled={busy} onClick={() => onDelete(source)}><Trash2 size={16} /></button></div></article>
    })}</div> : <div className="empty-state compact"><div className="empty-icon"><Radar size={27} /></div><h3>{query ? '没有匹配的来源' : '还没有自定义来源'}</h3><p>{query ? '换一个关键词试试。' : '可以先从一个 RSS、YouTube 频道或股票列表开始。'}</p>{!query && <button className="button primary" onClick={() => onCreate(null, 'generic')}><Plus size={18} />添加第一个</button>}</div>}</section>
  </>
}

interface SourceDialogProps {
  draft: SourceDraft
  setDraft: Dispatch<SetStateAction<SourceDraft>>
  busy: boolean
  testing: boolean
  dirty: boolean
  onClose: () => void
  onSave: () => void
  onTest: () => void
  onCancelTest: () => void
  onResetToPicker: () => void
}

const change = (setDraft: Dispatch<SetStateAction<SourceDraft>>, key: keyof SourceDraft) => (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
  const value = event.target instanceof HTMLInputElement && event.target.type === 'checkbox' ? event.target.checked : event.target.value
  setDraft((current) => ({ ...current, [key]: value }))
}

export const SourceDialog = forwardRef<HTMLDialogElement, SourceDialogProps>(function SourceDialog({ draft, setDraft, busy, testing, dirty, onClose, onSave, onTest, onCancelTest, onResetToPicker }, ref) {
  const formRef = useRef<HTMLFormElement>(null)
  const requestClose = () => {
    if (!dirty || window.confirm('放弃尚未保存的来源修改？')) onClose()
  }
  const choose = (kind: SourceKind) => setDraft((current) => chooseProvider(current, kind))
  const provider = draft.kind ? providerRegistry[draft.kind] : null
  const runTest = () => {
    if (formRef.current?.reportValidity()) onTest()
  }
  return <Modal ref={ref} className="wide-modal" labelledBy="source-dialog-title" onRequestClose={requestClose}>
    <form ref={formRef} className="modal-card" onSubmit={(event) => { event.preventDefault(); onSave() }}>
      <header className="modal-header"><div><span className="eyebrow">{draft.editing ? '编辑来源' : provider ? `添加 ${provider.label}` : '选择来源类型'}</span><h2 id="source-dialog-title">{draft.editing ? draft.publisher || '监测来源' : provider ? `${provider.label} 来源` : '添加监测来源'}</h2></div><button className="icon-button" type="button" aria-label="关闭来源编辑" onClick={requestClose}><X size={19} /></button></header>
      {!provider ? <div className="modal-body provider-step"><ul className="kind-picker large" aria-label="来源类型">{providerList.map((item) => <li key={item.kind}><button type="button" onClick={() => choose(item.kind)}><item.icon size={22} /><span><strong>{item.label}</strong><small>{item.description}</small></span></button></li>)}</ul></div> : <>
        <div className="modal-body">
          <div className="selected-provider"><span className="source-kind-icon"><provider.icon size={20} /></span><div><strong>{provider.label}</strong></div>{!draft.editing && draft.entryMode === 'generic' && <button className="text-button" type="button" onClick={onResetToPicker}>更换类型</button>}</div>
          {draft.kind === 'official_list' && <>
            <label><span>官方公告列表地址</span><input type="url" pattern="https://.*" required value={draft.target} onChange={change(setDraft, 'target')} /><small>{provider.targetHint}</small></label>
            <label><span>公告链接目录</span><textarea required value={draft.articleUrlPrefixes} onChange={change(setDraft, 'articleUrlPrefixes')} placeholder="https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/" /><small>每行一个 HTTPS 目录，以 / 结尾。只收录这些目录下带发布日期的公告。</small></label>
            <label><span>发布日期时区</span><input required value={draft.sourceTimezone} onChange={change(setDraft, 'sourceTimezone')} /></label>
            <label><span>内容保存策略</span><select value={draft.contentPolicy} onChange={change(setDraft, 'contentPolicy')}><option value="feed_metadata_and_original_link_only">保存标题和原文链接</option><option value="public_document_full_text">抓取公开一手文档</option></select></label>
          </>}
          <div className="form-grid two"><label><span>显示名称</span><input value={draft.publisher} onChange={change(setDraft, 'publisher')} required placeholder="例如：公司公告 / 关注的频道" /></label><label><span>分组</span><input value={draft.section} onChange={change(setDraft, 'section')} required placeholder="例如：News" /></label></div>
          {draft.kind === 'rss' && <><label><span>{provider.targetLabel}</span><input value={draft.target} onChange={change(setDraft, 'target')} type="url" pattern="https://.*" required placeholder="https://example.com/feed.xml" /><small>{provider.targetHint}</small></label><div className="form-grid two"><label><span>内容保存策略</span><select value={draft.contentPolicy} onChange={change(setDraft, 'contentPolicy')}><option value="feed_metadata_and_original_link_only">仅 Feed 标题、摘要和链接</option><option value="feed_full_text_allowed">保存 Feed 明确提供的全文</option><option value="public_document_full_text">抓取公开一手文档</option></select><small>商业媒体应保持第一项；只有得到授权的 Feed 才选第二项。</small></label><label><span>内容时效上限（秒）</span><input value={draft.maxContentAgeSeconds} onChange={(event) => setDraft((current) => ({ ...current, maxContentAgeSeconds: Number(event.target.value) }))} type="number" min="0" step="1" required /><small>0 表示不限；86400 秒为 1 天。最新内容超过此时限将报告来源异常。</small></label></div>{draft.contentPolicy === 'public_document_full_text' && <p className="content-policy-warning"><ShieldAlert size={17} /><span><strong>仅用于政府、央行、监管机构等公开一手资料。</strong>系统会访问条目链接并保存纯文本；不要用于付费媒体或未经授权的网页。</span></p>}</>}
          {draft.kind === 'youtube' && <label><span>{provider.targetLabel}</span><input value={draft.target} onChange={change(setDraft, 'target')} pattern="UC[\w-]{20,}" required placeholder="UC 开头的稳定频道 ID" /><small>{provider.targetHint}</small></label>}
          {draft.kind === 'x' && <label><span>{provider.targetLabel}</span><input value={draft.target} onChange={change(setDraft, 'target')} inputMode="numeric" pattern="[0-9]+" required placeholder="平台分配的数字 user ID" /><small>{provider.targetHint}</small></label>}
          {draft.kind === 'market' && <><label><span>{provider.targetLabel}</span><input value={draft.target} onChange={change(setDraft, 'target')} required placeholder="AAPL, MSFT, NVDA" /><small>{provider.targetHint}</small></label><div className="form-grid three"><label><span>涨跌阈值</span><div className="input-suffix"><input value={draft.threshold} onChange={(event) => setDraft((current) => ({ ...current, threshold: Number(event.target.value) }))} type="number" min="0.1" step="0.1" required /><span>%</span></div></label><label><span>跳空阈值</span><div className="input-suffix"><input value={draft.gap} onChange={(event) => setDraft((current) => ({ ...current, gap: Number(event.target.value) }))} type="number" min="0.1" step="0.1" required /><span>%</span></div></label><label><span>成交量倍数</span><div className="input-suffix"><input value={draft.volume} onChange={(event) => setDraft((current) => ({ ...current, volume: Number(event.target.value) }))} type="number" min="1" step="0.1" required /><span>×</span></div></label></div></>}
          {draft.kind === 'imap' && <><div className="form-grid two"><label><span>{provider.targetLabel}</span><input value={draft.target} onChange={change(setDraft, 'target')} required placeholder="imap.example.com" /><small>{provider.targetHint}</small></label><label><span>邮箱文件夹</span><input value={draft.mailbox} onChange={change(setDraft, 'mailbox')} required /></label></div><label><span>搜索条件</span><input value={draft.search} onChange={change(setDraft, 'search')} required placeholder="ALL" /></label></>}
          <section className="rule-section"><div className="section-heading"><div><h3>判断规则</h3><p>决定哪些采集结果值得通过通知渠道发送。</p></div></div>{draft.editing && draft.originalRule && draft.notificationMode === 'preserve' ? <div className="preserve-rule"><ShieldCheck size={19} /><div><strong>保留现有通知规则</strong><span>普通来源编辑不会覆盖当前高级规则。</span></div><button className="button subtle" type="button" onClick={() => setDraft((current) => ({ ...current, notificationMode: 'simple' }))}>替换为简单规则</button></div> : <div className="form-grid two"><label><span>关注关键词（可选）</span><input value={draft.keywords} onChange={change(setDraft, 'keywords')} placeholder="逗号分隔；留空则每条更新都通知" /></label><label><span>排除词（可选）</span><input value={draft.excludes} onChange={change(setDraft, 'excludes')} placeholder="例如：podcast, sponsored" /></label></div>}</section>
          <label className="switch-field"><input checked={draft.enabled} onChange={change(setDraft, 'enabled')} type="checkbox" /><span><strong>保存后启用</strong><small>凭据尚未准备好时建议先关闭</small></span></label>
          <details className="inline-advanced"><summary>凭据与高级选项</summary><div className="advanced-fields"><div className="form-grid two"><label><span>内部标识</span><input value={draft.id} onChange={change(setDraft, 'id')} disabled={draft.editing} pattern="[a-z][a-z0-9_-]{1,63}" required /><small>保存后保持稳定。</small></label>{draft.kind === 'market' && <label><span>冷却时间（秒）</span><input value={draft.cooldown} onChange={(event) => setDraft((current) => ({ ...current, cooldown: Number(event.target.value) }))} type="number" min="60" required /></label>}</div>{draft.kind === 'market' && <div className="form-grid two"><label><span>API Key 环境变量</span><input value={draft.apiKeyEnv} onChange={change(setDraft, 'apiKeyEnv')} required /></label><label><span>API Secret 环境变量</span><input value={draft.apiSecretEnv} onChange={change(setDraft, 'apiSecretEnv')} required /></label><label><span>市场数据 API 地址</span><input value={draft.apiBaseUrl} onChange={change(setDraft, 'apiBaseUrl')} type="url" required /></label></div>}{draft.kind === 'x' && <label><span>Bearer Token 环境变量</span><input value={draft.bearerTokenEnv} onChange={change(setDraft, 'bearerTokenEnv')} required /></label>}{draft.kind === 'imap' && <div className="form-grid two"><label><span>用户名环境变量</span><input value={draft.usernameEnv} onChange={change(setDraft, 'usernameEnv')} required /></label><label><span>密码环境变量</span><input value={draft.passwordEnv} onChange={change(setDraft, 'passwordEnv')} required /></label></div>}<p className="security-note"><ShieldCheck size={17} />这里只填写服务器环境变量名称，绝不填写真实密码或 Token。</p>{draft.originalSource && <p className="preservation-note">未在普通表单中展示的高级字段会原样保留。</p>}</div></details>
        </div>
        <footer className="dialog-actions split"><button className={`button subtle ${testing ? 'danger-text' : ''}`} type="button" disabled={busy} onClick={testing ? onCancelTest : runTest}>{testing ? <X size={17} /> : <Activity size={17} />}{testing ? '取消连接测试' : '测试连接'}</button><div><button className="button subtle" type="button" onClick={requestClose}>取消</button><button className="button primary" type="submit" disabled={busy || testing}>{busy ? <RefreshCw className="spin" size={17} /> : <Check size={17} />}保存来源</button></div></footer>
      </>}
    </form>
  </Modal>
})
