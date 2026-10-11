"""Rebuild round-trip trades from raw fills.

A trade is one position cycle on one contract: it opens when the position leaves zero
and closes when it returns to zero (scaling in/out stays in the same trade). A fill that
flips the position (long 50 -> short 25) is split between the two trades. Options still
open after expiry are settled at the price in `settlements` (default 0 = expired
worthless); futures past expiry wait for you to enter a settlement price.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from . import charges as ch
from .instruments import Instrument

# When the same broker/account/day has fills from several sources, only the
# highest-priority source is used, so overlapping CSV + API data is never double counted.
SOURCE_PRIORITY = {
    "zerodha_csv": 3, "zerodha_api": 2,
    "dhan_api": 3, "dhan_csv": 2,
    "upstox_api": 3, "upstox_csv": 2, "upstox_api_today": 1,
}
EPS = 1e-9


@dataclass
class Cycle:
    direction: str
    open_ts: str
    uid_src: dict
    entry_qty: float = 0.0
    entry_value: float = 0.0
    exit_qty: float = 0.0
    exit_value: float = 0.0
    max_pos: float = 0.0
    charges: float = 0.0
    estimated: bool = False
    close_ts: str | None = None
    status: str = "OPEN"
    parts: list = field(default_factory=list)   # (fill_id, qty, role)
    fill_ids: set = field(default_factory=set)


def _select_sources(fills: list[dict]) -> list[dict]:
    by_day: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for f in fills:
        by_day[(f["broker"], f["account"], f["trade_date"])][f["source"]] += 1
    chosen = {
        day: max(counts, key=lambda s: (SOURCE_PRIORITY.get(s, 2), counts[s]))
        for day, counts in by_day.items()
    }
    return [f for f in fills if chosen[(f["broker"], f["account"], f["trade_date"])] == f["source"]]


def _snap_expiries(fills: list[dict]) -> None:
    """Replace estimated monthly expiries with a real expiry date seen elsewhere in the data."""
    exact: dict[tuple, set] = defaultdict(set)
    for f in fills:
        if f["expiry"] and f["expiry_exact"]:
            exact[(f["underlying"], f["expiry"][:7])].add(f["expiry"])
    for f in fills:
        if f["expiry"] and not f["expiry_exact"]:
            known = exact.get((f["underlying"], f["expiry"][:7]))
            if known:
                f["expiry"] = max(known)
                f["expiry_exact"] = 1
        inst = Instrument(f["underlying"], f["kind"],
                          date.fromisoformat(f["expiry"]) if f["expiry"] else None, f["strike"])
        f["instrument_key"] = inst.key
        f["symbol"] = inst.display


def _estimate_charges(fills: list[dict]) -> None:
    orders: dict[tuple, list[dict]] = defaultdict(list)
    for f in fills:
        f["est"] = f["charges"] is None
        if f["est"]:
            oid = f["order_id"] or f"fill-{f['id']}"
            orders[(f["broker"], f["account"], f["trade_date"], oid, f["instrument_key"])].append(f)
    for (broker, _a, day, _o, _k), group in orders.items():
        d = date.fromisoformat(day)
        order_value = sum(g["qty"] * g["price"] for g in group)
        brokerage = ch.order_brokerage(broker, group[0]["kind"], order_value)
        gst = ch.gst_rate(d)
        for g in group:
            value = g["qty"] * g["price"]
            share = value / order_value if order_value > 0 else 1 / len(group)
            stat, gstable = ch.statutory(g["kind"], g["exchange"] or "NSE", g["segment"] or "FO",
                                         g["side"], value, d)
            b = brokerage * share
            g["charges"] = round(b + stat + (b + gstable) * gst, 4)


def _uid(f: dict) -> str:
    return hashlib.sha1(
        f"{f['broker']}|{f['account']}|{f['instrument_key']}|{f['dedupe_key']}".encode()
    ).hexdigest()[:16]


def _sort_key(f: dict):
    tid = str(f["trade_id"] or "")
    return (f["ts"], int(tid) if tid.isdigit() else 0, tid, f["id"])


def build_trades(fills: list[dict], settlements: dict[str, float], today: date | None = None):
    today = today or date.today()
    fills = _select_sources(fills)
    _snap_expiries(fills)
    _estimate_charges(fills)

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in fills:
        groups[(f["broker"], f["account"], f["instrument_key"])].append(f)

    trades, links = [], []
    for (_b, _a, key), group in groups.items():
        group.sort(key=_sort_key)
        pos = 0.0
        cycle: Cycle | None = None
        done: list[Cycle] = []
        for f in group:
            signed = f["qty"] if f["side"] == "BUY" else -f["qty"]
            remaining = signed
            while abs(remaining) > EPS:
                if cycle is None:
                    cycle = Cycle("LONG" if remaining > 0 else "SHORT", f["ts"], f)
                if abs(pos) < EPS or (pos > 0) == (remaining > 0):
                    part = remaining
                    cycle.entry_qty += abs(part)
                    cycle.entry_value += abs(part) * f["price"]
                    role = "ENTRY"
                else:
                    part = remaining if abs(remaining) <= abs(pos) else -pos
                    cycle.exit_qty += abs(part)
                    cycle.exit_value += abs(part) * f["price"]
                    role = "EXIT"
                cycle.charges += f["charges"] * abs(part) / f["qty"]
                cycle.estimated |= f["est"]
                cycle.parts.append((f["id"], abs(part), role))
                cycle.fill_ids.add(f["id"])
                pos += part
                remaining -= part
                cycle.max_pos = max(cycle.max_pos, abs(pos))
                if abs(pos) < EPS:
                    pos = 0.0
                    cycle.close_ts = f["ts"]
                    cycle.status = "CLOSED"
                    done.append(cycle)
                    cycle = None
        if cycle is not None:
            last = group[-1]
            exp = date.fromisoformat(last["expiry"]) if last["expiry"] else None
            if exp and exp < today:
                price = settlements.get(key, 0.0 if last["kind"] in ("CE", "PE") else None)
                if price is not None:
                    q = abs(pos)
                    cycle.exit_qty += q
                    cycle.exit_value += q * price
                    cycle.parts.append((None, q, "SETTLEMENT"))
                    cycle.close_ts = f"{exp.isoformat()}T15:30:00"
                    cycle.status = "EXPIRED"
                else:
                    cycle.status = "NEEDS_SETTLEMENT"
            done.append(cycle)

        for c in done:
            ref = c.uid_src
            sign = 1 if c.direction == "LONG" else -1
            entry_avg = c.entry_value / c.entry_qty
            exit_avg = c.exit_value / c.exit_qty if c.exit_qty > EPS else None
            gross = sign * ((exit_avg or 0) - entry_avg) * c.exit_qty if exit_avg is not None else 0.0
            if c.status in ("CLOSED", "EXPIRED"):
                gross = sign * (c.exit_value - c.entry_value)
            holding = None
            if c.close_ts:
                holding = (datetime.fromisoformat(c.close_ts) - datetime.fromisoformat(c.open_ts)).total_seconds() / 60
            uid = _uid(ref)
            trades.append({
                "trade_uid": uid, "broker": ref["broker"], "account": ref["account"],
                "instrument_key": key, "symbol": ref["symbol"], "underlying": ref["underlying"],
                "kind": ref["kind"], "expiry": ref["expiry"], "strike": ref["strike"],
                "exchange": ref["exchange"], "segment": ref["segment"],
                "direction": c.direction, "status": c.status, "open_ts": c.open_ts, "close_ts": c.close_ts,
                "qty": c.max_pos, "entry_qty": c.entry_qty, "exit_qty": c.exit_qty,
                "entry_avg": round(entry_avg, 4), "exit_avg": round(exit_avg, 4) if exit_avg is not None else None,
                "gross_pnl": round(gross, 2), "charges": round(c.charges, 2),
                "charges_estimated": int(c.estimated), "net_pnl": round(gross - c.charges, 2),
                "num_fills": len(c.fill_ids), "holding_minutes": holding,
            })
            links += [(uid, fid, q, role) for fid, q, role in c.parts if fid is not None]
    return trades, links


def rebuild(conn: sqlite3.Connection, today: date | None = None) -> dict:
    fills = [dict(r) for r in conn.execute("SELECT * FROM fills")]
    settlements = {r["instrument_key"]: r["price"] for r in conn.execute("SELECT * FROM settlements")}
    trades, links = build_trades(fills, settlements, today)
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM trade_fills")
    if trades:
        cols = list(trades[0].keys())
        conn.executemany(
            f"INSERT INTO trades ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [[t[c] for c in cols] for t in trades],
        )
    # A fill can map to the same trade twice with the same role only via splits; merge those.
    merged: dict[tuple, float] = defaultdict(float)
    for uid, fid, q, role in links:
        merged[(uid, fid, role)] += q
    conn.executemany("INSERT INTO trade_fills (trade_uid, fill_id, qty, role) VALUES (?,?,?,?)",
                     [(u, f, q, r) for (u, f, r), q in merged.items()])
    conn.commit()
    status = defaultdict(int)
    for t in trades:
        status[t["status"]] += 1
    return {"fills": len(fills), "trades": len(trades), **status}
