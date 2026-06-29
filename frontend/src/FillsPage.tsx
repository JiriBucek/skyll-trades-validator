import { useEffect, useState } from 'react'
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

function Sep({ day, newer }: { day: string; newer: string | null }) {
  const wd = weekdayOf(day)
  const weekend = wd === 0 || wd === 6
  const gap = newer && weekendBetween(day, newer)
  return (
    <>
      {gap && (
        <tr>
          <td colSpan={8} className="py-0.5">
            <div className="h-[3px] bg-amber-200 rounded-full mx-1" title="weekend gap" />
          </td>
        </tr>
      )}
      <tr>
        <td colSpan={8} className="pt-1.5 pb-0.5">
          <div className={'text-[11px] font-semibold tracking-wide px-1 ' + (weekend ? 'text-amber-700' : 'text-slate-500')}>
            {day} · {WD[wd]}{weekend ? ' · weekend' : ''}
          </div>
        </td>
      </tr>
    </>
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

  // interleave day/weekend separators (rows are latest-first)
  const body: JSX.Element[] = []
  let prevDay: string | null = null
  if (data) {
    data.fills.forEach((f, i) => {
      const day = dayOf(f.timestamp)
      if (day !== prevDay) {
        body.push(<Sep key={'sep-' + day} day={day} newer={prevDay} />)
        prevDay = day
      }
      body.push(<Row key={i} f={f} max={max} />)
    })
  }

  return (
    <div className="min-h-screen pb-20">
      <header className="sticky top-0 z-30 bg-white border-b border-slate-200 px-4 py-2.5 shadow-sm">
        <div className="flex items-center gap-3 flex-wrap">
          <button className="text-[12px] rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-100" onClick={back}>
            ← back
          </button>
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
