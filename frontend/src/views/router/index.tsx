import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/router'
const columns = [
  "规则编号",
  "匹配条件",
  "目标服务",
  "权重比例",
  "降级服务",
  "版本号",
  "规则状态"
]
// 动作只在来源状态匹配时可用，状态只能 草稿→生效中→已降级→已关闭 单向推进
const actionByStatus: Record<string, { action?: string; needFallback?: boolean }> = {
  "草稿": { action: "启用规则" },
  "生效中": { action: "降级路由", needFallback: true },
  "已降级": { action: "关闭规则" },
  "已关闭": {},
}
const stats = [
  {
    "label": "路由规则总数",
    "value": 0
  },
  {
    "label": "生效中规则",
    "value": 0
  },
  {
    "label": "已降级规则",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type Receipt = { 动作: string; 时间: string; 版本号: number; 降级服务?: string; 说明?: string }
type DispatchResult = {
  query: string
  effective: Array<Record<string, string | number>>
  degraded: Array<Record<string, string | number>>
  excluded: Array<Record<string, string>>
  total_weight: number
}

async function readMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json()
    if (payload?.message) return String(payload.message)
    if (payload?.detail) return String(payload.detail)
  } catch {
    // 响应体不是 JSON 时退回通用提示
  }
  return fallback
}

export default function RouterView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [dispatch, setDispatch] = useState<DispatchResult | null>(null)
  const filterFields = useMemo(() => ["规则编号", "匹配条件", "目标服务"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('路由规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询路由分发列表读取失败')
    }
  }, [filters])

  const loadDispatch = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/dispatch`)
      if (!response.ok) {
        throw new Error('查询分发结果读取失败')
      }
      setDispatch(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询分发结果读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void loadDispatch()
  }, [reload, loadDispatch])

  async function runAction(action: string, row: Row, needFallback = false) {
    setErrorMessage('')
    let fallback = ''
    if (needFallback) {
      fallback = (window.prompt('请输入明确的降级目标服务（不能为空）：') ?? '').trim()
      if (!fallback) {
        setErrorMessage('降级路由必须指定明确的降级目标服务，已取消本次操作')
        return
      }
    }
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action, 降级服务: fallback }),
      })
      if (!response.ok) {
        throw new Error(await readMessage(response, '查询路由分发动作未生效，请稍后重试'))
      }
      await reload()
      await loadDispatch()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询路由分发操作失败')
    }
  }

  return (
    <section className="page" data-module="router">
      <header className="page-head">
        <div>
          <h2>查询路由分发管理</h2>
          <p className="page-desc">维护查询路由的匹配条件、分发权重与降级策略，为查询路由分发提供可审计的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('路由规则登记入口尚未接入审批流')}>
            登记路由规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出查询路由分发清单
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

      {dispatch && (
        <div className="dispatch-panel">
          <h3>查询路由分发结果（已关闭规则立即退出匹配，结果不含无效目标）</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>规则编号</th>
                <th>状态</th>
                <th>匹配条件</th>
                <th>分发目标</th>
                <th>权重 / 归一化权重</th>
                <th>规则版本</th>
              </tr>
            </thead>
            <tbody>
              {dispatch.effective.map((item) => (
                <tr key={String(item.规则编号)}>
                  <td>{String(item.规则编号)}</td>
                  <td>{String(item.状态)}</td>
                  <td>{String(item.匹配条件)}</td>
                  <td>{String(item.目标服务)}</td>
                  <td>{String(item.权重比例)} / {Number(item.归一化权重).toFixed(4)}</td>
                  <td>v{String(item.版本号)}</td>
                </tr>
              ))}
              {dispatch.degraded.map((item) => (
                <tr key={String(item.规则编号)}>
                  <td>{String(item.规则编号)}</td>
                  <td>{String(item.状态)}</td>
                  <td>{String(item.匹配条件)}</td>
                  <td>{String(item.分发目标)}</td>
                  <td>降级分发</td>
                  <td>v{String(item.版本号)}</td>
                </tr>
              ))}
              {dispatch.effective.length + dispatch.degraded.length === 0 && (
                <tr><td className="empty-state" colSpan={6}>当前没有可分发的有效规则</td></tr>
              )}
            </tbody>
          </table>
          {dispatch.excluded.length > 0 && (
            <p className="page-desc">
              未参与匹配：{dispatch.excluded.map((item) => `${item.规则编号}（${item.状态}：${item.原因}）`).join('；')}
            </p>
          )}
        </div>
      )}

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
            <th>最新回执</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const receipts = Array.isArray(row.回执) ? (row.回执 as Receipt[]) : []
            const latest = receipts[receipts.length - 1]
            const actionConf = actionByStatus[String(row.status ?? '')]
            const nextAction = actionConf?.action
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{row[column] === '' || row[column] == null ? '—' : String(row[column])}</td>)}
                <td>{latest ? `${latest.动作} v${latest.版本号} ${latest.时间}` : '—'}</td>
                <td className="row-actions">
                  {nextAction ? (
                    <button
                      className="link"
                      type="button"
                      onClick={() => void runAction(nextAction, row, actionConf?.needFallback)}
                    >
                      {nextAction}
                    </button>
                  ) : (
                    <span className="muted-text">终态，无可用动作</span>
                  )}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无查询路由分发数据，可先登记路由规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条查询路由分发记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
