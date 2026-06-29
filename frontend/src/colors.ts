import type { State } from './api'

// cell background, badge background, short label, long label, severity (matches engine.SEVERITY)
export const STATE: Record<State, { cell: string; badge: string; label: string; long: string; sev: number }> = {
  flat:             { cell: '#4ade80', badge: '#16a34a', label: 'Flat',       long: 'Flat — net 0, all fills aggregated', sev: 0 },
  settled_residual: { cell: '#9ca3af', badge: '#6b7280', label: 'Residual',   long: 'Old non-flat residual (display triage; not chased)', sev: 1 },
  partial_carry:    { cell: '#a3b8cc', badge: '#64748b', label: 'Carry',      long: 'Pre-retention carry — opened before the FIX wall; in-window reconciles', sev: 2 },
  confirmed_open:   { cell: '#3b82f6', badge: '#2563eb', label: 'Open ✓',     long: 'Open — our net == the FIX feed. A genuine hold', sev: 3 },
  pending_fix:      { cell: '#cbd5e1', badge: '#94a3b8', label: 'Open …',     long: 'Open — FIX cross-check pending', sev: 4 },
  unverifiable:     { cell: '#f59e0b', badge: '#d97706', label: 'Open ?',     long: 'Open — no FIX cross-check (pre-retention / option / give-up account)', sev: 4 },
  orphan:           { cell: '#fb923c', badge: '#ea580c', label: 'Orphan',     long: 'Orphan fills — unassigned to trades on a completed day', sev: 5 },
  unreconciled:     { cell: '#a855f7', badge: '#9333ea', label: 'Unrecon',    long: 'Diverges from FIX but cannot be pinned (block-vs-leg / synthetic) — investigate', sev: 6 },
  extra_misattr:    { cell: '#f43f5e', badge: '#e11d48', label: 'Extra!',     long: 'EXTRA / MIS-ATTRIBUTED — we hold fills the FIX feed lacks (duplicate / alias-defaulted)', sev: 7 },
  stranded:         { cell: '#b91c1c', badge: '#991b1b', label: 'Stranded!',  long: 'STRANDED — futures fills under trader 0/349 never linked (recalc, no backfill)', sev: 8 },
  drop:             { cell: '#ef4444', badge: '#dc2626', label: 'Dropped!',   long: 'DROPPED FILL — the FIX feed has fills we lack (recoverable)', sev: 9 },
}

export const ORDER: State[] = [
  'drop', 'stranded', 'extra_misattr', 'unreconciled', 'orphan', 'unverifiable',
  'pending_fix', 'confirmed_open', 'partial_carry', 'settled_residual', 'flat',
]

// the 🔴 actionable verdicts (a fill is genuinely wrong and you can act on it)
export const ACTIONABLE: State[] = ['drop', 'stranded', 'extra_misattr']

export function worstOf(states: State[]): State {
  let w: State = 'flat'
  for (const s of states) if (STATE[s].sev > STATE[w].sev) w = s
  return w
}
