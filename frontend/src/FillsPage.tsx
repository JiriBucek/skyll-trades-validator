import { useEffect, useMemo, useState } from 'react'
import { fetchFills, type FillRow, type FillsHistory } from './api'
import { fmtNet } from './components'

const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function dayOf(ts: string): string {
  return ts.slice(0, 10)
}
function weekdayOf(day: string): number {
  return new Date(day + 'T00:00:00Z').getUTCDay()
}
// is there a Sat/Sun strictly between the older day and the newer day?
function weekendBetween(olderDay: string, newerDay: string): boolean {
  const a = new Date(olderDay + 'T00:00:00Z').getTime()
  const b = new Date(newerDay + 'T00:00:00Z').getTime()
  for (let t = a + 86400000; t < b; t += 86400000) {
    const wd = new Date(t).getUTCDay()
    if (wd === 0 || wd === 6) return true
  }
  return false
}

function PositionBar({ pos, max }: { pos: number; max: number }) {
  // tiny inline bar so you can eyeball the position swing; green long, red short
  const w = max > 0 ? Math.min(100, (Math.abs(pos) / max) * 100) : 0
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="tnum text-[13px] font-bold w-[48px] text-right"
        style={{ color: pos > 1e-9 ? '#15803d' : pos < -1e-9 ? '#b91c1c' : '#94a3b8' }}>
        {fmtNet(pos)}
      </span>
      <span className="inline-block h-2 rounded-sm" style={{
        width: Math.max(2, w * 0.6), background: pos >= 0 ? '#86efac' : '#fca5a5',
      }} />
    </span>
  )
}

type DayGroup = { day: string; fills: FillRow[]; eodPos: number; dayDelta: number }

function WeekendGap() {
  return (
    <tr>
      <td colSpan={8} className="py-0.5">
        <div className="h-[3px] bg-amber-200 rounded-full mx-1" title="weekend gap" />
      </td>
    </tr>
  )
}

// Clickable day header. Collapsed → this row IS the whole day: date + its END-OF-DAY position (where
// the position ended up). Expanded → the day's individual fills follow below. The Δ + position cells
// line up with the fill rows' columns, so the day total sits right above the detail.
function DayHeader({ g, collapsed, onToggle, max }: {
  g: DayGroup; collapsed: boolean; onToggle: () => void; max: number
}) {
  const wd = weekdayOf(g.day)
  const weekend = wd === 0 || wd === 6
  return (
    <tr className="border-y border-slate-200 bg-slate-50 cursor-pointer hover:bg-slate-100" onClick={onToggle}>
      <td colSpan={4} className="py-1 pl-1">
        <span className="inline-flex items-center gap-1.5">
          <span className="text-slate-400 text-[11px] w-3">{collapsed ? '▸' : '▾'}</span>
          <span className={'text-[12px] font-semibold tracking-wide ' + (weekend ? 'text-amber-700' : 'text-slate-600')}>
            {g.day} · {WD[wd]}{weekend ? ' · weekend' : ''}
          </span>
          <span className="text-[11px] text-slate-400">{g.fills.length} fill{g.fills.length > 1 ? 's' : ''}</span>
        </span>
      </td>
      <td className="tnum text-[12px] text-right pr-3" style={{ color: g.dayDelta >= 0 ? '#16a34a' : '#dc2626' }}>
        {g.dayDelta >= 0 ? '+' : ''}{g.dayDelta}
      </td>
      <td className="text-right pr-3"><PositionBar pos={g.eodPos} max={max} /></td>
      <td colSpan={2} className="text-right pr-1 text-[10px] uppercase tracking-wide text-slate-400">EOD</td>
    </tr>
  )
}

function Row({ f, max }: { f: FillRow; max: number }) {
  const buy = f.side === 1
  const stranded = f.trader_id === 0 || f.trader_id === 349
  return (
    <tr className="border-b border-slate-100 hover:bg-slate-50">
      <td className="py-[3px] pl-1 tnum text-[12px] text-slate-600 whitespace-nowrap">
        {f.timestamp.replace('T', ' ').slice(0, 23)}
      </td>
      <td className="tnum text-[12px] font-semibold" style={{ color: buy ? '#15803d' : '#b91c1c' }}>
        {buy ? 'BUY' : 'SELL'}
      </td>
      <td className="tnum text-[12px] text-slate-700 text-right pr-2">{f.qty}</td>
      <td className="tnum text-[12px] text-slate-500 text-right pr-3">{f.price ?? '—'}</td>
      <td className="tnum text-[12px] text-right pr-3" style={{ color: f.delta >= 0 ? '#16a34a' : '#dc2626' }}>
        {f.delta >= 0 ? '+' : ''}{f.delta}
      </td>
      <td className="text-right pr-3"><PositionBar pos={f.running_position} max={max} /></td>
      <td className="text-center">
        {f.linked
          ? <span className="text-green-600" title="aggregated into a trade">✓</span>
          : <span className="text-amber-500" title="not linked to a trade (orphan / pending)">○</span>}
      </td>
      <td className="tnum text-[11px] text-right pr-1"
        style={{ color: stranded ? '#b91c1c' : '#94a3b8' }}
        title={stranded ? 'trader 0/349 — stranded' : `trader ${f.trader_id}`}>
        {f.trader_id}{f.fill_type && f.fill_type !== 'Outright' ? ` ${f.fill_type}` : ''}
      </td>
    </tr>
  )
}

export default function FillsPage({ account, contract }: { account: string; contract: string }) {
  const [data, setData] = useState<FillsHistory | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true); setErr(null)
    fetchFills(account, contract).then(setData).catch((e) => setErr(String(e))).finally(() => setLoading(false))
  }, [account, contract])

  const back = () => { window.location.hash = '' }
  const max = data ? Math.max(1, ...data.fills.map((f) => Math.abs(f.running_position))) : 1

  // group fills by UTC day (rows are newest-first). EOD position = the day's LATEST fill (the first
  // one we encounter); dayDelta = the net the position moved that day.
  const dayGroups = useMemo<DayGroup[]>(() => {
    const groups: DayGroup[] = []
    let cur: DayGroup | null = null
    for (const f of data?.fills ?? []) {
      const day = dayOf(f.timestamp)
      if (!cur || cur.day !== day) {
        cur = { day, fills: [], eodPos: f.running_position, dayDelta: 0 }
        groups.push(cur)
      }
      cur.fills.push(f)
      cur.dayDelta += f.delta
    }
    return groups
  }, [data])

  // collapse-by-day: default expanded; a collapsed day shows only its header (date + EOD position).
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const toggleDay = (day: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      next.has(day) ? next.delete(day) : next.add(day)
      return next
    })
  const everyCollapsed = dayGroups.length > 0 && dayGroups.every((g) => collapsed.has(g.day))
  const toggleAll = () => setCollapsed(everyCollapsed ? new Set() : new Set(dayGroups.map((g) => g.day)))

  const body: JSX.Element[] = []
  let newerDay: string | null = null
  for (const g of dayGroups) {
    if (newerDay && weekendBetween(g.day, newerDay)) body.push(<WeekendGap key={'wg-' + g.day} />)
    const isCollapsed = collapsed.has(g.day)
    body.push(<DayHeader key={'dh-' + g.day} g={g} collapsed={isCollapsed} onToggle={() => toggleDay(g.day)} max={max} />)
    if (!isCollapsed) g.fills.forEach((f, i) => body.push(<Row key={g.day + '-' + i} f={f} max={max} />))
    newerDay = g.day
  }

  return (
    <div className="min-h-screen pb-20">
      <header className="sticky top-0 z-30 bg-white border-b border-slate-200 px-4 py-2.5 shadow-sm">
        <div className="flex items-center gap-3 flex-wrap">
          <button className="text-[12px] rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-100" onClick={back}>
            ← back
          </button>
          {data && data.fills.length > 0 && (
            <button className="text-[12px] rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-100"
              onClick={toggleAll}
              title={everyCollapsed ? 'show every fill' : 'show only one row per day, with the end-of-day position'}>
              {everyCollapsed ? 'Expand days' : 'Collapse days'}
            </button>
          )}
          <h1 className="text-[16px] font-bold text-slate-900">{account} / {contract}</h1>
          {data && (
            <span className="text-[12px] text-slate-500">
              current net <span className="tnum font-semibold" style={{ color: Math.abs(data.current_net) < 1e-9 ? '#94a3b8' : data.current_net > 0 ? '#15803d' : '#b91c1c' }}>{fmtNet(data.current_net)}</span>
              {' · '}{data.total_fills} fills
              {data.first_fill && ` · ${data.first_fill.slice(0, 10)} → ${data.last_fill?.slice(0, 10)}`}
              {data.truncated && <span className="text-amber-700"> · showing latest {data.returned} (running position is still absolute)</span>}
            </span>
          )}
        </div>
        <div className="text-[11px] text-slate-400 mt-1">
          newest fill at the top · <b>running position</b> = signed cumulative qty (buy +, sell −) as of that fill · ✓ = aggregated into a trade
        </div>
      </header>

      <div className="px-4 pt-3">
        {loading && <div className="text-[13px] text-slate-500 py-10 text-center">Loading fills…</div>}
        {err && <div className="text-[13px] text-red-600 py-4">{err}</div>}
        {data && data.fills.length === 0 && <div className="text-[13px] text-slate-500 py-6">No fills for this contract.</div>}
        {data && data.fills.length > 0 && (
          <table className="w-full border-collapse">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-200">
                <th className="py-1 pl-1 font-medium">time (UTC)</th>
                <th className="font-medium">side</th>
                <th className="font-medium text-right pr-2">qty</th>
                <th className="font-medium text-right pr-3">price</th>
                <th className="font-medium text-right pr-3">Δ</th>
                <th className="font-medium text-right pr-3">position</th>
                <th className="font-medium text-center">trade</th>
                <th className="font-medium text-right pr-1">trader</th>
              </tr>
            </thead>
            <tbody>{body}</tbody>
          </table>
        )}
      </div>
    </div>
  )
}
