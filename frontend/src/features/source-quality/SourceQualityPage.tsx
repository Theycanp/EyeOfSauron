import { useState } from 'react'
import { BadgeCheck, Gauge, Minus, Plus, RotateCcw, Save } from 'lucide-react'
import type { SourceQualityProfile } from '../../shared/types'

type Props = {
  profiles: SourceQualityProfile[]
  busy: boolean
  onFeedback: (profile: SourceQualityProfile, signal: -1 | 0 | 1, reason: string) => Promise<void>
  onOverride: (profile: SourceQualityProfile, weight: number, reason: string) => Promise<void>
  onClearOverride: (profile: SourceQualityProfile) => Promise<void>
}

export function SourceQualityPage({ profiles, busy, onFeedback, onOverride, onClearOverride }: Props) {
  const [reason, setReason] = useState<Record<string, string>>({})
  const [weights, setWeights] = useState<Record<string, string>>({})
  const updateReason = (id: string, value: string) => setReason((current) => ({ ...current, [id]: value }))
  return <div className="page-stack">
    <section className="page-heading"><div><p className="eyebrow">长期校准</p><h1>信源质量</h1><p>用长期、可审计的反馈校准日报排序。质量权重不会抑制即时告警。</p></div><div className="heading-mark"><Gauge size={24} /></div></section>
    <section className="panel"><div className="panel-header"><div><h2><BadgeCheck size={17} />计算逻辑</h2><p>自动评分需要足够样本和时间跨度，避免短期噪声造成剧烈变化。</p></div></div><div className="logic-grid"><span>90 天半衰期</span><span>至少 30 个有效样本</span><span>至少覆盖 90 天</span><span>权重范围 0.70–1.15</span><span>每 30 天最多变化 0.05</span><span>只影响日报排序</span></div></section>
    <section className="panel"><div className="panel-header"><div><h2>来源概览</h2><p>没有反馈或证据不足的来源保持 1.00，不会被自动降权。</p></div></div><div className="quality-table-wrap"><table className="quality-table"><thead><tr><th>来源</th><th>权重</th><th>评分</th><th>有效样本</th><th>跨度</th><th>状态</th><th>操作</th></tr></thead><tbody>{profiles.map((profile) => { const sourceReason = reason[profile.source_id] || ''; const weight = weights[profile.source_id] ?? String(profile.weight); return <tr key={profile.source_id}><td><strong>{profile.source_id}</strong>{profile.manual_override !== null && profile.manual_override !== undefined && <small>人工覆盖：{profile.override_reason}</small>}</td><td><strong>{profile.weight.toFixed(2)}</strong><small>自动 {Number(profile.automatic_weight ?? profile.weight).toFixed(2)}</small></td><td>{profile.score.toFixed(2)}</td><td>{profile.effective_samples.toFixed(1)}<small>+{profile.positive_count} / -{profile.negative_count} / {profile.neutral_count}</small></td><td>{profile.evidence_span_days} 天</td><td><span className={`status-pill ${profile.manual_override !== null && profile.manual_override !== undefined ? 'info' : profile.eligible ? 'positive' : 'neutral'}`}>{profile.manual_override !== null && profile.manual_override !== undefined ? '人工覆盖' : profile.eligible ? '自动生效' : '证据不足'}</span></td><td><div className="quality-actions"><input aria-label={`${profile.source_id}反馈原因`} value={sourceReason} onChange={(event) => updateReason(profile.source_id, event.target.value)} placeholder="反馈原因" maxLength={1000} /><div className="icon-action-row"><button className="icon-button" title="正反馈" disabled={busy || !sourceReason.trim()} onClick={() => void onFeedback(profile, 1, sourceReason)}><Plus size={16} /></button><button className="icon-button" title="负反馈" disabled={busy || !sourceReason.trim()} onClick={() => void onFeedback(profile, -1, sourceReason)}><Minus size={16} /></button><input className="weight-input" aria-label={`${profile.source_id}人工权重`} type="number" min="0.7" max="1.15" step="0.01" value={weight} onChange={(event) => setWeights((current) => ({ ...current, [profile.source_id]: event.target.value }))} /><button className="icon-button" title="保存人工权重" disabled={busy || !sourceReason.trim()} onClick={() => void onOverride(profile, Number(weight), sourceReason)}><Save size={16} /></button>{profile.manual_override !== null && profile.manual_override !== undefined && <button className="icon-button" title="清除人工覆盖" disabled={busy} onClick={() => void onClearOverride(profile)}><RotateCcw size={16} /></button>}</div></div></td></tr> })}</tbody></table>{profiles.length === 0 && <div className="empty-compact">还没有可评估的来源。</div>}</div></section>
  </div>
}
