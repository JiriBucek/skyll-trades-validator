import { useState } from 'react'
import type { Contract, DayCell, Group, Health, Summary, Trader, TTPosResult, TTPosRow } from './api'
import type { CellState } from './colors'
import { CELL_COLOR, LABEL, NUM_COLOR } from './colors'

// TT position-check index (built in App.tsx from /api/ttpos): `${account}|${contract}` -> row.
// null = the user hasn't run the TT check yet.
export type TTIndex = Record<string, TTPosRow & { status: string }> | null

export function fmtNet(n: number | undefined | null): string {
  if (n == null || Math.abs(n) < 1e-9) return '0'
  const r = Math.round(n * 100) / 100
  return (r > 0 ? '+' : '') + r
}

// --- product family helpers (mirror backend engine.symbol_of / contracts.expiry_month) ---
// product symbol = first token: "I Sep26" -> "I", "SO3 Dec26" -> "SO3".
export function symbolOf(contract: string): string {
  return contract.trim().split(' ')[0]
}
const MONTH_IDX: Record<string, number> = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
}
const MAT_RE = /(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-?\s?(\d{2})\b/i
// sortable maturity key (year*100 + month) so same-product months sit in chronological order:
// Jun26 < Sep26 < Dec26 < Mar27. Unparseable maturities sort to the end of their family.
export function maturityKey(contract: string): number {
  const m = MAT_RE.exec(contract)
  if (!m) return 9_999_99
  return (2000 + Number(m[2])) * 100 + MONTH_IDX[m[1].toLowerCase()]
}
// two faint alternating tints so adjacent product families (each a band of same-symbol rows) are
// visually separable even though the whole list is one alphabetical sort.
const FAMILY_BG = ['rgba(99,102,241,0.05)', 'rgba(14,165,233,0.06)']

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

// TT badge on a contract row — what the platform's own position book says about this line.
function TTBadge({ r }: { r: TTPosRow & { status: string } }) {
  const live = `TT live net ${fmtNet(r.tt_net)}${r.tt_sod != null ? ` · start-of-day ${fmtNet(r.tt_sod)}` : ''}${r.tt_pnl != null ? ` · open PnL ${r.tt_pnl}` : ''}`
  const lag = 'TT is live, our fills batch-ingest (~15 min lag) — a diff on a contract trading right now can be benign.'
  const S: Record<string, { cls: string; text: string; title: string }> = {
    match: {
      cls: 'text-emerald-700 border-emerald-300 bg-emerald-50',
      text: `TT ✓ ${fmtNet(r.tt_net)}`,
      title: `TT agrees this position is open.\n${live}`,
    },
    diff: {
      cls: 'text-rose-700 border-rose-300 bg-rose-50',
      text: `TT ${fmtNet(r.tt_net)} ≠ ${fmtNet(r.db_net)}`,
      title: `TT's book shows a DIFFERENT position than our fills.\n${live}\n${lag}\nIf it persists on a quiet contract: missing/extra fill on our side.`,
    },
    tt_flat: {
      cls: 'text-rose-700 border-rose-300 bg-rose-50',
      text: 'TT: flat',
      title: `TT has NO position row for this contract — the platform thinks it is FLAT while our fills say ${fmtNet(r.db_net)}.\nAbsence is a real signal (the endpoint lists idle opens). Phantom-open family: missed closing fill on our side, sim position reset, or double-booked TT ledger — investigate with make skyll-fills.`,
    },
    tt_only: {
      cls: 'text-rose-700 border-rose-300 bg-rose-50',
      text: `TT open ${fmtNet(r.tt_net)}`,
      title: `TT shows an OPEN position but this line is ${r.db_net == null ? 'not open in the window' : `${fmtNet(r.db_net)} in our DB`} — possible missing fill on OUR side.\n${live}\n${lag}`,
    },
    expired: {
      cls: 'text-slate-400 border-slate-200 bg-slate-50',
      text: 'TT n/a · expired',
      title: 'Contract already expired — TT drops delisted instruments from the position monitor, so "no row" is meaningless here. This open is the expiry-carry class (settlement close not modeled).',
    },
    no_api: {
      cls: 'text-slate-400 border-slate-200 bg-slate-50',
      text: 'no TT API',
      title: 'Stellar-platform account — there is no TT API to ask (FIX drop-copy is the only feed).',
    },
    error: {
      cls: 'text-amber-700 border-amber-300 bg-amber-50',
      text: 'TT ?',
      title: 'The TT snapshot for this environment failed (credentials / network) — see the TT panel.',
    },
  }
  const s = S[r.status]
  if (!s) return null
  return (
    <span className={'text-[11px] tnum font-semibold border rounded px-1.5 py-0.5 ' + s.cls} title={s.title}>
      {s.text}
    </span>
  )
}

export function ContractRow({
  c, days, familyBg, familyTop, familyBottom, tt,
}: {
  c: Contract; days: string[]
  familyBg?: string      // shared tint for a multi-contract product family (undefined = lone contract)
  familyTop?: boolean    // first row of the family — draw the top edge
  familyBottom?: boolean // last row of the family — draw the bottom edge
  tt?: TTIndex           // TT position-check results (null/undefined until the user runs it)
}) {
  const ttRow = tt?.[`${c.account}|${c.contract}`]
  const spread = c.is_spread
  const famStyle = familyBg
    ? {
        background: familyBg,
        boxShadow: 'inset 3px 0 0 rgba(99,102,241,0.35)',  // left rail marks "one product, many months"
        borderTop: familyTop ? '1px solid rgba(99,102,241,0.18)' : undefined,
        borderBottom: familyBottom ? '1px solid rgba(99,102,241,0.18)' : undefined,
      }
    : undefined
  return (
    <div className={'flex items-center gap-2 py-[2px] ' + (familyBg ? '' : 'hover:bg-slate-50')}
      style={famStyle} data-acct={c.account} data-contract={c.contract}>
      <div className="flex items-center gap-2" style={{ width: 360, paddingLeft: 36 }}>
        <span className="tnum text-[12px] text-slate-400 w-[88px] truncate" title={c.account}>{c.account}</span>
        <a className={'text-[12px] hover:underline w-[150px] truncate ' + (spread ? 'text-slate-500' : 'text-blue-700')}
          href={`#/fills?account=${encodeURIComponent(c.account)}&contract=${encodeURIComponent(c.contract)}`}
          onClick={() => sessionStorage.setItem('validator.scrollTo', JSON.stringify({ account: c.account, contract: c.contract }))}
          title={`fill history — ${c.contract}`}>{c.contract}</a>
        <span className="tnum text-[12px] font-semibold w-[44px] text-right"
          style={{ color: spread || Math.abs(c.current_net) < 1e-9 ? '#94a3b8' : '#0f172a' }}>
          {fmtNet(c.current_net)}
        </span>
      </div>
      {c.sustained_open
        ? <NumberStrip days={days} cells={numberCells(c)} faint={spread && !c.has_mismatch} />
        : <DayStrip days={days} cells={squareCells(c)} faint={spread} />}
      <div className="flex items-center gap-2 ml-2">
        {ttRow && <TTBadge r={ttRow} />}
        {spread && (
          <span className="text-[10px] uppercase tracking-wide font-semibold text-indigo-600 border border-indigo-300 bg-indigo-50 rounded px-1.5 py-0.5"
            title="Known spread / curve book — per-leg net is expected by design, not a problem. Excluded from the counts.">spread</span>
        )}
        {(c.problem || c.has_mismatch) && (
          <span className={'text-[11px] tnum font-semibold ' + (c.has_mismatch ? 'text-rose-700' : c.unverifiable ? 'text-slate-400' : 'text-amber-700')}
            title={c.has_mismatch
              ? 'at least one completed day where our fills volume ≠ the FIX feed — a fill is probably missing'
              : c.unverifiable
                ? `open ${openAge(c)}, but the FIX feed has no rows for this contract (option / give-up / clearing-alias account) — can’t verify`
                : `open ${openAge(c)}${c.opened_before_window ? ' (carried in from before the window — not counted in the aggregated timeline)' : ''}; the FIX feed agrees on every completed day’s volume`}>
            {c.has_mismatch ? 'feed mismatch' : `open ${openAge(c)}${c.unverifiable ? ' · no FIX' : ''}`}
          </span>
        )}
        {c.skipped_count > 0 && (
          <span className={'text-[11px] tnum font-semibold border rounded px-1.5 py-0.5 ' + (c.closes_to_zero
            ? 'text-emerald-700 border-emerald-300 bg-emerald-50'
            : 'text-purple-700 border-purple-200 bg-purple-50')}
            title={`${c.skipped_count} fill(s) across this contract's whole history are in the ledger but were never aggregated into a trade — the trades are off by ${fmtNet(c.skipped_lots)} lots. ` + (c.closes_to_zero
              ? `Counting ALL fills (including these), the contract nets ${fmtNet(c.current_net)} ≈ 0 — so re-aggregating (recalc_trader) re-walks the skips into trades and it lands flat. The recalc-able batch.`
              : `Counting ALL fills (including these), the contract still nets ${fmtNet(c.current_net)} — a genuine open, so recalc can't flatten it (preflight aborts on net≠0).`) + ` Purple cells mark the skipped days inside the window.`}>
            {c.skipped_count} skipped · {fmtNet(c.skipped_lots)}{c.closes_to_zero ? ' → closes to 0' : ''}
          </span>
        )}
        {(c.problem || c.skipped_count > 0 || c.has_mismatch) && (
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

export function TraderRow({
  t, days, filters, onlyProblems, tt,
}: {
  t: Trader; days: string[]
  filters: { hideSim: boolean; hideOptOut: boolean; onlyClosesToZero: boolean }
  onlyProblems: boolean
  tt?: TTIndex
}) {
  const [open, setOpen] = useState(t.summary.mismatch > 0)
  // force-expand so the filtered contracts show (only-closes-to-zero, and only-problems)
  const isOpen = filters.onlyClosesToZero || onlyProblems || open

  const accounts = t.accounts.filter(
    (a) => !(filters.hideSim && a.is_sim) && !(filters.hideOptOut && a.opt_out),
  )
  let contracts: Contract[] = []
  for (const a of accounts) contracts.push(...a.contracts)
  // "only problems" hides spread books (we don't support them — not problems) and fine contracts,
  // leaving just the actionable rows. A feed mismatch (likely dropped fill) stays even on a spread
  // leg — it's the one spread state that's still actionable.
  if (onlyProblems) contracts = contracts.filter((c) => c.has_mismatch || (!c.is_spread && (c.problem || c.skipped_count > 0)))
  // the contracts are already window-gated by the engine; "only closes to zero" just filters THIS
  // windowed set down to the recalc-able ones — it never pulls in dormant/old contracts.
  if (filters.onlyClosesToZero) contracts = contracts.filter((c) => c.closes_to_zero)
  // sort PURELY by product family then maturity — every month of a product sits together, in
  // chronological order. (We no longer float problems to the top; the day strips + tags flag them.)
  contracts.sort((x, y) =>
    symbolOf(x.contract).localeCompare(symbolOf(y.contract)) ||
    maturityKey(x.contract) - maturityKey(y.contract) ||
    x.contract.localeCompare(y.contract) || x.account.localeCompare(y.account))

  // a product symbol with 2+ rows in this (filtered) list is a "family" — give each family a faint
  // alternating band + left rail so you can see at a glance it's one product across several months.
  const symCount = new Map<string, number>()
  for (const c of contracts) symCount.set(symbolOf(c.contract), (symCount.get(symbolOf(c.contract)) ?? 0) + 1)
  const famBgOf = new Map<string, string>()
  let famIdx = 0
  for (const sym of symCount.keys()) {
    if ((symCount.get(sym) ?? 0) >= 2) famBgOf.set(sym, FAMILY_BG[famIdx++ % FAMILY_BG.length])
  }

  const sim = t.accounts.some((a) => a.is_sim)
  const nSpread = t.summary.spread || 0
  return (
    <div className="border-t border-slate-100">
      <div className="flex items-center gap-2 py-1 cursor-pointer hover:bg-slate-50" onClick={() => setOpen(!open)}>
        <div className="flex items-center gap-1.5" style={{ width: 360, paddingLeft: 18 }}>
          <span className="text-slate-400 text-[11px] w-3">{isOpen ? '▾' : '▸'}</span>
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
      {isOpen && (
        <div className="pb-1">
          {contracts.map((c, i) => {
            const sym = symbolOf(c.contract)
            const familyBg = famBgOf.get(sym)
            return (
              <ContractRow key={c.account + c.contract} c={c} days={days} familyBg={familyBg}
                familyTop={!!familyBg && symbolOf(contracts[i - 1]?.contract ?? '') !== sym}
                familyBottom={!!familyBg && symbolOf(contracts[i + 1]?.contract ?? '') !== sym}
                tt={tt} />
            )
          })}
          {contracts.length === 0 && (
            <div className="text-[12px] text-slate-400 py-1" style={{ paddingLeft: 36 }}>
              {onlyProblems ? 'no problem contracts'
                : filters.onlyClosesToZero ? 'no “closes to zero” contracts' : 'no contracts in window'}
            </div>
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
  g, days, filters, onlyProblems, tt,
}: {
  g: Group; days: string[]
  filters: { hideSim: boolean; hideOptOut: boolean; onlyClosesToZero: boolean }
  onlyProblems: boolean
  tt?: TTIndex
}) {
  const [open, setOpen] = useState((g.summary.mismatch + g.summary.open) > 0)
  // force-expand so the filtered traders show (only-closes-to-zero, and only-problems)
  const isOpen = filters.onlyClosesToZero || onlyProblems || open

  let traders = [...g.traders].sort(
    (a, b) => traderScore(b) - traderScore(a) || a.trader_name.localeCompare(b.trader_name),
  )
  if (onlyProblems) traders = traders.filter((t) => traderProblems(t) > 0)
  if (filters.onlyClosesToZero) traders = traders.filter((t) => t.summary.closes_to_zero > 0)

  return (
    <div className="mb-2 rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center gap-2 px-2 py-1.5 cursor-pointer" onClick={() => setOpen(!open)}>
        <div className="flex items-center gap-1.5" style={{ width: 360 }}>
          <span className="text-slate-400 text-[11px] w-3">{isOpen ? '▾' : '▸'}</span>
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
      {isOpen && (
        <div className="px-2 pb-2">
          {traders.map((t) => <TraderRow key={t.trader_id} t={t} days={days} filters={filters} onlyProblems={onlyProblems} tt={tt} />)}
          {traders.length === 0 && <div className="text-[12px] text-slate-400 py-2 pl-5">no traders match the filter</div>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// header bits
// ---------------------------------------------------------------------------

// summary panel for the TT position check — the verdict counts + the reverse detector
// (TT-open positions with no open validator line = possible drops on OUR side).
export function TTPanel({ tt }: { tt: TTPosResult }) {
  const [showOnly, setShowOnly] = useState(false)
  const c = tt.counts
  const chips = [
    { color: '#059669', n: c.match ?? 0, label: 'TT agrees' },
    { color: '#dc2626', n: c.diff ?? 0, label: 'TT differs' },
    { color: '#dc2626', n: c.tt_flat ?? 0, label: 'TT flat (phantom open)' },
    { color: '#94a3b8', n: c.expired ?? 0, label: 'expired (n/a)' },
    { color: '#94a3b8', n: c.no_api ?? 0, label: 'Stellar (no API)' },
    { color: '#d97706', n: c.error ?? 0, label: 'errors' },
  ].filter((x) => x.n > 0)
  const envs = Object.entries(tt.envs)
    .map(([e, s]) => `${e.replace('ext_prod_', '')}: ${s.rows_nonzero} open rows`).join(' · ')
  return (
    <div className="rounded-lg px-3 py-2 mb-2 border border-sky-200 bg-sky-50">
      <div className="flex items-center flex-wrap gap-2">
        <span className="text-[12px] font-bold text-sky-800">TT position check</span>
        <span className="text-[11px] text-slate-500" title={tt.note}>
          {new Date(tt.fetched_at).toLocaleTimeString()} · {envs} · live vs ~15 min-lagged fills
        </span>
        {chips.map((x) => (
          <span key={x.label} className="inline-flex items-center gap-1 text-[11px] rounded-full px-2 py-0.5"
            style={{ background: x.color + '22', color: x.color }}>
            <span className="font-semibold tnum">{x.n}</span> {x.label}
          </span>
        ))}
        {tt.tt_only.length > 0 && (
          <button className="text-[11px] text-rose-700 font-semibold underline decoration-dotted"
            title="TT shows these positions OPEN, but the validator has no open line for them (flat in our DB, or no fills in the window) — each one is a possible missing fill on OUR side."
            onClick={() => setShowOnly(!showOnly)}>
            {tt.tt_only.length} TT-only open{tt.tt_only.length > 1 ? 's' : ''} {showOnly ? '▾' : '▸'}
          </button>
        )}
        {Object.entries(tt.errors).map(([env, err]) => (
          <span key={env} className="text-[11px] text-amber-700" title={err}>⚠ {env} failed</span>
        ))}
      </div>
      {showOnly && (
        <div className="mt-1.5 grid gap-x-6 gap-y-0.5" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
          {tt.tt_only.map((r) => (
            <div key={`${r.account}|${r.contract}`} className="text-[11px] tnum text-slate-700">
              <a className="text-blue-700 hover:underline"
                href={`#/fills?account=${encodeURIComponent(r.account)}&contract=${encodeURIComponent(r.contract)}`}>
                {r.account} · {r.contract}
              </a>
              {'  '}TT <span className="font-semibold text-rose-700">{fmtNet(r.tt_net)}</span>
              {' vs DB '}{r.db_net == null
                ? <span className="text-slate-400" title="no fills in the display window — dormant contract; check its full history">not in window</span>
                : <span className="font-semibold">{fmtNet(r.db_net)}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function SummaryChips({ summary }: { summary: Summary }) {
  const chips = [
    { color: '#dc2626', n: summary.mismatch, label: 'feed mismatch' },
    { color: '#9333ea', n: summary.skipped_fills, label: `skipped fills (${summary.skipped_contracts})` },
    { color: '#059669', n: summary.closes_to_zero, label: 'close to zero (recalc-able)' },
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
