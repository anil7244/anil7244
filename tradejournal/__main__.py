"""Command line: python -m tradejournal <command>"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import analytics, db, importers, matching
from .brokers import AuthError

BROKERS = ("zerodha", "dhan", "upstox")


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _rebuild(conn) -> None:
    stats = matching.rebuild(conn)
    print("Rebuilt trades:", ", ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_import(args) -> None:
    conn = db.connect()
    files: list[Path] = []
    for p in args.files:
        path = Path(p)
        files += sorted(f for f in path.rglob("*") if f.suffix.lower() in (".csv", ".xlsx", ".xls")) \
            if path.is_dir() else [path]
    for f in files:
        try:
            res = importers.import_file(conn, f, args.broker, args.account)
        except ValueError as e:
            print(f"! {e}")
            continue
        print(f"{f.name}: parsed {res['parsed']}, new {res['inserted']}, "
              f"range {res['from']} -> {res['to']}, skipped {len(res['skipped'])}")
        for n, reason in res["skipped"][:5]:
            print(f"    row {n}: {reason}")
    _rebuild(conn)


def cmd_login(args) -> None:
    if args.broker == "dhan":
        from .brokers import dhan
        dhan.save_access_token(input("Paste Dhan access token (web.dhan.co -> Access DhanHQ APIs): "))
        print("Saved.")
        return
    mod = __import__(f"tradejournal.brokers.{args.broker}", fromlist=["x"])
    print("1. Open this URL and log in:\n\n   " + mod.login_url() + "\n")
    print("2. After login you'll be redirected (the page may not load - that's fine).")
    value = input("3. Paste the full redirect URL here: ")
    mod.complete_login(value)
    print(f"Logged in to {args.broker}. Token saved in data/tokens.json (expires daily).")


def cmd_sync(args) -> None:
    conn = db.connect()
    brokers = BROKERS if args.broker == "all" else (args.broker,)
    end = args.to or date.today()
    for broker in brokers:
        try:
            if broker == "zerodha":
                from .brokers import zerodha
                fills, skipped = zerodha.fetch_fills(args.account)
                source, start = "zerodha_api", date.today()
            elif broker == "dhan":
                from .brokers import dhan
                start = args.start or _last_synced(conn, broker, "dhan_api") or date(2021, 1, 1)
                fills, skipped = dhan.fetch_fills(start, end, args.account)
                source = "dhan_api"
            else:
                from .brokers import upstox
                default_start = date(date.today().year - 3, 4, 1)
                start = args.start or _last_synced(conn, broker, "upstox_api") or default_start
                fills, skipped = upstox.fetch_fills(start, end, args.account)
                today_fills, today_skipped = upstox.fetch_today(args.account)
                fills, skipped, source = fills + today_fills, skipped + today_skipped, "upstox_api"
        except AuthError as e:
            print(f"{broker}: {e}")
            continue
        inserted = db.insert_fills(conn, fills)
        db.log_sync(conn, broker, source, start.isoformat(), end.isoformat(), len(fills), inserted)
        print(f"{broker}: fetched {len(fills)} fills, {inserted} new, {len(skipped)} skipped")
    _rebuild(conn)


def _last_synced(conn, broker: str, source: str) -> date | None:
    row = conn.execute("SELECT MAX(to_date) FROM sync_log WHERE broker=? AND source=?", (broker, source)).fetchone()
    return _date(row[0]) - timedelta(days=3) if row and row[0] else None


def cmd_rebuild(args) -> None:
    _rebuild(db.connect())


def cmd_status(args) -> None:
    conn = db.connect()
    cov = analytics.coverage(conn)
    if cov.empty:
        print("No fills yet. Start with: python -m tradejournal import zerodha <tradebook.csv>")
        return
    print("Fills per broker / year (check for missing years):\n")
    print(cov.to_string(index=False))
    pending = conn.execute("SELECT COUNT(*) FROM trades WHERE status='NEEDS_SETTLEMENT'").fetchone()[0]
    if pending:
        print(f"\n{pending} expired futures need a settlement price (dashboard -> Settlements).")


def cmd_stats(args) -> None:
    conn = db.connect()
    df = analytics.load_trades(conn)
    if args.broker:
        df = df[df["broker"] == args.broker]
    s = analytics.summary(df)
    if not s.get("trades"):
        print("No closed trades yet.")
        return
    for k, v in s.items():
        print(f"{k:>16}: {v:,.2f}" if isinstance(v, float) else f"{k:>16}: {v}")
    print("\nBy financial year:\n")
    print(analytics.breakdown(df, "fy").round(2).to_string(index=False))


def cmd_dashboard(args) -> None:
    app = Path(__file__).with_name("dashboard.py")
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port)], check=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="tradejournal", description="Personal trading journal")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="import tradebook CSV/Excel files (or folders of them)")
    p.add_argument("broker", choices=BROKERS + ("other",))
    p.add_argument("files", nargs="+")
    p.add_argument("--account", default="default", help="label to separate multiple accounts at one broker")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("login", help="log in to a broker API (daily for Zerodha/Upstox)")
    p.add_argument("broker", choices=BROKERS)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("sync", help="pull trades from broker APIs")
    p.add_argument("broker", choices=BROKERS + ("all",))
    p.add_argument("--from", dest="start", type=_date, help="YYYY-MM-DD (Dhan/Upstox)")
    p.add_argument("--to", type=_date, help="YYYY-MM-DD (default today)")
    p.add_argument("--account", default="default")
    p.set_defaults(func=cmd_sync)

    sub.add_parser("rebuild", help="recompute trades from fills").set_defaults(func=cmd_rebuild)
    sub.add_parser("status", help="show history coverage per broker/year").set_defaults(func=cmd_status)

    p = sub.add_parser("stats", help="print performance summary")
    p.add_argument("--broker", choices=BROKERS + ("other",))
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("dashboard", help="open the web dashboard")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(func=cmd_dashboard)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
