"""Find any NSE stock from what the user types: an exact symbol, part of a symbol, or a company name."""
from __future__ import annotations

import logging

from bot import angel

log = logging.getLogger(__name__)
_EQ: dict = {}


def instrument_list():
    """Angel One's public instrument list (needs no login), or None if it can't be loaded."""
    try:
        if angel.configured():
            return angel.session().inst
        return angel.instruments()
    except Exception as exc:  # login or download trouble must never break an analysis
        log.info("Instrument list unavailable: %s", exc)
        return None


def nse_symbols(inst) -> set[str]:
    if _EQ.get("id") != id(inst):
        _EQ["id"], _EQ["set"] = id(inst), set(angel.nse_equities(inst)["sym"])
    return _EQ["set"]


def yahoo_search(query: str, limit: int = 8) -> list[str]:
    """Company-name search on Yahoo; returns NSE symbols (without .NS)."""
    try:
        import yfinance as yf

        quotes = yf.Search(query, max_results=15, news_count=0).quotes or []
    except Exception as exc:  # unofficial API: never let it break the bot
        log.info("Yahoo search failed: %s", exc.__class__.__name__)
        return []
    out = []
    for q in quotes:
        sym = str(q.get("symbol", ""))
        if sym.endswith(".NS") and sym[:-3] not in out:
            out.append(sym[:-3])
    return out[:limit]


def clean(query: str) -> str:
    return query.strip().upper().removeprefix("$").removesuffix(".NS").replace(" ", "")


def find_stock(query: str) -> tuple[str | None, list[str]]:
    """(symbol, []) when the query names one stock or index, or (None, candidates) to choose from."""
    sym = clean(query)
    if not sym:
        return None, []
    if sym in angel.INDEX_ALIASES:
        return sym, []
    inst = instrument_list()
    if inst is not None:
        if sym in nse_symbols(inst):
            return sym, []
        matches = angel.search_equities(inst, query)
        if " " in query.strip() or not matches:  # company names: ask Yahoo too
            matches += [m for m in yahoo_search(query) if m not in matches]
        if len(matches) == 1:
            return matches[0], []
        return None, matches[:8]
    matches = yahoo_search(query)
    if sym in matches or not matches:
        return sym, []           # no list to check against: try the symbol as typed
    return (matches[0], []) if len(matches) == 1 else (None, matches)


def has_fno(name: str) -> bool:
    if not angel.configured():
        return False
    if name.upper() in angel.INDEX_ALIASES:
        return True
    inst = instrument_list()
    return bool(inst is not None and angel.has_fno(inst, name))
