"""Upstox API v2. Historical trades (GET /v2/charges/historical-trades) go back roughly three
financial years; older years come from the tradebook export on upstox.com (Reports)."""
from __future__ import annotations

import time
from datetime import date, timedelta
from urllib.parse import urlencode

import requests

from .. import config
from ..normalize import SkipRow, make_fill
from . import AuthError, extract_param, load_token, save_token

API = "https://api.upstox.com/v2"
SEGMENTS = ("FO", "EQ", "COM", "CD")


def _creds() -> tuple[str, str, str]:
    key, secret = config.env("UPSTOX_API_KEY"), config.env("UPSTOX_API_SECRET")
    redirect = config.env("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8765/callback")
    if not key or not secret:
        raise AuthError("Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env (from account.upstox.com/developer/apps).")
    return key, secret, redirect


def login_url() -> str:
    key, _, redirect = _creds()
    return f"{API}/login/authorization/dialog?" + urlencode(
        {"response_type": "code", "client_id": key, "redirect_uri": redirect})


def complete_login(redirect_url_or_code: str) -> str:
    key, secret, redirect = _creds()
    code = extract_param(redirect_url_or_code, "code")
    r = requests.post(f"{API}/login/authorization/token", timeout=30,
                      headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                      data={"code": code, "client_id": key, "client_secret": secret,
                            "redirect_uri": redirect, "grant_type": "authorization_code"})
    body = r.json()
    if r.status_code != 200 or "access_token" not in body:
        raise AuthError(f"Upstox login failed: {body}")
    save_token("upstox", body["access_token"], user_id=body.get("user_id"))
    return body["access_token"]


def _headers() -> dict:
    token = load_token("upstox")
    if not token:
        raise AuthError("Not logged in. Run: python -m tradejournal login upstox")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "Api-Version": "2.0"}


def _get(url: str, params: dict) -> dict:
    for _ in range(5):
        r = requests.get(url, headers=_headers(), params=params, timeout=60)
        if r.status_code == 401:
            raise AuthError("Upstox token expired (tokens reset daily). Run: python -m tradejournal login upstox")
        if r.status_code == 429:
            time.sleep(2)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Upstox {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("Upstox rate limit: too many retries")


def _fy_windows(start: date, end: date):
    cur = start
    while cur <= end:
        fy_end = date(cur.year if cur.month < 4 else cur.year + 1, 3, 31)
        win_end = min(fy_end, end)
        yield cur, win_end
        cur = win_end + timedelta(days=1)


def _hist_record(t: dict, segment: str) -> dict:
    return {
        "date": t.get("trade_date"), "symbol": t.get("symbol") or t.get("scrip_name"),
        "side": t.get("transaction_type"), "qty": t.get("quantity"), "price": t.get("price"),
        "value": t.get("amount"), "trade_id": t.get("trade_id"), "exchange": t.get("exchange"),
        "segment": t.get("segment") or segment, "expiry": t.get("expiry"), "strike": t.get("strike_price"),
        "option_type": t.get("option_type"),
    }


def fetch_fills(start: date, end: date, account: str = "default", segments=SEGMENTS,
                progress=print) -> tuple[list[dict], list]:
    page_size = int(config.env("UPSTOX_PAGE_SIZE", "100"))
    fills, skipped = [], []
    for seg in segments:
        for w_start, w_end in _fy_windows(start, end):
            page, total_pages = 1, 1
            while page <= total_pages:
                body = _get(f"{API}/charges/historical-trades", {
                    "segment": seg, "start_date": w_start.isoformat(), "end_date": w_end.isoformat(),
                    "page_number": page, "page_size": page_size})
                for t in body.get("data") or []:
                    try:
                        fills.append(make_fill(_hist_record(t, seg), "upstox", account, "upstox_api"))
                    except SkipRow as e:
                        skipped.append((t.get("trade_id"), str(e)))
                meta = (body.get("meta_data") or {}).get("page") or {}
                total_pages = int(meta.get("total_pages") or 1)
                page += 1
                time.sleep(0.3)
            progress(f"  upstox {seg} {w_start} -> {w_end}: {len(fills)} fills so far")
    return fills, skipped


def fetch_today(account: str = "default") -> tuple[list[dict], list]:
    body = _get(f"{API}/order/trades/get-trades-for-day", {})
    fills, skipped = [], []
    for t in body.get("data") or []:
        record = {
            "time": t.get("exchange_timestamp") or t.get("order_timestamp"),
            "symbol": t.get("trading_symbol") or t.get("tradingsymbol"), "side": t.get("transaction_type"),
            "qty": t.get("quantity"), "price": t.get("average_price"), "trade_id": t.get("trade_id"),
            "order_id": t.get("order_id"), "exchange": t.get("exchange"), "product": t.get("product"),
        }
        try:
            fills.append(make_fill(record, "upstox", account, "upstox_api_today"))
        except SkipRow as e:
            skipped.append((t.get("trade_id"), str(e)))
    return fills, skipped
