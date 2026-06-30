export type State =
  | 'flat' | 'settled_residual' | 'partial_carry' | 'confirmed_open' | 'pending_fix'
  | 'unverifiable' | 'orphan' | 'unreconciled' | 'extra_misattr' | 'stranded' | 'drop'

export interface DayCell {
  date: string
  eod_net: number
  flat: boolean
  n_fills: number
  n_orphan: number
  n_stranded: number
  state?: State
}

export interface FixFill {
  timestamp: string | null
  day: string | null
  side: number
  qty: number
  price: number | null
  uniqueExecId?: string
}

export interface FixInfo {
  feed: string
  raw_net?: number
  raw_n?: number
  our_net?: number
  pre_retention?: number
  gap?: number
  reason?: string
  method?: string
  missing_count?: number
  recoverable_net?: number
  missing?: FixFill[]
  extra_count?: number
  extra_net?: number
  extra?: FixFill[]
  miss_net?: number
}

export interface StrandedInfo { n: number; net: number; last: string | null }

export interface Contract {
  account: string
  contract: string
  platform_id: number
  current_net: number
  first_fill: string | null
  last_fill: string | null
  expired: boolean | null
  category: 'active' | 'stale_residual'
  days: DayCell[]
  switch_on: string | null
  has_orphans: boolean
  has_stranded: boolean
  verdict: State
  fix: FixInfo | null
  stranded_info: StrandedInfo | null
  is_spread?: boolean
}

export interface Account {
  account: string
  platform_id: number
  platform_name: string
  is_sim: boolean
  opt_out: boolean
  active: Contract[]
  residual: Contract[]
}

export type Summary = Record<string, number>

export interface Trader {
  trader_id: number
  trader_name: string
  accounts: Account[]
  day_status: Record<string, State>
  worst: State
  open_since: string | null
  recon_flags: number
  summary: Summary
}

export interface Group {
  group_id: number
  group_name: string
  traders: Trader[]
  day_status: Record<string, State>
  worst: State
  summary: Summary
}

export interface DropDay { day: string; fills: number; net: number }

export interface Health {
  drop_contracts: number
  drop_windows: number
  drop_fills: number
  extra_misattr: number
  stranded: number
  unreconciled: number
  unverifiable: number
  confirmed_open: number
  partial_carry: number
  orphan: number
  flat: number
  spread: number
  actionable: number
  healthy: boolean
  headline: string
}

export interface Overview {
  window: { start_date: string; end_date: string; days: string[] }
  generated_at: string
  groups: Group[]
  overall: Summary
  drop_rollup: DropDay[]
  health: Health
  fix_checked: boolean
  fix_error: string | null
  cached_at: number
}

// --- on-demand FIX-feed diff (the authoritative drop detector) ---
export interface RawDiffResult {
  account: string
  contract: string
  feed?: string
  canonical_account?: string
  our_net_retention?: number
  fix_net?: number
  pre_retention_carry?: number
  our_fills?: number
  raw_fills?: number
  missing_from_us?: FixFill[]
  missing_net?: number
  extra_in_us?: FixFill[]
  extra_net?: number
  method?: string
  verdict?: string
  note?: string
  error?: string
}

export async function fetchOverview(window: number, fix: boolean, refresh = false): Promise<Overview> {
  const r = await fetch(`/api/overview?window=${window}&fix=${fix ? 1 : 0}&refresh=${refresh ? 1 : 0}`)
  if (!r.ok) throw new Error(`overview ${r.status}: ${await r.text()}`)
  return r.json()
}

export async function fetchRawDiff(account: string, contract: string): Promise<RawDiffResult> {
  const r = await fetch(`/api/raw-diff?account=${encodeURIComponent(account)}&contract=${encodeURIComponent(contract)}`)
  if (!r.ok) throw new Error(`raw-diff ${r.status}: ${await r.text()}`)
  return r.json()
}

// --- fill history (the "what did he do" page) ---
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
