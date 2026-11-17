import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/scorecard'
const columns = [
  "评分编号",
  "评分对象",
  "特征变量组",
  "变量分箱",
  "WOE编码",
  "变量权重",
  "基准分数",
  "评分刻度"
]
const nextActionByStatus: Record<string, string | null> = {
  "草稿": "提交评分校验",
  "评分校验中": "启用评分",
  "已启用": "废弃评分",
  "已废弃": null,
}
const RECHECK_ACTION = "复验稳定性"

interface RecheckRecord {
  复验编号: string
  复验时间: string
  来源: string
  关联检测编号: string | null
  监控数据集: string | null
  复验时漂移值: number | null
  复验时漂移阈值: number | null
  阈值是否触发: boolean
  稳定性结论: string
}

interface Row {
  id: number
  status: string
  评分编号: string
  关联检测编号: string | null
  稳定性结论: string
  告警时点特征偏移: Record<string, unknown> | null
  复验明细: RecheckRecord[]
  pending: boolean
  abnormal: boolean
  [key: string]: unknown
}

function asText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function stabilityClass(conclusion: string): string {
  if (conclusion === '漂移告警') return 'badge badge-alert'
  if (conclusion === '检测中断') return 'badge badge-muted'
  if (conclusion === '稳定') return 'badge badge-stable'
  return 'badge badge-muted'
}

function newIdemKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export default function ScorecardView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [dataset, setDataset] = useState<{ total: number; items: RecheckRecord[] } | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      const payload = await response.json().catch(() => null)
      // 业务失败（ok=false）与网络失败都要把后端可读原因暴露出来，不能吞掉
      if (!response.ok || !payload) {
        throw new Error(payload?.message ?? '评分卡列表读取失败')
      }
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '评分卡列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => ([
    { label: "评分卡总数", value: rows.length },
    { label: "已启用评分", value: rows.filter((r) => r.status === "已启用").length },
    { label: "校验中评分", value: rows.filter((r) => r.status === "评分校验中").length },
    { label: "漂移告警评分", value: rows.filter((r) => r.稳定性结论 === "漂移告警").length },
    { label: "检测中断评分", value: rows.filter((r) => r.稳定性结论 === "检测中断").length },
  ]), [rows])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action, idem_key: newIdemKey() }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || !payload || payload.ok === false) {
        setErrorMessage(payload?.message ?? '评分卡动作未生效，请稍后重试')
        return
      }
      setInfoMessage(payload.message ?? '操作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '评分卡操作失败')
    }
  }

  async function runRecheck(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/recheck`, {
        method: 'POST',
        body: JSON.stringify({ idem_key: newIdemKey() }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || !payload || payload.ok === false) {
        setErrorMessage(payload?.message ?? '稳定性复验未生效，请稍后重试')
        return
      }
      setInfoMessage(payload.message ?? '稳定性复验完成')
      setExpandedId(row.id)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '评分卡复验失败')
    }
  }

  async function runReconcile() {
    setErrorMessage('')
    setInfoMessage('')
    try {
      // 收尾第一步：先触发联动对账更新当前界面数据
      const response = await request(`${ENDPOINT}/reconcile`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || !payload || payload.ok === false) {
        setErrorMessage(payload?.message ?? '联动对账失败')
        return
      }
      const entry = payload.entry ?? {}
      setInfoMessage(
        `${payload.message}（校正检测 ${asText(entry.校正检测数)} 个，复验明细 ${asText(entry.生成复验明细数)} 条）`
      )
      // 收尾第二步：刷新业务清单，查关联结果是否已同批回写
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '联动对账失败')
    }
  }

  async function openDataset() {
    try {
      const response = await request(`${ENDPOINT}/recheck-dataset`)
      const payload = await response.json()
      if (!response.ok) throw new Error('复验明细数据集读取失败')
      setDataset({ total: payload.total ?? 0, items: payload.items ?? [] })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '复验明细数据集读取失败')
    }
  }

  return (
    <section className="page" data-module="scorecard">
      <header className="page-head">
        <div>
          <h2>评分卡管理</h2>
          <p className="page-desc">管理用户或业务对象的评分卡模型，定义特征分箱、WOE编码和变量权重；稳定性与数据漂移告警同批联动。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void runReconcile()}>
            联动对账校正
          </button>
          <button className="btn" type="button" onClick={() => void openDataset()}>
            复验明细数据集
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出评分卡清单
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
            <th>状态</th>
            <th>关联漂移检测</th>
            <th>稳定性结论</th>
            <th>复验明细</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const nextAction = nextActionByStatus[row.status] ?? null
            const expanded = expandedId === row.id
            return (
              <Fragment key={String(row.id)}>
                <tr className={row.abnormal ? 'row-alert' : undefined}>
                  {columns.map((column) => <td key={column}>{asText(row[column])}</td>)}
                  <td><span className="badge badge-muted">{row.status}</span></td>
                  <td>{asText(row.关联检测编号)}</td>
                  <td><span className={stabilityClass(row.稳定性结论)}>{asText(row.稳定性结论)}</span></td>
                  <td>
                    <button
                      className="link"
                      type="button"
                      onClick={() => setExpandedId(expanded ? null : row.id)}
                    >
                      {row.复验明细?.length ?? 0} 条{expanded ? '（收起）' : '（查看）'}
                    </button>
                  </td>
                  <td className="row-actions">
                    {nextAction && (
                      <button className="link" type="button" onClick={() => void runAction(nextAction, row)}>
                        {nextAction}
                      </button>
                    )}
                    <button className="link" type="button" onClick={() => void runRecheck(row)}>
                      {RECHECK_ACTION}
                    </button>
                  </td>
                </tr>
                {expanded && (
                  <tr className="detail-row">
                    <td colSpan={columns.length + 5}>
                      <div className="detail-panel">
                        <strong>{row.评分编号} 复验明细（可复核数据集）</strong>
                        {row.告警时点特征偏移 && (
                          <p className="detail-note">
                            告警时点特征偏移已快照保留：漂移值
                            {asText((row.告警时点特征偏移 as Record<string, unknown>).告警时漂移值)}，
                            阈值 {asText((row.告警时点特征偏移 as Record<string, unknown>).漂移阈值)}
                          </p>
                        )}
                        <table className="data-table sub-table">
                          <thead>
                            <tr>
                              <th>复验编号</th><th>复验时间</th><th>来源</th>
                              <th>监控数据集</th><th>漂移值</th><th>阈值</th>
                              <th>是否触发</th><th>稳定性结论</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(row.复验明细 ?? []).map((record) => (
                              <tr key={record.复验编号}>
                                <td>{record.复验编号}</td>
                                <td>{record.复验时间}</td>
                                <td>{record.来源}</td>
                                <td>{asText(record.监控数据集)}</td>
                                <td>{asText(record.复验时漂移值)}</td>
                                <td>{asText(record.复验时漂移阈值)}</td>
                                <td>{record.阈值是否触发 ? '是' : '否'}</td>
                                <td><span className={stabilityClass(record.稳定性结论)}>{record.稳定性结论}</span></td>
                              </tr>
                            ))}
                            {(row.复验明细 ?? []).length === 0 && (
                              <tr><td className="empty-state" colSpan={8}>暂无复验明细</td></tr>
                            )}
                          </tbody>
                        </table>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 5}>暂无评分卡数据，可先登记评分卡</td>
            </tr>
          )}
        </tbody>
      </table>

      {dataset && (
        <div className="modal-mask" onClick={() => setDataset(null)}>
          <div className="modal-panel" onClick={(event) => event.stopPropagation()}>
            <header className="modal-head">
              <strong>校正形成的评分卡复验明细数据集（共 {dataset.total} 条，基准 DATA-0001）</strong>
              <button className="btn ghost" type="button" onClick={() => setDataset(null)}>关闭</button>
            </header>
            <div className="modal-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>评分编号</th><th>复验编号</th><th>来源</th>
                    <th>监控数据集</th><th>漂移值</th><th>阈值</th>
                    <th>稳定性结论</th><th>复验时间</th>
                  </tr>
                </thead>
                <tbody>
                  {dataset.items.map((record: RecheckRecord & { 评分编号?: string }) => (
                    <tr key={record.复验编号}>
                      <td>{asText(record.评分编号)}</td>
                      <td>{record.复验编号}</td>
                      <td>{record.来源}</td>
                      <td>{asText(record.监控数据集)}</td>
                      <td>{asText(record.复验时漂移值)}</td>
                      <td>{asText(record.复验时漂移阈值)}</td>
                      <td><span className={stabilityClass(record.稳定性结论)}>{record.稳定性结论}</span></td>
                      <td>{record.复验时间}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条评分卡记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
