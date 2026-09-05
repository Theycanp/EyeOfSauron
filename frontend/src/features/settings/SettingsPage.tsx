import { useState } from 'react'
import { Check, Database, FileClock, History, Send, Settings2, ShieldCheck } from 'lucide-react'
import type { AdminStatus, ConfigRevision, HealthSummary } from '../../shared/types'
import { formatDate, pageSlice } from '../../shared/utils'
import { Pagination } from '../../shared/ui/Pagination'

interface SettingsPageProps {
  status: AdminStatus
  health: HealthSummary
  revisions: ConfigRevision[]
  revisionTotal?: number
  busy: boolean
  advancedKind: 'sources' | 'rules'
  advancedJson: string
  onAdvancedKind: (kind: 'sources' | 'rules') => void
  onAdvancedJson: (value: string) => void
  onLoadExample: () => void
  onSaveAdvanced: () => void
  onRollback: (revision: ConfigRevision) => void
}

export function SettingsPage({ status, health, revisions, revisionTotal, busy, advancedKind, advancedJson, onAdvancedKind, onAdvancedJson, onLoadExample, onSaveAdvanced, onRollback }: SettingsPageProps) {
  const [page, setPage] = useState(1)
  const pageSize = 10
  const visible = pageSlice(revisions, page, pageSize)
  const desired = health.desiredRevision
  const applied = health.appliedRevision
  const deadLetters = Number(status.outbox?.dead || 0)
  const retrying = Number(status.outbox_metrics?.retrying || 0)
  const pendingAge = Number(status.outbox_metrics?.oldest_pending_age_seconds || 0)
  const deliveryTone = deadLetters ? 'negative' : retrying || pendingAge > 900 ? 'warning' : 'neutral'
  const deliveryLabel = deadLetters ? `${deadLetters} 条死信需处理` : retrying ? `${retrying} 条正在重试` : pendingAge > 900 ? '队列存在滞留' : '队列无异常 · 未主动探测'
  return <>
    <div className="page-actions"><div><h2>设置</h2></div></div>
    <section className="settings-grid">
      <article className="settings-card"><div className="settings-icon"><Send size={21} /></div><div><h3>通知渠道</h3><p>ntfy / <strong>eos</strong>；待发 {Number(status.outbox?.pending || 0)} 条，重试 {retrying} 条，死信 {deadLetters} 条。</p><span className={`status-pill ${deliveryTone}`}>{deliveryLabel}</span></div></article>
      <article className="settings-card"><div className="settings-icon"><Database size={21} /></div><div><h3>状态数据库</h3><p>SQLite schema {status.database_schema || '—'}，共 {Number(status.observations || 0).toLocaleString('zh-CN')} 条观察记录。</p><span className="status-pill positive">管理后台可读取</span></div></article>
      <article className="settings-card"><div className="settings-icon"><ShieldCheck size={21} /></div><div><h3>访问保护</h3><p>仅监听 127.0.0.1:18080，通过 SSH 隧道访问。</p><span className="status-pill positive">仅本机</span></div></article>
      <article className="settings-card config-state-card"><div className="settings-icon"><History size={21} /></div><div><h3>配置应用状态</h3><p>期望修订 {desired ?? '—'}；引擎已应用 {applied ?? '后端未报告'}。</p><span className={`status-pill ${health.configPending ? 'warning' : applied !== null ? 'positive' : 'neutral'}`}>{health.configPending ? '等待 Argus 应用' : applied !== null ? '已验证应用' : '无法验证'}</span></div></article>
    </section>
    <section className="panel settings-panel"><div className="panel-header"><div><h2>配置历史</h2><p>回滚会创建新修订；“已保存”不等于 Argus 已加载。</p></div><span className="revision-chip">期望修订 {desired || 0}</span></div>{visible.length ? <div className="revision-list">{visible.map((revision) => <article key={revision.revision} className="revision-row"><div className="revision-line"><span className="revision-node">{revision.revision === desired && <Check size={14} />}</span></div><div><strong>修订 {revision.revision} {revision.revision === desired && <span className="status-pill info">期望</span>}{revision.revision === applied && <span className="status-pill positive">已应用</span>}</strong><p>{revision.reason || '配置更新'}</p><small>{formatDate(revision.created_at)} · {revision.actor || 'admin'}</small></div>{revision.revision !== desired && <button className="button subtle" disabled={busy} onClick={() => onRollback(revision)}><History size={16} />回滚</button>}</article>)}</div> : <div className="empty-compact"><History size={21} /><span>还没有自定义配置修订</span></div>}<Pagination page={page} pageSize={pageSize} loaded={revisions.length} total={revisionTotal} onPage={setPage} noun="条修订" /></section>
    <details className="advanced-panel"><summary><span><Settings2 size={19} /><span><strong>高级 JSON</strong><small>仅在普通表单无法表达配置时使用</small></span></span></summary><div className="advanced-content"><div className="form-grid two"><label><span>对象类型</span><select value={advancedKind} onChange={(event) => onAdvancedKind(event.target.value as 'sources' | 'rules')}><option value="sources">监测来源</option><option value="rules">判断与通知规则</option></select></label><div className="field-action"><button className="button subtle" type="button" onClick={onLoadExample}><FileClock size={16} />载入示例</button></div></div><label><span>单个 JSON 对象</span><textarea value={advancedJson} onChange={(event) => onAdvancedJson(event.target.value)} className="code-input" rows={14} spellCheck={false} placeholder="在这里粘贴单个来源或规则对象" /></label><div className="dialog-actions"><button className="button primary" disabled={busy || !advancedJson.trim()} onClick={onSaveAdvanced}>保存高级配置</button></div></div></details>
  </>
}
