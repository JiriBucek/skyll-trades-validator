import { useState } from 'react'
import type { Contract, DayCell, Group, Health, Summary, Trader } from './api'
import type { CellState } from './colors'
import { CELL_COLOR, LABEL, NUM_COLOR } from './colors'

export function fmtNet(n: number | undefined | null): string {
  if (n == null || Math.abs(n) < 1e-9) return '0'
  const r = Math.round(n * 100) / 100
  return (r > 0 ? '+' : '') + r
}

// true age of the current open run — "12d", or "365+d" when older than the look-back
function openAge(c: Contract): string {
  return `${c.open_days}${c.open_capped ? '+' : ''}d`
}

// gross volume (unsigned), rounded — for the buys/sells breakdown bubble
function fmtVol(n: number): string {
  return String(Math.round(n * 100) / 100)
}

// strip geometry — exported so the DayAxis in App.tsx stays pixel-aligned with the cells.
// columns are wide enough that a 4-digit signed net (e.g. -227) never overlaps its neighbour.
export const CELL_W = 22   // column width (fits a 4-digit signed net like -227 without overlap)
export const CELL_H = 13   // column height
export const GAP = 1       // gap between columns
export const MON_GAP = 6   // extra left margin before each Monday (week separator)
const ROW_H = CELL_H + 5

function weekdayUTC(d: string): number {
  return new Date(d + 'T00:00:00Z').getUTCDay()
}

function Pill({ color, text, title }: { color: string; text: string; title?: string }) {
  return (
    <span className="text-[11px] tnum px-1 rounded text-white" style={{ background: color }} title={title}>
      {text}
    </span>
  )
}

// ---------------------------------------------------------------------------
// the two strip renderers
// ---------------------------------------------------------------------------

// square strip — green / yellow (/ red on roll-ups). Used for "fine" contract rows + roll-ups.
export function DayStrip({
  days, cells, faint, onCell,
}: {
  days: string[]
  cells: Record<string, { state: CellState; title: string; show?: boolean }>
  faint?: boolean
  onCell?: (date: string) => void
}) {
  return (
    <div className="flex items-center" style={{ height: ROW_H }}>
      {days.map((d) => {
        const c = cells[d]
        const wd = weekdayUTC(d)
        const monday = wd === 1
        const weekend = wd === 0 || wd === 6
        const blank = !c || c.show === false   // no fills that day → empty (activity-only strip)
        const bg = blank ? 'transparent' : CELL_COLOR[c.state]
        const dim = weekend && (!c || c.state === 'flat') ? 0.45 : 1
        return (
          <div key={d}
            onClick={onCell && !blank ? () => onCell(d) : undefined}
            title={blank ? `${d}: no fills` : c.title}
            style={{
              width: CELL_W, height: CELL_H, marginRight: GAP, marginLeft: monday ? MON_GAP : 0,
              background: bg, opacity: blank ? 1 : (faint ? dim * 0.3 : dim),
              borderRadius: 2, cursor: onCell && !blank ? 'pointer' : 'default',
            }} />
        )
      })}
    </div>
  )
}

// number strip — a line of EOD-net lots. Used for PROBLEM contract rows (sustained opens).
// flat day -> muted 0 · open day -> amber number (feeds agree) · mismatch -> red number.
export function NumberStrip({
  days, cells, faint,
}: {
  days: string[]
  cells: Record<string, { net: number; state: CellState; title: string; show?: boolean }>
  faint?: boolean   // spread legs: same yellow/grey numbers, just muted so they don't alarm
}) {
  return (
    <div className="flex items-center" style={{ height: ROW_H, opacity: faint ? 0.5 : 1 }}>
      {days.map((d) => {
        const c = cells[d]
        const monday = weekdayUTC(d) === 1
        const blank = !c || c.show === false   // no fills that day (and not a drop) → empty
        const flat = !c || c.state === 'flat'
        const color = c ? NUM_COLOR[c.state] : NUM_COLOR.flat
        return (
          <div key={d}
            title={blank ? `${d}: no fills` : c.title}
            style={{
              width: CELL_W, height: CELL_H, marginRight: GAP, marginLeft: monday ? MON_GAP : 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 8.5, lineHeight: 1, color, fontWeight: flat ? 400 : 700,
              fontVariantNumeric: 'tabular-nums', overflow: 'visible', whiteSpace: 'nowrap',
            }}>
            {blank ? '' : flat ? '0' : fmtNet(c.net)}
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// cell builders
// ---------------------------------------------------------------------------

function stateOf(d: DayCell): CellState {
  return (d.state as CellState) ?? (d.mismatch ? 'mismatch' : d.skipped > 0 ? 'skipped' : d.open ? 'open' : 'flat')
}

function dayTitle(d: DayCell, st: CellState): string {
  const lines = [
    d.date,
    `EOD net: ${fmtNet(d.eod_net)}`,
    `fills today: ${d.n_fills}  (gross ${d.gross})`,
  ]
  if (d.skipped > 0) lines.push(`skipped: ${d.skipped} fill(s), ${fmtNet(d.skipped_lots)} lots — in the ledger, never aggregated into a trade`)
  if (d.raw_gross != null) {
    lines.push(
      st === 'mismatch'
        ? `FIX gross: ${d.raw_gross}  ✗ differs by ${fmtNet(d.gross - d.raw_gross)}`
        : `FIX gross: ${d.raw_gross}  ✓ match`,
    )
  }
  lines.push(LABEL[st])
  return lines.join('\n')
}

// only render a day that had real activity — fills on the day, a drop (FIX has fills we lack, so our
// n_fills is 0 but it's a mismatch), or a skipped fill. Carried-forward held days with no activity
// go blank.
function isActiveDay(d: DayCell): boolean {
  return d.n_fills > 0 || d.mismatch || d.skipped > 0
}

function squareCells(c: Contract): Record<string, { state: CellState; title: string; show: boolean }> {
  const m: Record<string, { state: CellState; title: string; show: boolean }> = {}
  for (const d of c.days) {
    const st = stateOf(d)
    m[d.date] = { state: st, title: dayTitle(d, st), show: isActiveDay(d) }
  }
  return m
}

function numberCells(c: Contract): Record<string, { net: number; state: CellState; title: string; show: boolean }> {
  const m: Record<string, { net: number; state: CellState; title: string; show: boolean }> = {}
  for (const d of c.days) {
    const st = stateOf(d)
    m[d.date] = { net: d.eod_net, state: st, title: dayTitle(d, st), show: isActiveDay(d) }
  }
  return m
}

function rollupCells(dayStatus: Record<string, CellState>): Record<string, { state: CellState; title: string }> {
  const m: Record<string, { state: CellState; title: string }> = {}
  for (const [d, st] of Object.entries(dayStatus)) m[d] = { state: st, title: `${d}: ${LABEL[st]}` }
  return m
}

// ---------------------------------------------------------------------------
// rows
// ---------------------------------------------------------------------------

export function ContractRow({ c, days }: { c: Contract; days: string[] }) {
  const spread = c.is_spread
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
      {c.sustained_open
        ? <NumberStrip days={days} cells={numberCells(c)} faint={spread} />
        : <DayStrip days={days} cells={squareCells(c)} faint={spread} />}
      <div className="flex items-center gap-2 ml-2">
        {spread && (
          <span className="text-[10px] uppercase tracking-wide font-semibold text-indigo-600 border border-indigo-300 bg-indigo-50 rounded px-1.5 py-0.5"
            title="Known spread / curve book — per-leg net is expected by design, not a problem. Excluded from the counts.">spread</span>
        )}
        {c.problem && (
          <span className={'text-[11px] tnum font-semibold ' + (c.has_mismatch ? 'text-rose-700' : c.unverifiable ? 'text-slate-400' : 'text-amber-700')}
            title={c.has_mismatch
              ? 'at least one completed day where our fills volume ≠ the FIX feed — a fill is probably missing'
              : c.unverifiable
                ? `open ${openAge(c)}, but the FIX feed has no rows for this contract (option / give-up / clearing-alias account) — can’t verify`
                : `open ${openAge(c)}${c.opened_before_window ? ' (carried in from before the window — not counted in the aggregated timeline)' : ''}; the FIX feed agrees on every completed day’s volume`}>
            {c.has_mismatch ? 'feed mismatch' : `open ${openAge(c)}${c.unverifiable ? ' · no FIX' : ''}`}
          </span>
        )}
        {c.skipped_count > 0 && (() => {
          const closes = Math.abs(c.current_net) > 1e-9 && Math.abs(c.net_ex_skips) < 0.5
          return (
            <span className="text-[11px] tnum font-semibold text-purple-700 border border-purple-200 bg-purple-50 rounded px-1.5 py-0.5"
              title={`${c.skipped_count} fill(s) across this contract's whole history are in the ledger but were never aggregated into a trade — the trades are off by ${fmtNet(c.skipped_lots)} lots. Without the skips the position nets ${fmtNet(c.net_ex_skips)}${closes ? ' — i.e. it would close to zero (the whole open is unaggregated skipped fills)' : ''}. Purple cells mark the skipped days inside the window.`}>
              {c.skipped_count} skipped · {fmtNet(c.skipped_lots)}{closes ? ' → closes to 0' : ''}
            </span>
          )
        })()}
        {(c.problem || c.skipped_count > 0) && (
          <span className="text-[11px] tnum border border-slate-200 rounded px-1.5 py-0.5"
            title="total volume over the whole history — buy lots (green) − sell lots (red) = net position">
            <span className="text-green-700 font-semibold">{fmtVol(c.total_buys)}</span>
            <span className="text-slate-400"> − </span>
            <span className="text-rose-700 font-semibold">{fmtVol(c.total_sells)}</span>
            <span className="text-slate-400"> = </span>
            <span className="font-semibold text-slate-800">{fmtNet(c.total_buys - c.total_sells)}</span>
          </span>
        )}
      </div>
    </div>
  )
}

function contractScore(c: Contract): number {
  if (c.is_spread) return 0
  if (!c.problem) return 1
  if (c.has_mismatch) return 4
  if (c.unverifiable) return 2
  return 3 // sustained open, feeds agree
}

export function TraderRow({
  t, days, filters,
}: {
  t: Trader; days: string[]; filters: { hideSim: boolean; hideOptOut: boolean }
}) {
  const [open, setOpen] = useState(t.summary.mismatch > 0)

  const accounts = t.accounts.filter(
    (a) => !(filters.hideSim && a.is_sim) && !(filters.hideOptOut && a.opt_out),
  )
  const contracts: Contract[] = []
  for (const a of accounts) contracts.push(...a.contracts)
  contracts.sort((x, y) =>
    contractScore(y) - contractScore(x) ||
    x.account.localeCompare(y.account) || x.contract.localeCompare(y.contract))

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
            title={`${nSpread} spread/curve leg(s) — excluded from the counts`}>spread {nSpread}</span>}
        </div>
        <DayStrip days={days} cells={rollupCells(t.day_status)} />
        <div className="flex items-center gap-2 ml-2">
          {t.summary.mismatch > 0 && <Pill color="#dc2626" text={`${t.summary.mismatch} mismatch`} title="contracts with a feed mismatch (likely dropped fills)" />}
          {t.summary.skipped_fills > 0 && <Pill color="#9333ea" text={`${t.summary.skipped_fills} skipped`} title="fills in the ledger that were never aggregated into a trade (whole history)" />}
          {t.summary.open > 0 && <Pill color="#d97706" text={`${t.summary.open} open`} title="sustained opens — FIX feed agrees" />}
          {t.summary.unverifiable > 0 && <Pill color="#94a3b8" text={`${t.summary.unverifiable}`} title="sustained opens the FIX feed can't confirm (option / give-up / alias account)" />}
        </div>
      </div>
      {open && (
        <div className="pb-1">
          {contracts.map((c) => <ContractRow key={c.account + c.contract} c={c} days={days} />)}
          {contracts.length === 0 && (
            <div className="text-[12px] text-slate-400 py-1" style={{ paddingLeft: 36 }}>no contracts in window</div>
          )}
        </div>
      )}
    </div>
  )
}

function traderProblems(t: Trader): number {
  return t.summary.mismatch + t.summary.open + t.summary.unverifiable
}
function traderScore(t: Trader): number {
  return t.summary.mismatch * 1_000_000 + t.summary.open * 1000 + t.summary.unverifiable
}

export function GroupRow({
  g, days, filters, onlyProblems,
}: {
  g: Group; days: string[]
  filters: { hideSim: boolean; hideOptOut: boolean }
  onlyProblems: boolean
}) {
  const [open, setOpen] = useState((g.summary.mismatch + g.summary.open) > 0)

  let traders = [...g.traders].sort(
    (a, b) => traderScore(b) - traderScore(a) || a.trader_name.localeCompare(b.trader_name),
  )
  if (onlyProblems) traders = traders.filter((t) => traderProblems(t) > 0)

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
          {g.summary.mismatch > 0 && <Pill color="#dc2626" text={String(g.summary.mismatch)} title="feed mismatch (likely dropped fills)" />}
          {g.summary.skipped_fills > 0 && <Pill color="#9333ea" text={`${g.summary.skipped_fills} skipped`} title="fills never aggregated into a trade (whole history)" />}
          {g.summary.open > 0 && <Pill color="#d97706" text={String(g.summary.open)} title="sustained opens (FIX feed agrees)" />}
          {g.summary.unverifiable > 0 && <Pill color="#94a3b8" text={String(g.summary.unverifiable)} title="unverifiable opens (no FIX rows)" />}
          {g.summary.spread > 0 && <Pill color="#6366f1" text={String(g.summary.spread)} title="spread/curve legs (excluded)" />}
        </div>
      </div>
      {open && (
        <div className="px-2 pb-2">
          {traders.map((t) => <TraderRow key={t.trader_id} t={t} days={days} filters={filters} />)}
          {traders.length === 0 && <div className="text-[12px] text-slate-400 py-2 pl-5">no traders match the filter</div>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// header bits
// ---------------------------------------------------------------------------

export function SummaryChips({ summary }: { summary: Summary }) {
  const chips = [
    { color: '#dc2626', n: summary.mismatch, label: 'feed mismatch' },
    { color: '#9333ea', n: summary.skipped_fills, label: `skipped fills (${summary.skipped_contracts})` },
    { color: '#d97706', n: summary.open, label: 'sustained open' },
    { color: '#94a3b8', n: summary.unverifiable, label: 'unverifiable' },
    { color: '#6366f1', n: summary.spread, label: 'spread legs' },
    { color: '#16a34a', n: summary.ok, label: 'fine' },
  ]
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {chips.map((c) => (
        <span key={c.label} className="inline-flex items-center gap-1 text-[12px] rounded-full px-2 py-0.5"
          style={{ background: c.color + '22', color: c.color }}>
          <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: c.color }} />
          <span className="font-semibold tnum">{c.n}</span> {c.label}
        </span>
      ))}
    </div>
  )
}

export function Legend() {
  const items = [
    { color: CELL_COLOR.flat, text: 'flat — closed to ~0 at EOD' },
    { color: CELL_COLOR.open, text: 'open at EOD' },
    { color: CELL_COLOR.skipped, text: 'skipped fill (never aggregated)' },
    { color: CELL_COLOR.mismatch, text: 'feed mismatch' },
  ]
  return (
    <div className="flex flex-wrap items-center gap-3">
      {items.map((i) => (
        <span key={i.text} className="inline-flex items-center gap-1 text-[11px] text-slate-500">
          <span className="inline-block w-3 h-3 rounded-sm" style={{ background: i.color }} />{i.text}
        </span>
      ))}
      <span className="text-[11px] text-slate-400">
        · open ≥3 trailing days → a line of EOD-net lots (numbers); red = that day’s fills volume ≠ the FIX feed (likely a dropped fill)
      </span>
    </div>
  )
}

export function HealthHeader({ health }: { health: Health }) {
  if (!health) return null
  const ok = health.healthy
  return (
    <div className={'rounded-lg px-3 py-2 mb-2 border ' + (ok ? 'border-green-300 bg-green-50' : 'border-rose-300 bg-rose-50')}>
      <div className="flex items-baseline gap-2">
        <span className={'text-[13px] font-bold ' + (ok ? 'text-green-700' : 'text-rose-700')}>
          {ok ? '✅ clean' : `🔴 ${health.actionable} actionable`}
        </span>
        <span className="text-[12px] text-slate-600">{health.headline}</span>
      </div>
    </div>
  )
}
