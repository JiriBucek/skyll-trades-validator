import type { CellState } from './colors'

export interface DayCell {
  date: string
  eod_net: number     // signed end-of-day net position (lots)
  flat: boolean       // |eod_net| ~ 0
  open: boolean       // non-flat at EOD
  gross: number       // our fills gross traded volume that day (Σ qty)
  raw_gross?: number  // problem rows only: raw_fills_fix gross that completed day (FIX feed)
  n_fills: number
  mismatch: boolean   // problem rows only: completed day where fills gross ≠ raw_fills_fix gross
  skipped: number     // fills that day with empty trade_ids that were skipped (a later fill is assigned)
  skipped_lots: number // signed lots skipped that day (buy +, sell −)
  state?: CellState   // resolved server-side: 'flat' | 'open' | 'skipped' | 'mismatch'
}

export interface Contract {
  account: string
  contract: string
  platform_id: number
  current_net: number
  total_buys: number      // whole-history gross buy lots
  total_sells: number     // whole-history gross sell lots
  first_fill: string | null
  last_fill: string | null
  expired: boolean | null
  is_spread: boolean
  days: DayCell[]
  trailing_open: number   // consecutive open EOD days at the end of the window (capped at window len)
  open_days: number       // TRUE age of the current open run (looks back beyond the window)
  open_capped: boolean    // the run is older than the look-back, so open_days is a floor ("N+ d")
  opened_before_window: boolean  // carried in from before day 1 — excluded from the aggregate timeline
  sustained_open: boolean // trailing_open >= PROBLEM_OPEN_DAYS — drives the number line (incl spreads)
  problem: boolean        // sustained_open AND not a spread — the subset that counts toward health
  has_mismatch: boolean   // any completed day flagged mismatch (likely a dropped fill)
  unverifiable: boolean   // problem row the FIX feed can't confirm (option / give-up / alias acct)
  skipped_count: number   // whole-history count of skipped fills (in ledger, never aggregated)
  skipped_lots: number    // whole-history signed lots skipped (buy +, sell −) — how much trades are off
  net_ex_skips: number    // current_net − skipped_lots (assigned-fills net); ~0 + open ⇒ genuine open
  closes_to_zero: boolean // has skips AND all-fills net ~0 ⇒ recalc re-walks the skips → lands flat
}

export interface Account {
  account: string
  platform_id: number
  platform_name: string
  is_sim: boolean
  opt_out: boolean
  contracts: Contract[]
}

export interface Summary {
  contracts: number
  ok: number            // fine (no sustained open)
  open: number          // sustained open, FIX feed confirms (feeds agree)
  unverifiable: number  // sustained open the FIX feed can't confirm (option / give-up / alias)
  mismatch: number      // sustained open with a feed mismatch (likely dropped fill)
  spread: number        // curated spread/curve legs (excluded)
  skipped_contracts: number // contracts with >=1 skipped fill (orthogonal to the buckets above)
  skipped_fills: number     // total skipped fills across those contracts
  closes_to_zero: number    // of those, how many net ~flat with all fills counted (recalc-able)
}

export interface Trader {
  trader_id: number
  trader_name: string
  accounts: Account[]
  day_status: Record<string, CellState>
  summary: Summary
}

export interface Group {
  group_id: number
  group_name: string
  traders: Trader[]
  day_status: Record<string, CellState>
  summary: Summary
}

export interface Health {
  mismatch: number
  open: number
  unverifiable: number
  spread: number
  skipped_contracts: number
  skipped_fills: number
  closes_to_zero: number
  actionable: number
  healthy: boolean
  headline: string
}

export interface Overview {
  window: { start_date: string; end_date: string; days: string[] }
  generated_at: string
  groups: Group[]
  overall: Summary
  health: Health
  fix_checked: boolean
  fix_error: string | null
  cached_at: number
}

export async function fetchOverview(window: number, fix: boolean, refresh = false): Promise<Overview> {
  const r = await fetch(`/api/overview?window=${window}&fix=${fix ? 1 : 0}&refresh=${refresh ? 1 : 0}`)
  if (!r.ok) throw new Error(`overview ${r.status}: ${await r.text()}`)
  return r.json()
}

// --- fill history (the click-through "what did he do" detail) ---
export interface FillRow {
  timestamp: string
  side: number
  qty: number
  price: number | null
  delta: number
  running_position: number
  trader_id: number
  fill_type: string
  linked: boolean
}

export interface FillsHistory {
  account: string
  contract: string
  total_fills: number
  returned: number
  truncated: boolean
  current_net: number
  first_fill: string | null
  last_fill: string | null
  fills: FillRow[]
}

export async function fetchFills(account: string, contract: string): Promise<FillsHistory> {
  const r = await fetch(`/api/fills?account=${encodeURIComponent(account)}&contract=${encodeURIComponent(contract)}`)
  if (!r.ok) throw new Error(`fills ${r.status}: ${await r.text()}`)
  return r.json()
}
