"""DhanHQ v2. Trade history with actual charges is available for any date range
(GET /v2/trades/{from}/{to}/{page}). Generate an access token at web.dhan.co ->
My Profile -> Access DhanHQ APIs (valid 24 hours) and put it in .env or run `login dhan`."""
from __future__ import annotations

import time
from datetime import date, timedelta

import requests

from .. import config
from ..normalize import SkipRow, make_fill
from . import AuthError, load_token, save_token

API = "https://api.dhan.co/v2"
WINDOW_DAYS = 30


def save_access_token(token: str) -> None:
    save_token("dhan", token.strip())


def _headers() -> dict:
    client_id = config.env("DHAN_CLIENT_ID")
    token = load_token("dhan") or config.env("DHAN_ACCESS_TOKEN")
    if not client_id or not token:
        raise AuthError("Set DHAN_CLIENT_ID in .env and DHAN_ACCESS_TOKEN (or run: python -m tradejournal login dhan).")
    return {"access-token": token, "client-id": client_id, "Accept": "application/json"}


def _na(v):
    return None if v in (None, "", "NA", "N/A") else v


def _record(t: dict) -> dict:
    charges = 0.0
    any_charge = False
    for k in ("brokerageCharges", "stt", "sebiTax", "serviceTax", "exchangeTransactionCharges", "stampDuty"):
        if t.get(k) not in (None, "", "NA"):
            charges += float(t[k])
            any_charge = True
    segment = str(t.get("exchangeSegment") or "")
    option_type = _na(t.get("drvOptionType"))
    expiry = _na(t.get("drvExpiryDate"))
    symbol = t.get("customSymbol") or t.get("tradingSymbol") or t.get("securityId")
    return {
        "time": _na(t.get("exchangeTime")) or _na(t.get("createTime")) or _na(t.get("updateTime")),
        "symbol": symbol, "side": t.get("transactionType"),
        "qty": t.get("tradedQuantity"), "price": t.get("tradedPrice"),
        "trade_id": t.get("exchangeTradeId"), "order_id": t.get("orderId") or t.get("exchangeOrderId"),
        "exchange": segment, "expiry": expiry, "strike": t.get("drvStrikePrice"),
        "option_type": option_type, "instrument_type": t.get("instrument"),
        "product": t.get("productType"), "charges": charges if any_charge else None,
    }


def fetch_fills(start: date, end: date, account: str = "default", progress=print) -> tuple[list[dict], list]:
    headers = _headers()
    fills, skipped = [], []
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=WINDOW_DAYS - 1), end)
        page = 0
        while True:
            url = f"{API}/trades/{cur.isoformat()}/{win_end.isoformat()}/{page}"
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code in (401, 403):
                raise AuthError("Dhan token invalid or expired. Generate a new one at web.dhan.co.")
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            items = r.json()
            if isinstance(items, dict):
                items = items.get("data", [])
            if not items:
                break
            for t in items:
                try:
                    fills.append(make_fill(_record(t), "dhan", account, "dhan_api"))
                except SkipRow as e:
                    skipped.append((t.get("exchangeTradeId"), str(e)))
            page += 1
            time.sleep(0.3)
        progress(f"  dhan {cur} -> {win_end}: {len(fills)} fills so far")
        cur = win_end + timedelta(days=1)
    return fills, skipped
