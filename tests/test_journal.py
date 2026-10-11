from datetime import date
from pathlib import Path

import pytest

from tradejournal import charges, db, importers, matching
from tradejournal.brokers import dhan
from tradejournal.instruments import monthly_expiry, parse_instrument
from tradejournal.normalize import make_fill

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2024, 6, 1)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


@pytest.mark.parametrize("symbol,kwargs,key", [
    ("NIFTY24OCT24000CE", {}, "NIFTY|CE|2024-10-31|24000"),
    ("NIFTY2410324000CE", {}, "NIFTY|CE|2024-01-03|24000"),
    ("SENSEX24O1880000CE", {}, "SENSEX|CE|2024-10-18|80000"),
    ("BANKNIFTY24DECFUT", {}, "BANKNIFTY|FUT|2024-12-26|"),
    ("M&M24JAN1600PE", {}, "M&M|PE|2024-01-25|1600"),
    ("NIFTY 26 DEC 24000 CALL", {"trade_date": date(2024, 12, 20)}, "NIFTY|CE|2024-12-26|24000"),
    ("NIFTY 24000 PE 26 DEC 24", {}, "NIFTY|PE|2024-12-26|24000"),
    ("BANKNIFTY-Dec2024-50000-CE", {"expiry": "2024-12-24"}, "BANKNIFTY|CE|2024-12-24|50000"),
    ("BAJAJ-AUTO-Dec2024-FUT", {}, "BAJAJ-AUTO|FUT|2024-12-26|"),
    ("RELIANCE-EQ", {}, "RELIANCE|EQ||"),
    ("NIFTY", {"segment": "FO", "expiry": "26-12-2024", "strike": "24000.0", "option_type": "PUT"},
     "NIFTY|PE|2024-12-26|24000"),
])
def test_parse_instrument(symbol, kwargs, key):
    assert parse_instrument(symbol, **kwargs).key == key


def test_monthly_expiry_weekday_switch():
    assert monthly_expiry(2024, 2) == date(2024, 2, 29)     # last Thursday
    assert monthly_expiry(2025, 10) == date(2025, 10, 28)   # last Tuesday after Sep 2025


def test_zerodha_import_and_matching(conn):
    res = importers.import_file(conn, FIX / "zerodha_tradebook.csv", "zerodha")
    assert res["parsed"] == 9 and res["inserted"] == 9 and not res["skipped"]
    matching.rebuild(conn, today=TODAY)
    trades = {(r["symbol"], r["open_ts"]): dict(r) for r in conn.execute("SELECT * FROM trades")}

    # Scale-in long option: buy 50@100 + 50@110, sell 100@130 -> +2500 gross
    t = trades[("NIFTY 03-Jan-2024 21500 CE", "2024-01-02T09:20:00")]
    assert (t["direction"], t["status"], t["gross_pnl"], t["entry_avg"]) == ("LONG", "CLOSED", 2500.0, 105.0)
    assert t["charges"] > 0 and t["net_pnl"] == round(t["gross_pnl"] - t["charges"], 2)

    # Short option overnight: sell 15@200, buy 15@150 -> +750
    t = trades[("BANKNIFTY 25-Jan-2024 48000 PE", "2024-01-10T11:00:00")]
    assert (t["direction"], t["gross_pnl"]) == ("SHORT", 750.0)

    # Futures flip: long 50@22000, sell 75@22100 (close 50, open short 25), buy 25@22050
    long_t = trades[("NIFTY 29-Feb-2024 FUT", "2024-02-05T09:30:00")]
    short_t = trades[("NIFTY 29-Feb-2024 FUT", "2024-02-06T09:30:00")]
    assert (long_t["direction"], long_t["gross_pnl"]) == ("LONG", 5000.0)
    assert (short_t["direction"], short_t["gross_pnl"], short_t["qty"]) == ("SHORT", 1250.0, 25)

    # Option bought and never sold, expired -> settled at 0
    t = trades[("NIFTY 28-Mar-2024 23000 CE", "2024-03-01T10:00:00")]
    assert (t["status"], t["gross_pnl"]) == ("EXPIRED", -2000.0)


def test_reimport_is_idempotent(conn):
    importers.import_file(conn, FIX / "zerodha_tradebook.csv", "zerodha")
    again = importers.import_file(conn, FIX / "zerodha_tradebook.csv", "zerodha")
    assert again["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 9


def test_settlement_override_and_journal_survives_rebuild(conn):
    importers.import_file(conn, FIX / "zerodha_tradebook.csv", "zerodha")
    matching.rebuild(conn, today=TODAY)
    uid, key = conn.execute("SELECT trade_uid, instrument_key FROM trades WHERE status='EXPIRED'").fetchone()
    conn.execute("INSERT INTO journal (trade_uid, notes) VALUES (?, 'held too long')", (uid,))
    conn.execute("INSERT INTO settlements VALUES (?, 55)", (key,))
    conn.commit()
    matching.rebuild(conn, today=TODAY)
    row = conn.execute("SELECT t.gross_pnl, j.notes FROM trades t JOIN journal j USING (trade_uid) "
                       "WHERE t.trade_uid=?", (uid,)).fetchone()
    assert tuple(row) == (750.0, "held too long")


def test_header_detection_with_title_rows(conn):
    res = importers.import_file(conn, FIX / "upstox_style.csv", "upstox")
    assert res["parsed"] == 2, res["skipped"]
    matching.rebuild(conn, today=TODAY)
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["symbol"] == "NIFTY 07-Mar-2024 22000 CE"
    assert t["open_ts"] == "2024-03-05T10:15:30" and t["gross_pnl"] == pytest.approx(987.5)


def test_csv_and_api_overlap_not_double_counted(conn):
    importers.import_file(conn, FIX / "upstox_style.csv", "upstox")
    api_fills = [make_fill({"date": "2024-03-05", "symbol": "NIFTY", "segment": "FO", "expiry": "2024-03-07",
                            "strike": "22000", "option_type": "CE", "side": side, "qty": 50, "price": px,
                            "trade_id": tid}, "upstox", "default", "upstox_api")
                 for side, px, tid in (("BUY", 120.5, "X1"), ("SELL", 140.25, "X2"))]
    db.insert_fills(conn, api_fills)
    matching.rebuild(conn, today=TODAY)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_dhan_api_record_uses_actual_charges():
    item = {"orderId": "1", "exchangeTradeId": "77", "transactionType": "SELL", "exchangeSegment": "NSE_FNO",
            "productType": "INTRADAY", "customSymbol": "NIFTY 26 DEC 24000 CALL", "tradedQuantity": 75,
            "tradedPrice": 100.5, "instrument": "OPTIDX", "sebiTax": 0.01, "stt": 7.5, "brokerageCharges": 20,
            "serviceTax": 4.0, "exchangeTransactionCharges": 2.6, "stampDuty": 0, "createTime": "NA",
            "exchangeTime": "2024-12-20 10:00:46", "drvExpiryDate": "2024-12-26", "drvOptionType": "CALL",
            "drvStrikePrice": 24000.0}
    f = make_fill(dhan._record(item), "dhan", "default", "dhan_api")
    assert f["instrument_key"] == "NIFTY|CE|2024-12-26|24000"
    assert (f["exchange"], f["segment"], f["side"], f["ts"]) == ("NSE", "FO", "SELL", "2024-12-20T10:00:46")
    assert f["charges"] == pytest.approx(34.11)


def test_stt_rates_by_date():
    sell_value = 100000
    old, _ = charges.statutory("CE", "NSE", "FO", "SELL", sell_value, date(2022, 1, 1))
    new, _ = charges.statutory("CE", "NSE", "FO", "SELL", sell_value, date(2026, 5, 1))
    assert old == pytest.approx(50 + 53 + 0.1)          # STT 0.05% + exch 0.053% + SEBI
    assert new == pytest.approx(150 + 35.03 + 0.1)      # STT 0.15% from Apr 2026
