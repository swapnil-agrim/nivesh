"""The owner's equity and ETF holdings as securities, shared by thesis, review and the
scheduled monitors."""

import sqlite3

from nivesh_agents.thesis_agent import THESIS_CLASSES, Held, merge_held
from nivesh_core.holdings_store import latest_holdings


def held_securities(sql: sqlite3.Connection) -> list[Held]:
    """Equity and ETF holdings merged per security, largest INR value first."""
    rows: list[Held] = []
    for h in latest_holdings(sql):
        if h.asset_class not in THESIS_CLASSES:
            continue
        row = None
        if h.isin:
            row = sql.execute(
                "SELECT id, name, market FROM security WHERE isin = ? "
                "ORDER BY unresolved, id LIMIT 1",
                (h.isin,),
            ).fetchone()
        if row is None:
            row = sql.execute(
                "SELECT id, name, market FROM security WHERE symbol = ? AND exchange = ?",
                (h.symbol, h.exchange),
            ).fetchone()
        rows.append(Held(int(row[0]), h.symbol, h.asset_class, h.value_inr, row[1] or "", row[2]))
    return merge_held(rows)
