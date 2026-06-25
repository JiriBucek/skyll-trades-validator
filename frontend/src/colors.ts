import type { State } from './api'

// cell background, badge background, short label, long label, severity
export const STATE: Record<State, { cell: string; badge: string; label: string; long: string; sev: number }> = {
  flat:             { cell: '#4ade80', badge: '#16a34a', label: 'Flat',        long: 'Flat — net 0, all fills aggregated', sev: 0 },
  settled_residual: { cell: '#9ca3af', badge: '#6b7280', label: 'Settled',     long: 'Settled / expired residual (cash settlement)', sev: 1 },
  open_confirmed:   { cell: '#3b82f6', badge: '#2563eb', label: 'Open ✓',      long: 'Open — confirmed by TT', sev: 2 },
  open_pending_tt:  { cell: '#cbd5e1', badge: '#94a3b8', label: 'Open …',      long: 'Open — TT check pending', sev: 3 },
  open_unverifiable:{ cell: '#f59e0b', badge: '#d97706', label: 'Open ?',      long: 'Open — unverifiable (Stellar / not in TT / recent)', sev: 4 },
  orphan:           { cell: '#fb923c', badge: '#ea580c', label: 'Orphan',      long: 'Orphan fills — unassigned to trades on a completed day', sev: 5 },
  suspected_drop:   { cell: '#ef4444', badge: '#dc2626', label: 'Dropped!',    long: 'Suspected dropped fill — open here, flat on TT', sev: 6 },
}

export const ORDER: State[] = [
  'suspected_drop', 'orphan', 'open_unverifiable', 'open_pending_tt',
  'open_confirmed', 'settled_residual', 'flat',
]

export function worstOf(states: State[]): State {
  let w: State = 'flat'
  for (const s of states) if (STATE[s].sev > STATE[w].sev) w = s
  return w
}
