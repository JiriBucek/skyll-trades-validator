import { useState } from 'react'
import type { Contract, DayCell, DropDay, Group, State, Summary, Trader } from './api'
import { ACTIONABLE, ORDER, STATE, worstOf } from './colors'

export function fmtNet(n: number | undefined | null): string {
  if (n == null || Math.abs(n) < 1e-9) return '0'
  const r = Math.round(n * 100) / 100
  return (r > 0 ? '+' : '') + r
}

const CELL = 15 // px including 1px gap
const isActionable = (s: State) => (ACTIONABLE as string[]).includes(s)

export function Badge({ state, text }: { state: State; text?: string }) {
  const s = STATE[state]
  return (
    <span
      className="inline-block rounded px-1.5 py-0.5 text-[11px] font-semibold text-white"
      style={{ background: s.badge }}
      title={s.long}
    >
      {text ?? s.label}
    </span>
  )
}

export function DayStrip({
  days, cells, onCell, faint,
}: {
  days: string[]
  cells: Record<string, { state: State; title: string; net?: number; orphan?: number; stranded?: number }>
  onCell?: (date: string) => void
  faint?: boolean   // spread legs: keep the colours but render them muted so they don't alarm
}) {
  return (
    <div className="flex items-center" style={{ height: CELL + 3 }}>
      {days.map((d) => {
        const c = cells[d]
        const wd = new Date(d + 'T00:00:00Z').getUTCDay()
        const monday = wd === 1
        const weekend = wd === 0 || wd === 6
        const bg = c ? STATE[c.state].cell : '#e5e7eb'
        const flagged = c && (c.orphan || c.stranded)
        const base = weekend && (!c || c.state === 'flat') ? 0.45 : 1
        return (
          <div
            key={d}
            onClick={onCell ? () => onCell(d) : undefined}
            title={c ? c.title : `${d}: no data`}
            style={{
              width: CELL - 2, height: CELL - 2, marginRight: 1,
              marginLeft: monday ? 5 : 0,
              background: bg,
              opacity: faint ? base * 0.3 : base,
              borderRadius: 2, cursor: onCell ? 'pointer' : 'default',
              outline: flagged && !faint ? '1.5px solid #7c2d12' : 'none',
            }}
          />
        )
      })}
    </div>
  )
}

function contractCells(c: Contract): Record<string, { state: State; title: string; net?: number; orphan?: number; stranded?: number }> {
  const m: Record<string, any> = {}
  for (const d of c.days as DayCell[]) {
    const st = (d.state as State) ?? (d.flat ? 'flat' : 'unverifiable')
    m[d.date] = {
      state: st,
      net: d.eod_net,
      orphan: d.n_orphan,
      stranded: d.n_stranded,
      title:
        `${d.date}\nEOD net: ${fmtNet(d.eod_net)}` +
        `\nfills: ${d.n_fills}` +
        (d.n_orphan ? `\norphans: ${d.n_orphan}` : '') +
        (d.n_stranded ? `\nstranded: ${d.n_stranded}` : '') +
        `\n${STATE[st].long}`,
    }
  }
  return m
}

function rollupCells(dayStatus: Record<string, State>): Record<string, { state: State; title: string }> {
  const m: Record<string, any> = {}
  for (const [d, st] of Object.entries(dayStatus)) {
    m[d] = { state: st, title: `${d}: ${STATE[st].label}` }
  }
  return m
}

// short detail string from the FIX verdict / stranded info
function fixText(c: Contract): string {
  if (c.verdict === 'stranded' && c.stranded_info) {
    const si = c.stranded_info
    return `${si.n} fills under trader 0/349 (net ${fmtNet(si.net)})`
  }
  const f = c.fix
  if (!f) return ''
  if (f.raw_net == null) return f.reason ?? ''
  let s = `FIX ${fmtNet(f.raw_net)} vs ours ${fmtNet(f.our_net)}`
  if (c.verdict === 'drop' && f.missing_count) s += ` · ${f.missing_count} missing`
  if (c.verdict === 'extra_misattr' && f.extra_count) s += ` · ${f.extra_count} extra`
  if (f.pre_retention) s += ` · carry ${fmtNet(f.pre_retention)}`
  return s
}

export function ContractRow({
  c, days, onDiff,
}: {
  c: Contract; days: string[]; onDiff: (account: string, contract: string) => void
}) {
  // a drill-down helps for any FIX-checkable verdict (drop / extra / unreconciled / open)
  const canDiff = c.verdict !== 'stranded' && c.verdict !== 'settled_residual'
  const spread = !!c.is_spread
  const actionable = isActionable(c.verdict) && !spread
  return (
    <div className="flex items-center gap-2 py-[2px] hover:bg-slate-50">
      <div className="flex items-center gap-2" style={{ width: 360, paddingLeft: 36 }}>
        <span className="tnum text-[12px] text-slate-400 w-[88px] truncate" title={c.account}>{c.account}</span>
        <a className={'text-[12px] hover:underline w-[150px] truncate ' + (spread ? 'text-slate-500' : 'text-blue-700')}
          href={`#/fills?account=${encodeURIComponent(c.account)}&contract=${encodeURIComponent(c.contract)}`}
          title={`fill history — ${c.contract}`}>{c.contract}</a>
        <span className="tnum text-[12px] font-semibold w-[44px] text-right"
          style={{ color: spread || Math.abs(c.current_net) < 1e-9 ? '#94a3b8' : '#0f172a' }}>
          {fmtNet(c.current_net)}
        </span>
      </div>
      <DayStrip days={days} cells={contractCells(c)} faint={spread} />
      <div className="flex items-center gap-2 ml-2">
        {spread && (
          <span className="text-[10px] uppercase tracking-wide font-semibold text-indigo-600 border border-indigo-300 bg-indigo-50 rounded px-1.5 py-0.5"
            title="Known spread / curve book — per-leg net is expected, not a problem. Excluded from the health counts.">spread</span>
        )}
        <span className={spread ? 'flex items-center gap-2 opacity-40' : 'flex items-center gap-2'}>
          <Badge state={c.verdict} />
          <span className={'text-[11px] tnum ' + (actionable ? 'text-rose-700' : 'text-slate-500')}>{fixText(c)}</span>
        </span>
        {canDiff && (
          <button
            className="text-[11px] rounded border border-slate-300 px-1.5 py-0.5 text-slate-600 hover:bg-slate-100"
            onClick={() => onDiff(c.account, c.contract)}
          >FIX diff</button>
        )}
      </div>
    </div>
  )
}

export function TraderRow({
  t, days, filters, onDiff,
}: {
  t: Trader; days: string[]
  filters: { hideSim: boolean; hideOptOut: boolean }
  onDiff: (a: string, c: string) => void
}) {
  const autoOpen = isActionable(t.worst)
  const [open, setOpen] = useState(autoOpen)

  const accounts = t.accounts.filter(
    (a) => !(filters.hideSim && a.is_sim) && !(filters.hideOptOut && a.opt_out),
  )
  const active: Contract[] = []
  const residual: Contract[] = []
  for (const a of accounts) { active.push(...a.active); residual.push(...a.residual) }
  // spread/curve legs sort to the bottom (calm), real findings stay on top
  active.sort((x, y) =>
    Number(!!x.is_spread) - Number(!!y.is_spread) ||
    STATE[y.verdict].sev - STATE[x.verdict].sev || x.contract.localeCompare(y.contract))

  const sim = t.accounts.some((a) => a.is_sim)
  const nSpread = t.summary.spread || 0
  return (
    <div className="border-t border-slate-100">
      <div className="flex items-center gap-2 py-1 cursor-pointer hover:bg-slate-50" onClick={() => setOpen(!open)}>
        <div className="flex items-center gap-1.5" style={{ width: 360, paddingLeft: 18 }}>
          <span className="text-slate-400 text-[11px] w-3">{open ? '▾' : '▸'}</span>
          <span className="text-[13px] font-medium text-slate-800 truncate" title={t.trader_name}>{t.trader_name}</span>
          {sim && <span className="text-[9px] uppercase tracking-wide text-slate-400 border border-slate-300 rounded px-1">sim</span>}
          {nSpread > 0 && <span className="text-[9px] uppercase tracking-wide text-indigo-500 border border-indigo-200 bg-indigo-50 rounded px-1"
            title={`${nSpread} spread/curve leg(s) — excluded from the health counts`}>spread {nSpread}</span>}
        </div>
        <DayStrip days={days} cells={rollupCells(t.day_status)} />
        <div className="flex items-center gap-2 ml-2">
          {t.worst !== 'flat' && <Badge state={t.worst} />}
          {t.open_since && (
            <span className="text-[11px] text-slate-500">open since {t.open_since === 'before_window' ? '‹window' : t.open_since}</span>
          )}
          {t.recon_flags > 0 && (
            <span className="text-[11px] text-amber-700" title="days where daily candle close diverges from realized P&L (not cross-day explained)">
              ⚑ {t.recon_flags} candle
            </span>
          )}
        </div>
      </div>
      {open && (
        <div className="pb-1">
          {active.map((c) => (
            <ContractRow key={c.account + c.contract} c={c} days={days} onDiff={onDiff} />
          ))}
          {active.length === 0 && (
            <div className="text-[12px] text-slate-400 py-1" style={{ paddingLeft: 36 }}>no active contracts in window</div>
          )}
          {residual.length > 0 && <ResidualBlock residual={residual} />}
        </div>
      )}
    </div>
  )
}

function ResidualBlock({ residual }: { residual: Contract[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ paddingLeft: 36 }} className="mt-0.5">
      <button className="text-[11px] text-slate-400 hover:text-slate-600" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} {residual.length} old / un-chased residual{residual.length > 1 ? 's' : ''}
      </button>
      {open && (
        <div className="mt-0.5">
          {residual.map((c) => (
            <div key={c.account + c.contract} className="flex items-center gap-2 text-[12px] text-slate-500 py-[1px]">
              <span className="tnum w-[88px] truncate">{c.account}</span>
              <span className="w-[150px] truncate">{c.contract}</span>
              <span className="tnum w-[44px] text-right">{fmtNet(c.current_net)}</span>
              <span className="text-slate-400">last fill {c.last_fill?.slice(0, 10)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function GroupRow({
  g, days, filters, onlyProblems, onDiff,
}: {
  g: Group; days: string[]
  filters: { hideSim: boolean; hideOptOut: boolean }
  onlyProblems: boolean
  onDiff: (a: string, c: string) => void
}) {
  const hasAlert = ACTIONABLE.reduce((acc, s) => acc + (g.summary[s] || 0), 0) > 0
  const [open, setOpen] = useState(hasAlert)

  let traders = [...g.traders].sort(
    (a, b) => STATE[b.worst].sev - STATE[a.worst].sev || a.trader_name.localeCompare(b.trader_name),
  )
  if (onlyProblems) traders = traders.filter((t) => STATE[t.worst].sev >= STATE['orphan'].sev)

  return (
    <div className="mb-2 rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center gap-2 px-2 py-1.5 cursor-pointer" onClick={() => setOpen(!open)}>
        <div className="flex items-center gap-1.5" style={{ width: 360 }}>
          <span className="text-slate-400 text-[11px] w-3">{open ? '▾' : '▸'}</span>
          <span className="text-[14px] font-semibold text-slate-900">{g.group_name}</span>
          <span className="text-[11px] text-slate-400">{g.traders.length} traders</span>
        </div>
        <DayStrip days={days} cells={rollupCells(g.day_status)} />
        <div className="flex items-center gap-1.5 ml-2">
          {ORDER.filter((s) => s !== 'flat' && (g.summary[s] || 0) > 0).map((s) => (
            <span key={s} className="text-[11px] tnum px-1 rounded text-white" style={{ background: STATE[s].badge }} title={STATE[s].long}>
              {g.summary[s]}
            </span>
          ))}
        </div>
      </div>
      {open && (
        <div className="px-2 pb-2">
          {traders.map((t) => (
            <TraderRow key={t.trader_id} t={t} days={days} filters={filters} onDiff={onDiff} />
          ))}
          {traders.length === 0 && <div className="text-[12px] text-slate-400 py-2 pl-5">no traders match the filter</div>}
        </div>
      )}
    </div>
  )
}

export function SummaryChips({ summary }: { summary: Summary }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {ORDER.map((s) => {
        const n = summary[s] || 0
        if (n === 0 && s !== 'drop' && s !== 'flat') return null
        return (
          <span key={s} className="inline-flex items-center gap-1 text-[12px] rounded-full px-2 py-0.5"
            style={{ background: STATE[s].cell + '33', color: STATE[s].badge }}>
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: STATE[s].cell }} />
            <span className="font-semibold tnum">{n}</span> {STATE[s].label}
          </span>
        )
      })}
    </div>
  )
}

export function Legend() {
  return (
    <div className="flex flex-wrap gap-3">
      {ORDER.slice().reverse().map((s) => (
        <span key={s} className="inline-flex items-center gap-1 text-[11px] text-slate-500">
          <span className="inline-block w-3 h-3 rounded-sm" style={{ background: STATE[s].cell }} />
          {STATE[s].long}
        </span>
      ))}
    </div>
  )
}

// --- the drop-by-ingestion-day rollup strip (1b) ---
export function DropRollup({ rollup }: { rollup: DropDay[] }) {
  const [open, setOpen] = useState(true)
  if (!rollup || rollup.length === 0) return null
  const totalFills = rollup.reduce((a, d) => a + d.fills, 0)
  return (
    <div className="mb-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2">
      <button className="text-[12px] font-semibold text-rose-800 flex items-center gap-1.5" onClick={() => setOpen(!open)}>
        <span>{open ? '▾' : '▸'}</span>
        Drops by ingestion day — {rollup.length} window{rollup.length > 1 ? 's' : ''}, {totalFills} fills
        <span className="font-normal text-rose-600">(a systemic gap is one row, not fifty)</span>
      </button>
      {open && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {rollup.map((d) => (
            <span key={d.day} className="inline-flex items-center gap-1.5 text-[12px] rounded border border-rose-300 bg-white px-2 py-0.5"
              title={`${d.fills} missing fills, net ${fmtNet(d.net)}`}>
              <span className="tnum font-semibold text-rose-700">{d.day}</span>
              <span className="tnum text-slate-500">{d.fills} fills</span>
              <span className="tnum text-slate-400">net {fmtNet(d.net)}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// --- top-line health header (1c) ---
export function HealthHeader({ health }: { health: import('./api').Health }) {
  if (!health) return null
  const ok = health.healthy
  return (
    <div className={'rounded-lg px-3 py-2 mb-2 border ' + (ok ? 'border-green-300 bg-green-50' : 'border-rose-300 bg-rose-50')}>
      <div className="flex items-baseline gap-2">
        <span className={'text-[13px] font-bold ' + (ok ? 'text-green-700' : 'text-rose-700')}>
          {ok ? '✅ HEALTHY' : `🔴 ${health.actionable} actionable`}
        </span>
        <span className="text-[12px] text-slate-600">{health.headline}</span>
      </div>
    </div>
  )
}
