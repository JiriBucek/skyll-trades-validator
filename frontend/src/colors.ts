// Day states: green flat, yellow open, purple skipped fill, red feed-mismatch.
export type CellState = 'flat' | 'open' | 'skipped' | 'mismatch'

// day-strip square backgrounds (contract rows when "fine", and the trader/group roll-up strips)
export const CELL_COLOR: Record<CellState, string> = {
  flat: '#4ade80',     // green  — closed to ~zero at EOD
  open: '#facc15',     // yellow — open position at EOD
  skipped: '#a855f7',  // purple — fills on this day were never aggregated into a trade
  mismatch: '#ef4444', // red    — a completed day where fills gross ≠ raw_fills_fix gross
}

// number colours for PROBLEM rows (the line of EOD-net lots). Yellow/purple need a darker tone to
// stay legible on white than the square does.
export const NUM_COLOR: Record<CellState, string> = {
  flat: '#cbd5e1',     // muted — flat day, shows a faint 0
  open: '#d97706',     // amber-600 — open, the two feeds agree on the day's volume
  skipped: '#9333ea',  // purple-600 — skipped fill(s) that day
  mismatch: '#dc2626', // red-600   — gross differs → a fill is probably missing
}

export const LABEL: Record<CellState, string> = {
  flat: 'flat (closed to ~0 at EOD)',
  open: 'open at EOD — feeds agree',
  skipped: 'skipped fill(s) — in the ledger but never aggregated into a trade',
  mismatch: 'open at EOD — fills volume ≠ FIX feed (likely dropped fill)',
}
