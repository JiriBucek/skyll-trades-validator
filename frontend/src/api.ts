export type State =
  | 'flat' | 'open_confirmed' | 'open_unverifiable' | 'open_pending_tt'
  | 'orphan' | 'suspected_drop' | 'settled_residual'

export interface DayCell {
  date: string
  eod_net: number
  flat: boolean
  n_fills: number
  n_orphan: number
  state?: State
}

export interface TTInfo {
  checked: boolean
  in_tt?: boolean
  tt_net?: number
  our_net?: number
  recent?: boolean
  mismatch?: boolean
  discrepancy?: number
  error?: string
}

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
  verdict: State
  tt: TTInfo | null
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

export interface Overview {
  window: { start_date: string; end_date: string; days: string[] }
  generated_at: string
  groups: Group[]
  overall: Summary
  tt_checked: boolean
  tt_error: string | null
  cached_at: number
}

export interface DiffResult {
  account: string
  contract: string
  env: string | null
  days: number
  our_fills: number
  tt_fills: number
  missing_count: number
  net_missing: number
  missing: Array<{
    timestamp: string; side: number; qty: number; price: number
    execId: string; uniqueExecId: string
  }>
  error?: string
}

export async function fetchOverview(window: number, tt: boolean, refresh = false): Promise<Overview> {
  const r = await fetch(`/api/overview?window=${window}&tt=${tt ? 1 : 0}&refresh=${refresh ? 1 : 0}`)
  if (!r.ok) throw new Error(`overview ${r.status}: ${await r.text()}`)
  return r.json()
}

export async function fetchDiff(account: string, contract: string, days: number): Promise<DiffResult> {
  const r = await fetch(
    `/api/tt-diff?account=${encodeURIComponent(account)}&contract=${encodeURIComponent(contract)}&days=${days}`,
  )
  if (!r.ok) throw new Error(`tt-diff ${r.status}: ${await r.text()}`)
  return r.json()
}
