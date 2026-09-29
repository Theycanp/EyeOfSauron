import { ArrowLeft, CloudSun, Clock3 } from 'lucide-react'
import type { AlertDetailRecord } from '../../shared/types'
import { formatDate } from '../../shared/utils'

export function WeatherNotificationReader({ alert, onBack }: { alert: AlertDetailRecord; onBack: () => void }) {
  const status = { delivered: '已投递', pending: '待发送', sending: '发送中', dead: '投递失败', cancelled: '已取消' }[alert.status]
  return <article className="event-reader" aria-label="天气通知详情">
    <header className="event-reader-header">
      <div className="page-actions">
        <button className="button subtle" onClick={onBack}><ArrowLeft size={16} />返回事件</button>
        <a className="button primary" href="/#/weather"><CloudSun size={17} />天气主页</a>
      </div>
      <div><span className="eyebrow">天气通知 #{alert.id}</span><h2>{alert.title}</h2>
        <div className="event-reader-meta"><span><Clock3 size={14} />生成 {formatDate(alert.created_at)}</span>
          {alert.delivered_at != null && <span>投递 {formatDate(alert.delivered_at)}</span>}
          <span>{status}</span></div>
      </div>
    </header>
    <section className="panel event-detail-section">
      <div className="panel-header"><h2>通知内容</h2></div>
      <div className="event-detail-copy preserve-lines"><p>{alert.message || '通知内容为空。'}</p></div>
    </section>
  </article>
}
