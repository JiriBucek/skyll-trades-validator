import { useEffect, useState } from 'react'
import { fetchOverview, type Overview } from './api'
import { CELL_W, GAP, GroupRow, HealthHeader, Legend, MON_GAP, SummaryChips } from './components'
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
          <div key={d} style={{ width: CELL_W, marginRight: GAP, marginLeft: monday ? MON_GAP : 0 }}
            className="text-[9px] text-slate-400 text-center overflow-visible whitespace-nowrap">
            {label}
          </div>
        )
      })}
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
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [hideSim, setHideSim] = useState(false)
  const [hideOptOut, setHideOptOut] = useState(false)
  const [route, setRoute] = useState<Route>(parseRoute)

  useEffect(() => {
    const on = () => setRoute(parseRoute())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  function load(refresh = false) {
    setLoading(true); setError(null)
    fetchOverview(windowDays, true, refresh)
      .then(setData).catch((e) => setError(String(e))).finally(() => setLoading(false))
  }
  useEffect(() => { if (route.page === 'overview') load() }, [windowDays, route.page])

  if (route.page === 'fills') return <FillsPage account={route.account} contract={route.contract} />

  return (
    <div className="min-h-screen pb-20">
      <header className="sticky top-0 z-30 bg-white border-b border-slate-200 px-4 py-2.5 shadow-sm">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-baseline gap-3">
            <h1 className="text-[16px] font-bold text-slate-900">Skyll Trades Validator</h1>
            {data && <span className="text-[12px] text-slate-400">{data.window.start_date} → {data.window.end_date}</span>}
            {data && !data.fix_checked && (
              <span className="text-[12px] text-red-600" title={data.fix_error ?? ''}>FIX cross-check unavailable</span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <select className="text-[12px] border border-slate-300 rounded px-1.5 py-0.5"
              value={windowDays} onChange={(e) => setWindowDays(Number(e.target.value))}>
              {[14, 30, 60, 90].map((w) => <option key={w} value={w}>last {w}d</option>)}
            </select>
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
            Computing the day-by-day state against the read-only replica + the FIX feed…
            <div className="text-[11px] text-slate-400 mt-1">first load is ~20–30s; cached for 5 min afterwards</div>
          </div>
        )}
        {error && <div className="text-[13px] text-red-600 py-4">{error}</div>}

        {data && (
          <>
            {data.health && <HealthHeader health={data.health} />}
            <div className="mb-2"><Legend /></div>
            <DayAxis days={data.window.days} />
            {data.groups.map((g) => (
              <GroupRow key={g.group_id} g={g} days={data.window.days}
                filters={{ hideSim, hideOptOut }} onlyProblems={onlyProblems} />
            ))}
          </>
        )}
      </div>
    </div>
  )
}
