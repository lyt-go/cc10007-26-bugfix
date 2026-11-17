import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/datadrift'
const columns = [
  "检测编号",
  "监控数据集",
  "基线版本",
  "漂移度量",
  "漂移阈值",
  "检测频率",
  "告警接收人",
  "最近漂移值"
]
const nextActionByStatus: Record<string, string | null> = {
  "草稿": "提交规则评审",
  "规则评审中": "启用检测",
  "已启用": "关闭检测",
  "已关闭": null,
}

interface PendingRow {
  检测编号: string
  检测状态: string
  告警状态: string
  监控数据集: string
  漂移阈值: number | null
  最近漂移值: number | null
  评分编号: string
  评分卡状态: string
  稳定性结论: string
  待复核: boolean
  复验明细条数: number
}

interface Row {
  id: number
  status: string
  检测编号: string
  漂移阈值数值: number | null
  最近漂移值数值: number | null
  阈值是否触发: boolean
  告警状态: string
  abnormal: boolean
  [key: string]: unknown
}

function asText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function alertClass(alertState: string): string {
  if (alertState === '告警中') return 'badge badge-alert'
  if (alertState === '告警已关闭') return 'badge badge-muted'
  return 'badge badge-stable'
}

function newIdemKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export default function DatadriftView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [pendingDetail, setPendingDetail] = useState<PendingRow[] | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      const payload = await response.json().catch(() => null)
      // 后端业务原因必须透出到页脚，不能用通用文案覆盖
      if (!response.ok || !payload) {
        throw new Error(payload?.message ?? '数据漂移列表读取失败')
      }
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据漂移列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => ([
    { label: "漂移检测总数", value: rows.length },
    { label: "已启用检测", value: rows.filter((r) => r.status === "已启用").length },
    { label: "评审中检测", value: rows.filter((r) => r.status === "规则评审中").length },
    { label: "告警中", value: rows.filter((r) => r.告警状态 === "告警中").length },
    { label: "已关闭检测", value: rows.filter((r) => r.status === "已关闭").length },
  ]), [rows])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        // 并发请求带幂等键：同批动作重复送达也只生成一条复验明细
        body: JSON.stringify({ action, idem_key: newIdemKey() }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || !payload || payload.ok === false) {
        setErrorMessage(payload?.message ?? '数据漂移动作未生效，请稍后重试')
        return
      }
      setInfoMessage(payload.message ?? '操作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据漂移操作失败')
    }
  }

  async function openPendingDetail() {
    try {
      const response = await request(`${ENDPOINT}/pending-detail`)
      const payload = await response.json()
      if (!response.ok) throw new Error('漂移待处理明细读取失败')
      setPendingDetail(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '漂移待处理明细读取失败')
    }
  }

  return (
    <section className="page" data-module="datadrift">
      <header className="page-head">
        <div>
          <h2>数据漂移管理</h2>
          <p className="page-desc">监控数据分布和特征漂移，对训练数据与生产数据间的分布偏移进行预警；告警状态与评分卡稳定性同批更新。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void openPendingDetail()}>
            数据漂移待处理明细
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据漂移清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>阈值(数值口径)</th>
            <th>最近漂移值</th>
            <th>检测状态</th>
            <th>告警状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const nextAction = nextActionByStatus[row.status] ?? null
            return (
              <Fragment key={String(row.id)}>
                <tr className={row.阈值是否触发 && row.告警状态 === '告警中' ? 'row-alert' : undefined}>
                  {columns.map((column) => <td key={column}>{asText(row[column])}</td>)}
                  <td>{asText(row.漂移阈值数值)}</td>
                  <td>
                    {asText(row.最近漂移值数值)}
                    {row.阈值是否触发 && <span className="badge badge-alert inline-badge">达阈值</span>}
                  </td>
                  <td><span className="badge badge-muted">{row.status}</span></td>
                  <td><span className={alertClass(row.告警状态)}>{asText(row.告警状态)}</span></td>
                  <td className="row-actions">
                    {nextAction ? (
                      <button className="link" type="button" onClick={() => void runAction(nextAction, row)}>
                        {nextAction}
                      </button>
                    ) : (
                      <span className="muted-text">终态无动作</span>
                    )}
                  </td>
                </tr>
              </Fragment>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 5}>暂无数据漂移数据，可先登记漂移检测</td>
            </tr>
          )}
        </tbody>
      </table>

      {pendingDetail && (
        <div className="modal-mask" onClick={() => setPendingDetail(null)}>
          <div className="modal-panel" onClick={(event) => event.stopPropagation()}>
            <header className="modal-head">
              <strong>数据漂移待处理明细（含评分卡复验登记，基准 DATA-0001）</strong>
              <button className="btn ghost" type="button" onClick={() => setPendingDetail(null)}>关闭</button>
            </header>
            <div className="modal-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>检测编号</th><th>检测状态</th><th>告警状态</th>
                    <th>监控数据集</th><th>漂移值</th><th>阈值</th>
                    <th>评分编号</th><th>评分卡状态</th><th>稳定性结论</th>
                    <th>待复核</th><th>复验明细</th>
                  </tr>
                </thead>
                <tbody>
                  {pendingDetail.map((item) => (
                    <tr key={`${item.检测编号}-${item.评分编号}`}>
                      <td>{item.检测编号}</td>
                      <td>{item.检测状态}</td>
                      <td><span className={alertClass(item.告警状态)}>{item.告警状态}</span></td>
                      <td>{item.监控数据集}</td>
                      <td>{asText(item.最近漂移值)}</td>
                      <td>{asText(item.漂移阈值)}</td>
                      <td>{item.评分编号}</td>
                      <td>{item.评分卡状态}</td>
                      <td>{item.稳定性结论}</td>
                      <td>{item.待复核 ? '是' : '否'}</td>
                      <td>{item.复验明细条数} 条</td>
                    </tr>
                  ))}
                  {pendingDetail.length === 0 && (
                    <tr><td className="empty-state" colSpan={11}>暂无待处理明细</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据漂移记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
