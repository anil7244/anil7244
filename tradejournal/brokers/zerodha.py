"""Zerodha Kite Connect. The API only returns *today's* trades, so run `sync zerodha` every
trading day after market close; older history comes from the Console tradebook CSV."""
from __future__ import annotations

import hashlib

import requests

from .. import config
from ..normalize import SkipRow, make_fill
from . import AuthError, extract_param, load_token, save_token

API = "https://api.kite.trade"


def _creds() -> tuple[str, str]:
    key, secret = config.env("KITE_API_KEY"), config.env("KITE_API_SECRET")
    if not key or not secret:
        raise AuthError("Set KITE_API_KEY and KITE_API_SECRET in .env (from developers.kite.trade).")
    return key, secret


def login_url() -> str:
    key, _ = _creds()
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"


def complete_login(redirect_url_or_token: str) -> str:
    key, secret = _creds()
    request_token = extract_param(redirect_url_or_token, "request_token")
    checksum = hashlib.sha256(f"{key}{request_token}{secret}".encode()).hexdigest()
    r = requests.post(f"{API}/session/token", headers={"X-Kite-Version": "3"},
                      data={"api_key": key, "request_token": request_token, "checksum": checksum}, timeout=30)
    body = r.json()
    if r.status_code != 200 or body.get("status") != "success":
        raise AuthError(f"Kite login failed: {body.get('message', r.text)}")
    token = body["data"]["access_token"]
    save_token("zerodha", token, user_id=body["data"].get("user_id"))
    return token


def fetch_fills(account: str = "default") -> tuple[list[dict], list]:
    key, _ = _creds()
    token = load_token("zerodha")
    if not token:
        raise AuthError("Not logged in. Run: python -m tradejournal login zerodha")
    r = requests.get(f"{API}/trades", timeout=30,
                     headers={"X-Kite-Version": "3", "Authorization": f"token {key}:{token}"})
    if r.status_code in (401, 403):
        raise AuthError("Kite session expired (tokens reset daily). Run: python -m tradejournal login zerodha")
    r.raise_for_status()
    fills, skipped = [], []
    for t in r.json().get("data", []):
        record = {
            "time": t.get("exchange_timestamp") or t.get("fill_timestamp") or t.get("order_timestamp"),
            "symbol": t.get("tradingsymbol"), "side": t.get("transaction_type"),
            "qty": t.get("quantity"), "price": t.get("average_price"),
            "trade_id": t.get("trade_id"), "order_id": t.get("order_id"),
            "exchange": t.get("exchange"), "product": t.get("product"),
        }
        try:
            fills.append(make_fill(record, "zerodha", account, "zerodha_api"))
        except SkipRow as e:
            skipped.append((t.get("trade_id"), str(e)))
    return fills, skipped
