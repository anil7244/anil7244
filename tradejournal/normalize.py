"""Convert one broker record (CSV row or API item, already mapped to logical field names)
into a fill dict ready for db.insert_fills."""
from __future__ import annotations

import re
from datetime import datetime

from .instruments import norm_exchange_segment, parse_datetime, parse_date, parse_instrument, parse_time

LOGICAL_FIELDS = (
    "date", "time", "symbol", "side", "qty", "price", "value", "trade_id", "order_id", "exchange",
    "segment", "expiry", "strike", "option_type", "instrument_type", "product", "charges", "underlying",
)


class SkipRow(Exception):
    pass


def to_float(value) -> float | None:
    if value is None:
        return None
    s = re.sub(r"[,\s₹]", "", str(value))
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if s in ("", "-", "NA", "N/A", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm_side(value) -> str | None:
    s = str(value or "").strip().upper()
    if s.startswith("B"):
        return "BUY"
    if s.startswith("S"):
        return "SELL"
    return None


def clean_id(value) -> str | None:
    s = str(value or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s if s and s.upper() not in ("NA", "N/A", "NAN", "NONE", "0", "-") else None


def make_fill(row: dict, broker: str, account: str, source: str) -> dict:
    side = norm_side(row.get("side"))
    if side is None:
        raise SkipRow(f"unknown side {row.get('side')!r}")
    qty = to_float(row.get("qty"))
    if qty is None or qty == 0:
        raise SkipRow("missing quantity")
    qty = abs(qty)
    price = to_float(row.get("price"))
    if price is None:
        value = to_float(row.get("value"))
        if value is None:
            raise SkipRow("missing price")
        price = abs(value) / qty

    d = parse_date(row.get("date"))
    ts = parse_datetime(row.get("time"))          # time column sometimes holds a full datetime
    if ts is not None and d is not None and ts.date() != d:
        ts = datetime.combine(d, ts.time())
    if ts is None:
        hms = parse_time(row.get("time"))
        if hms is None:
            ts = parse_datetime(row.get("date"))   # date column may carry the time itself
        elif d is not None:
            ts = datetime(d.year, d.month, d.day, *hms)
    if ts is None:
        raise SkipRow("missing date")

    symbol = str(row.get("symbol") or row.get("underlying") or "").strip()
    if not symbol:
        raise SkipRow("missing symbol")
    inst = parse_instrument(
        symbol, segment=row.get("segment"), expiry=row.get("expiry"), strike=row.get("strike"),
        option_type=row.get("option_type"), instrument_type=row.get("instrument_type"),
        trade_date=ts.date(),
    )
    exchange, segment = norm_exchange_segment(row.get("exchange"), row.get("segment"), inst)
    charges = to_float(row.get("charges"))
    return {
        "broker": broker,
        "account": account or "default",
        "source": source,
        "trade_id": clean_id(row.get("trade_id")),
        "order_id": clean_id(row.get("order_id")),
        "ts": ts.isoformat(timespec="seconds"),
        "trade_date": ts.date().isoformat(),
        "exchange": exchange,
        "segment": segment,
        "symbol_raw": symbol,
        "underlying": inst.underlying,
        "kind": inst.kind,
        "expiry": inst.expiry.isoformat() if inst.expiry else None,
        "expiry_exact": int(inst.expiry_exact),
        "strike": inst.strike,
        "instrument_key": inst.key,
        "side": side,
        "qty": qty,
        "price": round(price, 4),
        "charges": abs(charges) if charges is not None else None,
        "product": (str(row.get("product")).strip().upper() or None) if row.get("product") else None,
    }


def assign_row_seq(fills: list[dict]) -> None:
    """Identical rows without trade ids are distinct fills; number repeats so re-imports stay idempotent."""
    seen: dict[tuple, int] = {}
    for f in fills:
        if f.get("trade_id"):
            continue
        sig = (f["trade_date"], f["instrument_key"], f["side"], f["ts"], f["qty"], f["price"], f.get("order_id"))
        f["_row_seq"] = seen.get(sig, 0)
        seen[sig] = f["_row_seq"] + 1
