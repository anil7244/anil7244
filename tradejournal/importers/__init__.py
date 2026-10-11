"""Import tradebook exports (CSV or Excel) from any broker.

Columns are matched by name (case/space/punctuation-insensitive) against the alias lists
below, and the header row is auto-detected, so report title rows above the table are fine.
If a broker export isn't recognised, add its column names to ALIASES or BROKER_ALIASES.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from pathlib import Path

from .. import db
from ..normalize import SkipRow, assign_row_seq, make_fill

ALIASES: dict[str, list[str]] = {
    "date": ["tradedate", "date", "transactiondate", "tradedt", "executiondate", "orderdate", "tradeday"],
    "time": ["orderexecutiontime", "tradetime", "exchangetime", "executiontime", "time", "exchangetimestamp",
             "filltimestamp", "tradedatetime", "ordertime", "datetime", "timestamp"],
    "symbol": ["symbol", "tradingsymbol", "scripname", "scrip", "scripsymbol", "contract", "contractname",
               "instrumentname", "securityname", "customsymbol", "securitydescription", "name", "stock",
               "companyname", "company", "scripcode"],
    "side": ["tradetype", "transactiontype", "buysell", "buyorsell", "side", "bs", "action", "type",
             "transaction"],
    "qty": ["quantity", "qty", "tradedqty", "tradedquantity", "filledqty", "filledquantity", "tradeqty",
            "executedqty", "netqty"],
    "price": ["price", "tradeprice", "tradedprice", "executionprice", "rate", "avgprice", "averageprice",
              "tradedrate", "marketrate", "netrate", "fillprice"],
    "value": ["tradevalue", "amount", "value", "turnover", "netamount", "totalvalue"],
    "trade_id": ["tradeid", "tradeno", "tradenumber", "tradenum", "exchangetradeid", "tradenos"],
    "order_id": ["orderid", "orderno", "ordernumber", "exchangeorderid", "orderref"],
    "exchange": ["exchange", "exch", "exchangesegment", "exchangename"],
    "segment": ["segment", "seg", "productsegment"],
    "expiry": ["expirydate", "expiry", "exp", "drvexpirydate", "expdate"],
    "strike": ["strikeprice", "strike", "drvstrikeprice", "strikerate"],
    "option_type": ["optiontype", "opttype", "callput", "drvoptiontype", "cepe", "optype"],
    "instrument_type": ["instrumenttype", "insttype", "instrument", "series"],
    "product": ["product", "producttype"],
    "charges": ["totalcharges", "charges", "totalcharge"],
    "underlying": ["underlying", "underlyingsymbol"],
}
# Extra/preferred names per broker (checked before the generic list).
BROKER_ALIASES: dict[str, dict[str, list[str]]] = {
    "zerodha": {"side": ["tradetype"], "time": ["orderexecutiontime"]},
    "dhan": {"symbol": ["customsymbol", "name", "scripname"], "price": ["tradedprice", "tradeprice", "price"]},
    "upstox": {"symbol": ["scripname", "symbol", "company"]},
}
REQUIRED = ("side", "qty", "symbol")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def _read_rows(path: Path) -> list[list[str]]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        import pandas as pd
        sheets = pd.read_excel(path, header=None, dtype=str, sheet_name=None)
        rows: list[list[str]] = []
        for df in sheets.values():   # some brokers split segments across sheets
            rows += df.fillna("").astype(str).values.tolist()
        return rows
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _map_header(header: list[str], broker: str) -> dict[str, int]:
    normed = [_norm(h) for h in header]
    mapping: dict[str, int] = {}
    used: set[int] = set()
    extra = BROKER_ALIASES.get(broker, {})
    for field in ALIASES:
        for alias in extra.get(field, []) + ALIASES[field]:
            if alias in normed and normed.index(alias) not in used:
                mapping[field] = normed.index(alias)
                used.add(mapping[field])
                break
    if "price" not in mapping and "value" not in mapping:
        return {}
    return mapping


def detect_table(rows: list[list[str]], broker: str) -> tuple[int, dict[str, int]]:
    """Find the header row: the first row (within 60) that maps all required fields."""
    best = (-1, {})
    for i, row in enumerate(rows[:60]):
        mapping = _map_header(row, broker)
        if all(f in mapping for f in REQUIRED) and ("date" in mapping or "time" in mapping):
            if len(mapping) > len(best[1]):
                best = (i, mapping)
            break
    return best


def parse_file(path: str | Path, broker: str, account: str = "default", source: str | None = None):
    """Return (fills, skipped) where skipped is a list of (row_number, reason)."""
    path = Path(path)
    rows = _read_rows(path)
    header_idx, mapping = detect_table(rows, broker)
    if header_idx < 0:
        raise ValueError(
            f"{path.name}: could not find a header row with date, symbol, buy/sell, quantity and price "
            "columns. Add the column names to ALIASES in tradejournal/importers/__init__.py."
        )
    header_len = len(rows[header_idx])
    fills, skipped = [], []
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not any(str(c).strip() for c in row):
            continue
        if len(row) < header_len:
            row = row + [""] * (header_len - len(row))
        if [_norm(c) for c in row] == [_norm(c) for c in rows[header_idx]]:
            continue   # repeated header (multi-sheet / multi-section exports)
        record = {field: row[idx] for field, idx in mapping.items()}
        try:
            fills.append(make_fill(record, broker, account, source or f"{broker}_csv"))
        except SkipRow as e:
            skipped.append((n, str(e)))
    assign_row_seq(fills)
    return fills, skipped


def import_file(conn: sqlite3.Connection, path: str | Path, broker: str, account: str = "default",
                source: str | None = None) -> dict:
    fills, skipped = parse_file(path, broker, account, source)
    inserted = db.insert_fills(conn, fills)
    dates = sorted(f["trade_date"] for f in fills)
    db.log_sync(conn, broker, source or f"{broker}_csv", dates[0] if dates else None,
                dates[-1] if dates else None, len(fills), inserted)
    return {"file": str(path), "parsed": len(fills), "inserted": inserted, "skipped": skipped,
            "from": dates[0] if dates else None, "to": dates[-1] if dates else None}
