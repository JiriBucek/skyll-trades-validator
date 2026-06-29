"""Parse a contract's expiry month from its string. Used ONLY as a display filter — to push
ancient, non-flat, un-chased contracts into the collapsed "residual" bucket so they don't flood
the active view. This is NOT a settlement concept: Skyll has no expiry logic; we only aggregate
the fills ledger into trades. A non-flat expired contract is still just a non-flat ledger (usually
a pre-retention lost fill we aren't chasing). See recovery/PRINCIPLES.md.

Contract strings look like: "MES Jun26", "FGBL Jun26", "yEBM Sep24", "SR3 Jun24",
"EW2 W02Aug-24 P500000" (weekly option). We look for a `Mmm YY` / `Mmm-YY` token anywhere
in the string and treat the contract as expiring at the END of that month.
"""
import re
from datetime import date

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-?\s?(\d{2})\b", re.I)


def expiry_month(contract: str | None) -> date | None:
    """Return the first day of the contract's expiry month, or None if unparseable."""
    if not contract:
        return None
    m = _RE.search(contract)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()]
    year = 2000 + int(m.group(2))
    return date(year, month, 1)


def is_expired(contract: str | None, today: date) -> bool | None:
    """True if the contract's expiry month is strictly before `today`'s month.
    None when the expiry can't be parsed (caller should treat as 'active', i.e. not hide it)."""
    exp = expiry_month(contract)
    if exp is None:
        return None
    today_month = date(today.year, today.month, 1)
    return exp < today_month
