import { useEffect, useState } from 'react'
import { fetchDiff, fetchOverview, type DiffResult, type Overview } from './api'
import { GroupRow, Legend, SummaryChips, fmtNet } from './components'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function DayAxis({ days }: { days: string[] }) {
  return (
    <div className="flex items-end" style={{ paddingLeft: 360, height: 18 }}>
      {days.map((d) => {
        const dt = new Date(d + 'T00:00:00Z')
        const wd = dt.getUTCDay()
        const dom = dt.getUTCDate()
        const monday = wd === 1
        const label = dom <= 7 && monday ? MONTHS[dt.getUTCMonth()] : monday ? `${dom}` : ''
        return (
          <div key={d} style={{ width: 13, marginRight: 1, marginLeft: monday ? 5 : 0 }}
            className="text-[9px] text-slate-400 text-left overflow-visible whitespace-nowrap">
            {label}
          </div>
        )
      })}
    </div>
  )
}

function DiffDrawer({ account, contract, onClose }: { account: string; contract: string; onClose: () => void }) {
  const [res, setRes] = useState<DiffResult | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [days, setDays] = useState(14)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    setRes(null); setErr(null); setLoading(true)
    fetchDiff(account, contract, days).then(setRes).catch((e) => setErr(String(e))).finally(() => setLoading(false))
  }, [account, contract, days])
  return (
    <div className="fixed inset-0 bg-black/30 flex justify-end z-50" onClick={onClose}>
      <div className="w-[560px] h-full bg-white shadow-xl p-4 overflow-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-[15px] font-semibold">TT fills diff — {account} / {contract}</h2>
          <button className="text-slate-400 hover:text-slate-700" onClick={onClose}>✕</button>
        </div>
        <div className="flex items-center gap-2 mb-3 text-[12px] text-slate-500">
          <span>lookback</span>
          <select className="border border-slate-300 rounded px-1 py-0.5" value={days}
            onChange={(e) => setDays(Number(e.target.value))} disabled={loading}>
            {[7, 14, 30, 60, 90, 180].map((d) => <option key={d} value={d}>{d}d</option>)}
          </select>
          <span className="text-slate-400">— widen this to reach drops older than the chart window (slower on high-volume accounts)</span>
        </div>
        {loading && <div className="text-[13px] text-slate-500">Querying TT ledger (paginated)… this can take 30s+ on high-volume accounts.</div>}
        {err && <div className="text-[13px] text-red-600">{err}</div>}
        {res && res.error && <div className="text-[13px] text-red-600">{res.error}</div>}
        {res && !res.error && (
          <div className="text-[13px]">
            <div className="grid grid-cols-2 gap-2 mb-3">
              <Stat label="TT env" v={res.env ?? '—'} />
              <Stat label="window" v={`${res.days}d`} />
              <Stat label="our fills" v={res.our_fills} />
              <Stat label="TT fills" v={res.tt_fills} />
              <Stat label="missing (TT not in DB)" v={res.missing_count} highlight={res.missing_count > 0} />
              <Stat label="net of missing" v={fmtNet(res.net_missing)} highlight={Math.abs(res.net_missing) > 0} />
            </div>
            {res.missing_count === 0 ? (
              <div className="text-green-700">No missing fills — TT and DB agree over this window. The open position is likely a real hold or a pre-window / cash-settlement effect.</div>
            ) : (
              <table className="w-full text-[12px] border-collapse">
                <thead><tr className="text-left text-slate-500 border-b">
                  <th className="py-1">timestamp (UTC)</th><th>side</th><th>qty</th><th>price</th><th>uniqueExecId</th>
                </tr></thead>
                <tbody>
                  {res.missing.map((m, i) => (
                    <tr key={i} className="border-b border-slate-100">
                      <td className="py-1 tnum">{m.timestamp.replace('T', ' ').slice(0, 23)}</td>
                      <td>{m.side === 1 ? 'buy' : 'sell'}</td>
                      <td className="tnum">{m.qty}</td>
                      <td className="tnum">{m.price}</td>
                      <td className="tnum text-slate-400 truncate max-w-[120px]" title={m.uniqueExecId}>{m.uniqueExecId}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, v, highlight }: { label: string; v: any; highlight?: boolean }) {
  return (
    <div className="rounded border border-slate-200 px-2 py-1">
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className={'tnum text-[14px] font-semibold ' + (highlight ? 'text-red-600' : 'text-slate-800')}>{v}</div>
    </div>
  )
}

function Toggle({ on, set, children }: { on: boolean; set: (b: boolean) => void; children: any }) {
  return (
    <label className="inline-flex items-center gap-1 text-[12px] text-slate-600 cursor-pointer select-none">
      <input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} />
      {children}
    </label>
  )
}

export default function App() {
  const [data, setData] = useState<Overview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [windowDays, setWindowDays] = useState(30)
  const [withTT, setWithTT] = useState(true)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [hideSim, setHideSim] = useState(false)
  const [hideOptOut, setHideOptOut] = useState(false)
  const [diff, setDiff] = useState<{ account: string; contract: string } | null>(null)

  function load(refresh = false) {
    setLoading(true); setError(null)
    fetchOverview(windowDays, withTT, refresh)
      .then(setData).catch((e) => setError(String(e))).finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [windowDays, withTT])

  return (
    <div className="min-h-screen pb-20">
      <header className="sticky top-0 z-30 bg-white border-b border-slate-200 px-4 py-2.5 shadow-sm">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-baseline gap-3">
            <h1 className="text-[16px] font-bold text-slate-900">Skyll Trades Validator</h1>
            {data && <span className="text-[12px] text-slate-400">{data.window.start_date} → {data.window.end_date}</span>}
            {data && !data.tt_checked && (
              <span className="text-[12px] text-red-600" title={data.tt_error ?? ''}>TT check unavailable</span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <select className="text-[12px] border border-slate-300 rounded px-1.5 py-0.5"
              value={windowDays} onChange={(e) => setWindowDays(Number(e.target.value))}>
              {[14, 30, 60, 90].map((w) => <option key={w} value={w}>last {w}d</option>)}
            </select>
            <Toggle on={withTT} set={setWithTT}>TT check</Toggle>
            <Toggle on={onlyProblems} set={setOnlyProblems}>only problems</Toggle>
            <Toggle on={hideSim} set={setHideSim}>hide sim</Toggle>
            <Toggle on={hideOptOut} set={setHideOptOut}>hide opt-out</Toggle>
            <button className="text-[12px] rounded bg-slate-900 text-white px-2.5 py-1 hover:bg-slate-700 disabled:opacity-50"
              disabled={loading} onClick={() => load(true)}>{loading ? 'Computing…' : 'Refresh'}</button>
          </div>
        </div>
        {data && (
          <div className="mt-2 flex items-center justify-between flex-wrap gap-2">
            <SummaryChips summary={data.overall} />
            <span className="text-[11px] text-slate-400">cached {new Date(data.cached_at * 1000).toLocaleTimeString()}</span>
          </div>
        )}
      </header>

      <div className="px-4 pt-3">
        {loading && !data && (
          <div className="text-[13px] text-slate-500 py-10 text-center">
            Computing validation state against the read-only replica{withTT ? ' + TT positions' : ''}…
            <div className="text-[11px] text-slate-400 mt-1">first load is ~15–20s; cached for 5 min afterwards</div>
          </div>
        )}
        {error && <div className="text-[13px] text-red-600 py-4">{error}</div>}

        {data && (
          <>
            <div className="mb-2"><Legend /></div>
            <DayAxis days={data.window.days} />
            {data.groups.map((g) => (
              <GroupRow key={g.group_id} g={g} days={data.window.days}
                filters={{ hideSim, hideOptOut }} onlyProblems={onlyProblems}
                onDiff={(account, contract) => setDiff({ account, contract })} />
            ))}
          </>
        )}
      </div>

      {diff && <DiffDrawer account={diff.account} contract={diff.contract} onClose={() => setDiff(null)} />}
    </div>
  )
}
