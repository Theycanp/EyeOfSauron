import { ChevronLeft, ChevronRight } from 'lucide-react'

interface PaginationProps {
  page: number
  pageSize: number
  loaded: number
  total?: number
  onPage: (page: number) => void
  noun?: string
}

export function Pagination({ page, pageSize, loaded, total, onPage, noun = '条记录' }: PaginationProps) {
  const localTotal = Math.min(total ?? loaded, loaded)
  const pages = Math.max(1, Math.ceil(localTotal / pageSize))
  const truncated = total !== undefined && total > loaded
  if (pages <= 1 && total === undefined) return loaded ? <p className="loaded-note">已加载最近 {loaded} {noun}</p> : null
  return <nav className="pagination" aria-label="分页">
    <button className="button subtle" type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>
      <ChevronLeft size={16} />上一页
    </button>
    <span aria-live="polite">第 {page} / {pages} 页 · {truncated ? `已加载 ${loaded} / 共 ${total}` : total === undefined ? `已加载 ${loaded}` : `共 ${total}`} {noun}</span>
    <button className="button subtle" type="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>
      下一页<ChevronRight size={16} />
    </button>
  </nav>
}
