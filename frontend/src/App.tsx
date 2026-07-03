import { useEffect, useState } from 'react'
import { fetchOverview, fetchTTPos, type Overview, type TTPosResult, type TTPosRow } from './api'
import { CELL_W, GAP, GroupRow, HealthHeader, Legend, MON_GAP, SummaryChips, TTPanel } from './components'
import FillsPage from './FillsPage'

// index the TT check by row key so ContractRow can pick up its badge in O(1). tt_only entries
// (TT open, no open validator line) are included with a synthetic status so a visible-but-flat
// row can still show "TT says open" in red.
export type TTIndex = Record<string, TTPosRow & { status: string }>
function buildTTIndex(tt: TTPosResult): TTIndex {
  const idx: TTIndex = {}
  for (const r of tt.rows) idx[`${r.account}|${r.contract}`] = r as TTPosRow & { status: string }
  for (const r of tt.tt_only) idx[`${r.account}|${r.contract}`] = { ...r, status: 'tt_only' }
  return idx
}

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
  const [onlyClosesToZero, setOnlyClosesToZero] = useState(false)
  const [hideSim, setHideSim] = useState(false)
  const [hideOptOut, setHideOptOut] = useState(false)
  const [route, setRoute] = useState<Route>(parseRoute)
  const [tt, setTT] = useState<TTPosResult | null>(null)
  const [ttIndex, setTTIndex] = useState<TTIndex | null>(null)
  const [ttLoading, setTTLoading] = useState(false)
  const [ttError, setTTError] = useState<string | null>(null)

  function loadTT(refresh = false) {
    setTTLoading(true); setTTError(null)
    fetchTTPos(windowDays, refresh)
      .then((r) => { setTT(r); setTTIndex(buildTTIndex(r)) })
      .catch((e) => setTTError(String(e)))
      .finally(() => setTTLoading(false))
  }
  // a TT snapshot annotates the CURRENT window's open rows — invalidate it when the window changes
  useEffect(() => { setTT(null); setTTIndex(null); setTTError(null) }, [windowDays])

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
  useEffect(() => { load() }, [windowDays])

  // Open a fill detail at the top; on return, bring the contract the user opened back to the top so
  // they don't lose their place in a long list. The overview stays mounted (hidden) behind the detail,
  // so its expanded groups + rows survive — we just scroll to the row. Target stored on click below.
  useEffect(() => {
    if (route.page === 'fills') { window.scrollTo({ top: 0 }); return }
    const raw = sessionStorage.getItem('validator.scrollTo')
    if (!raw) return
    sessionStorage.removeItem('validator.scrollTo')
    let t: { account: string; contract: string }
    try { t = JSON.parse(raw) } catch { return }
    requestAnimationFrame(() => {
      const el = document.querySelector<HTMLElement>(
        `[data-acct="${CSS.escape(t.account)}"][data-contract="${CSS.escape(t.contract)}"]`)
      if (!el) return
      const header = document.querySelector('header')
      const offset = (header?.getBoundingClientRect().height ?? 72) + 8   // sit just below the sticky header
      window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - offset })
    })
  }, [route.page])

  return (
    <>
      {route.page === 'fills' && <FillsPage account={route.account} contract={route.contract} />}
      <div className="min-h-screen pb-20" style={route.page === 'fills' ? { display: 'none' } : undefined}>
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
            <Toggle on={onlyClosesToZero} set={setOnlyClosesToZero}>only closes to zero</Toggle>
            <Toggle on={hideSim} set={setHideSim}>hide sim</Toggle>
            <Toggle on={hideOptOut} set={setHideOptOut}>hide opt-out</Toggle>
            <button className="text-[12px] rounded border border-sky-600 text-sky-700 px-2.5 py-1 hover:bg-sky-50 disabled:opacity-50"
              disabled={ttLoading || loading || !data}
              title="Ask the TT API what IT thinks the open positions are and annotate every open line (one bulk position pull per env — cheap). First ever run warms an id→name cache and can take ~1 min."
              onClick={() => loadTT(!!tt)}>{ttLoading ? 'Asking TT…' : tt ? 'TT ⟳' : 'TT check'}</button>
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
            {ttError && <div className="text-[12px] text-red-600 mb-2">TT check failed: {ttError}</div>}
            {tt && <TTPanel tt={tt} />}
            <div className="mb-2"><Legend /></div>
            <DayAxis days={data.window.days} />
            {data.groups.map((g) => (
              <GroupRow key={g.group_id} g={g} days={data.window.days}
                filters={{ hideSim, hideOptOut, onlyClosesToZero }} onlyProblems={onlyProblems}
                tt={ttIndex} />
            ))}
          </>
        )}
      </div>
      </div>
    </>
  )
}
