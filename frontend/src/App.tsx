import { useEffect, useState } from 'react'
import { fetchOverview, fetchRawDiff, type FixFill, type Overview, type RawDiffResult } from './api'
import { DropRollup, GroupRow, HealthHeader, Legend, SummaryChips, fmtNet } from './components'
import FillsPage from './FillsPage'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

type Route = { page: 'overview' } | { page: 'fills'; account: string; contract: string }
function parseRoute(): Route {
  const h = window.location.hash
  if (h.startsWith('#/fills')) {
    const qs = new URLSearchParams(h.slice(h.indexOf('?') + 1))
    return { page: 'fills', account: qs.get('account') || '', contract: qs.get('contract') || '' }
  }
  return { page: 'overview' }
}

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

function FillTable({ rows, kind }: { rows: FixFill[]; kind: 'missing' | 'extra' }) {
  return (
    <table className="w-full text-[12px] border-collapse mb-3">
      <thead><tr className="text-left text-slate-500 border-b">
        <th className="py-1">timestamp (UTC)</th><th>side</th><th>qty</th><th>price</th>
        <th>{kind === 'missing' ? 'uniqueExecId (reingest)' : ''}</th>
      </tr></thead>
      <tbody>
        {rows.map((m, i) => (
          <tr key={i} className="border-b border-slate-100">
            <td className="py-1 tnum">{m.timestamp ? m.timestamp.replace('T', ' ').slice(0, 23) : '—'}</td>
            <td>{m.side === 1 ? 'buy' : 'sell'}</td>
            <td className="tnum">{m.qty}</td>
            <td className="tnum">{m.price}</td>
            <td className="tnum text-slate-400 truncate max-w-[150px]" title={m.uniqueExecId}>{m.uniqueExecId ?? ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function DiffDrawer({ account, contract, onClose }: { account: string; contract: string; onClose: () => void }) {
  const [res, setRes] = useState<RawDiffResult | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    setRes(null); setErr(null); setLoading(true)
    fetchRawDiff(account, contract).then(setRes).catch((e) => setErr(String(e))).finally(() => setLoading(false))
  }, [account, contract])
  return (
    <div className="fixed inset-0 bg-black/30 flex justify-end z-50" onClick={onClose}>
      <div className="w-[620px] h-full bg-white shadow-xl p-4 overflow-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-[15px] font-semibold">FIX-feed diff — {account} / {contract}</h2>
          <button className="text-slate-400 hover:text-slate-700" onClick={onClose}>✕</button>
        </div>
        <div className="text-[11px] text-slate-400 mb-3">
          compares our <code>fills</code> against <code>raw_fills_fix</code> (the authoritative per-account FIX copy),
          as of now. Missing-from-us = dropped fills (recover); extra-in-us = duplicate / mis-attributed (orphan off).
        </div>
        {loading && <div className="text-[13px] text-slate-500">Querying the FIX feed…</div>}
        {err && <div className="text-[13px] text-red-600">{err}</div>}
        {res && res.error && <div className="text-[13px] text-red-600">{res.error}</div>}
        {res && res.note && !res.error && <div className="text-[13px] text-amber-700">{res.note}</div>}
        {res && !res.error && res.fix_net !== undefined && (
          <div className="text-[13px]">
            <div className="grid grid-cols-3 gap-2 mb-3">
              <Stat label="feed" v={res.feed ?? '—'} />
              <Stat label="our net (retention)" v={fmtNet(res.our_net_retention)} />
              <Stat label="FIX net" v={fmtNet(res.fix_net)} highlight={res.our_net_retention !== res.fix_net} />
              <Stat label="pre-retention carry" v={fmtNet(res.pre_retention_carry)} />
              <Stat label="our fills" v={res.our_fills} />
              <Stat label="FIX fills" v={res.raw_fills} />
            </div>
            {(res.missing_from_us?.length ?? 0) > 0 && (
              <>
                <div className="text-[12px] font-semibold text-rose-700 mb-1">
                  {res.missing_from_us!.length} missing from us — DROPPED (net {fmtNet(res.missing_net)}); recover these
                </div>
                <FillTable rows={res.missing_from_us!} kind="missing" />
              </>
            )}
            {(res.extra_in_us?.length ?? 0) > 0 && (
              <>
                <div className="text-[12px] font-semibold text-rose-700 mb-1">
                  {res.extra_in_us!.length} extra in us — DUPLICATE / MIS-ATTRIBUTED (net {fmtNet(res.extra_net)}); orphan off the book
                </div>
                <FillTable rows={res.extra_in_us!} kind="extra" />
              </>
            )}
            {(res.missing_from_us?.length ?? 0) === 0 && (res.extra_in_us?.length ?? 0) === 0 && (
              <div className="text-green-700">Our fills match the FIX feed over the retention window — a genuine open or a pre-retention carry, not a dropped fill.</div>
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
      <div className={'tnum text-[14px] font-semibold ' + (highlight ? 'text-rose-600' : 'text-slate-800')}>{v}</div>
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
  const [withFix, setWithFix] = useState(true)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [hideSim, setHideSim] = useState(false)
  const [hideOptOut, setHideOptOut] = useState(false)
  const [diff, setDiff] = useState<{ account: string; contract: string } | null>(null)
  const [route, setRoute] = useState<Route>(parseRoute)

  useEffect(() => {
    const on = () => setRoute(parseRoute())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  function load(refresh = false) {
    setLoading(true); setError(null)
    fetchOverview(windowDays, withFix, refresh)
      .then(setData).catch((e) => setError(String(e))).finally(() => setLoading(false))
  }
  useEffect(() => { if (route.page === 'overview') load() }, [windowDays, withFix, route.page])

  if (route.page === 'fills') return <FillsPage account={route.account} contract={route.contract} />

  return (
    <div className="min-h-screen pb-20">
      <header className="sticky top-0 z-30 bg-white border-b border-slate-200 px-4 py-2.5 shadow-sm">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-baseline gap-3">
            <h1 className="text-[16px] font-bold text-slate-900">Skyll Trades Validator</h1>
            {data && <span className="text-[12px] text-slate-400">{data.window.start_date} → {data.window.end_date}</span>}
            {data && !data.fix_checked && (
              <span className="text-[12px] text-red-600" title={data.fix_error ?? ''}>FIX check unavailable</span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <select className="text-[12px] border border-slate-300 rounded px-1.5 py-0.5"
              value={windowDays} onChange={(e) => setWindowDays(Number(e.target.value))}>
              {[14, 30, 60, 90].map((w) => <option key={w} value={w}>last {w}d</option>)}
            </select>
            <Toggle on={withFix} set={setWithFix}>FIX check</Toggle>
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
            Computing validation state against the read-only replica{withFix ? ' + the FIX feed' : ''}…
            <div className="text-[11px] text-slate-400 mt-1">first load is ~20–30s; cached for 5 min afterwards</div>
          </div>
        )}
        {error && <div className="text-[13px] text-red-600 py-4">{error}</div>}

        {data && (
          <>
            {data.health && <HealthHeader health={data.health} />}
            <DropRollup rollup={data.drop_rollup} />
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
