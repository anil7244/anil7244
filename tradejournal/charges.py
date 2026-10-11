"""Estimate brokerage + statutory charges for fills that don't come with actual charges.

Rates are percentages of turnover with the date they took effect. They are close to
NSE/BSE published rates but not exact for every period (exchange charges were revised
several times before 2024). Dhan's API returns actual charges, which are always used
instead. Edit BROKERAGE below if your plan differs.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date

_D0 = date(2000, 1, 1)

# STT, % of sell-side turnover (options: premium).
STT = {
    "OPT": [(_D0, 0.017), (date(2016, 6, 1), 0.05), (date(2023, 4, 1), 0.0625),
            (date(2024, 10, 1), 0.1), (date(2026, 4, 1), 0.15)],
    "FUT": [(_D0, 0.01), (date(2023, 4, 1), 0.0125), (date(2024, 10, 1), 0.02),
            (date(2026, 4, 1), 0.05)],
    "EQ": [(_D0, 0.1)],  # delivery rate, charged on both sides
}
# Exchange transaction charges, % of turnover.
EXCHANGE = {
    "NSE": {"OPT": [(_D0, 0.053), (date(2024, 10, 1), 0.03503)],
            "FUT": [(_D0, 0.0019), (date(2024, 10, 1), 0.00173)],
            "EQ": [(_D0, 0.00325), (date(2024, 10, 1), 0.00297)]},
    "BSE": {"OPT": [(_D0, 0.0325)],
            "FUT": [(_D0, 0.0)],
            "EQ": [(_D0, 0.00375)]},
}
# Stamp duty, % of buy-side turnover (uniform nationwide from 1 Jul 2020).
STAMP = {"OPT": 0.003, "FUT": 0.002, "EQ": 0.015}
SEBI_FEE = 0.0001          # Rs 10 per crore
GST = [(_D0, 15.0), (date(2017, 7, 1), 18.0)]   # service tax before GST

# Per executed order. F&O options: flat. Futures/equity: min(flat, pct of order value).
BROKERAGE = {
    "zerodha": {"flat": 20.0, "pct": 0.03, "eq_delivery": 0.0},
    "dhan": {"flat": 20.0, "pct": 0.03, "eq_delivery": 0.0},
    "upstox": {"flat": 20.0, "pct": 0.05, "eq_delivery": 20.0},
    "default": {"flat": 20.0, "pct": 0.03, "eq_delivery": 0.0},
}


def _rate(table: list[tuple[date, float]], d: date) -> float:
    i = bisect_right([t[0] for t in table], d) - 1
    return table[max(i, 0)][1]


def kind_class(kind: str) -> str:
    return "OPT" if kind in ("CE", "PE") else ("FUT" if kind == "FUT" else "EQ")


def order_brokerage(broker: str, kind: str, order_value: float) -> float:
    cfg = BROKERAGE.get(broker, BROKERAGE["default"])
    kc = kind_class(kind)
    if kc == "OPT":
        return cfg["flat"]
    if kc == "FUT":
        return min(cfg["flat"], order_value * cfg["pct"] / 100)
    return cfg["eq_delivery"] if cfg["eq_delivery"] == 0 else min(cfg["eq_delivery"], order_value * cfg["pct"] / 100)


def statutory(kind: str, exchange: str, segment: str, side: str, value: float, d: date) -> tuple[float, float]:
    """Return (charges excluding GST, GST-able portion) for one fill."""
    if segment not in ("FO", "EQ"):
        return 0.0, 0.0   # commodity / currency: not estimated
    kc = kind_class(kind)
    ex_table = EXCHANGE.get(exchange, EXCHANGE["NSE"])[kc]
    exch = value * _rate(ex_table, d) / 100
    sebi = value * SEBI_FEE / 100
    stt = 0.0
    if kc == "EQ" or side == "SELL":
        stt = value * _rate(STT[kc], d) / 100
    stamp = value * STAMP[kc] / 100 if side == "BUY" else 0.0
    return stt + stamp + exch + sebi, exch + sebi


def gst_rate(d: date) -> float:
    return _rate(GST, d) / 100
