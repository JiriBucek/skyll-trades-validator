"""Hard-coded list of SPREAD traders the validator IGNORES.

WHY: Skyll only forms trades by aggregating fills until the per-contract position returns to ZERO.
Calendar / inter-contract spread traders hold offsetting legs across expiries (and on some
strategies we don't even receive the fills), so their per-contract position never returns to flat
by design — the validator's phantom-open check is meaningless for them and they drown the real
signal. Per operator decision (2026-06-27): we DO NOT support spread strategies; flag these traders
and exclude them from the validator export so only normally-trading (close-to-zero) traders remain.

HOW THIS LIST WAS BUILT (reproducible): collapse_pct = 1 - sum(abs(per-PRODUCT net)) /
sum(abs(per-CONTRACT net)) over a trader's fills (all platforms). >=70% = the legs offset at the
product level = spread trader. Anchored on ground truth: tr5 Demetris = 4.5% (drop victim, NOT
listed), tr19 Greg = 87.3% (spreader). Corroborated by: 22/24 are in the "Axia" group (the spread
desk, ~90% of all validator red) and tr335 is literally named "D Jordan - Spread trader".

To update: re-run the collapse_pct query (see aws-mwaa-local-runner recovery memory) and reconcile.
"""

# trader_id -> (name, collapse_pct%)  — collapse_pct measured 2026-06-27, fills>=300, all platforms
SPREAD_TRADERS = {
    274: ("Vicko Perasovic", 100.0),
    264: ("Igor Horvatic", 100.0),
    335: ("D Jordan - Spread trader", 100.0), 
    11:  ("Ralph Hazell", 100.0),
    112: ("Eduardo Betancor", 100.0),
    263: ("Gansham Halai", 100.0),
    23:  ("James Pitron", 99.8),
    133: ("Pablo Carrasco", 99.3),
    270: ("Robert O'Shea", 98.0),
    12:  ("Roberto Vernazza", 97.7),
    30:  ("Luis Sanchez", 97.7),
    31:  ("Antonio Sanchez", 97.5),
    209: ("George Georgakakis", 95.7),
    206: ("Shiraz Ahmed", 94.8),
    271: ("Steve Hunter", 94.0),
    257: ("Andrew Sully", 93.1),
    258: ("Denijal Fisic", 88.4),
    19:  ("Greg Lechmar", 87.3),
    265: ("Jay Vowell", 86.8),
    269: ("Raul Hodzic", 85.7),
    # --- borderline (70-80%)
    214: ("Juan Vercher", 79.7),
    1:   ("Ryan Cohen", 78.1),
    2:   ("James Binns", 77.3),
    4:   ("Jake Nippers", 76.8),
    # archived duplicate of Greg Lechmar (kept for completeness; archived so normally not shown)
    326: ("Greg Lechmar (archived dup)", 100.0),
}

SPREAD_TRADER_IDS = frozenset(SPREAD_TRADERS)
