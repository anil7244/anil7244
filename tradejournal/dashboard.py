"""Streamlit dashboard. Run with: python -m tradejournal dashboard"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradejournal import analytics, config, db, importers, matching  # noqa: E402
from tradejournal.brokers import AuthError  # noqa: E402

# Profit/loss is a diverging encoding: blue = profit, red = loss (colour-blind safe).
PROFIT, LOSS, NEUTRAL = "#2a78d6", "#e34948", "#f0efec"
DIVERGING = [[0, LOSS], [0.5, NEUTRAL], [1, PROFIT]]
EMOTIONS = ["", "Calm", "Confident", "FOMO", "Fear", "Greed", "Revenge", "Bored", "Tired", "Impatient"]

st.set_page_config(page_title="Trading Journal", layout="wide")


conn = db.connect()   # cheap; a fresh connection per rerun avoids SQLite thread issues


def inr(x: float) -> str:
    if pd.isna(x):
        return "-"
    return f"-₹{abs(x):,.0f}" if x < 0 else f"₹{x:,.0f}"


def pnl_bar(df: pd.DataFrame, x: str, title: str, horizontal: bool = False):
    df = df.copy()
    df["colour"] = df["net_pnl"].apply(lambda v: "Profit" if v > 0 else "Loss")
    fig = px.bar(df, x="net_pnl" if horizontal else x, y=x if horizontal else "net_pnl",
                 color="colour", color_discrete_map={"Profit": PROFIT, "Loss": LOSS},
                 orientation="h" if horizontal else "v", title=title,
                 hover_data={"trades": True, "win_rate": ":.1f", "colour": False})
    fig.update_layout(showlegend=False, bargap=0.25, margin=dict(t=40, b=10, l=10, r=10),
                      xaxis_title=None, yaxis_title=None)
    fig.update_traces(marker_line_width=0)
    # Profit and loss bars are separate traces; pin the category order to the data's order.
    order = dict(type="category", categoryorder="array", categoryarray=list(df[x]))
    if horizontal:
        fig.update_yaxes(**order)
    else:
        fig.update_xaxes(**order)
    return fig


def reload_trades() -> pd.DataFrame:
    return analytics.load_trades(conn)


trades = reload_trades()

# ---------------------------------------------------------------- sidebar filters
st.sidebar.title("Trading Journal")
if trades.empty:
    st.sidebar.info("No trades yet. Use the Import & Sync tab.")
    filtered = trades
else:
    brokers = sorted(trades["broker"].unique())
    sel_brokers = st.sidebar.multiselect("Broker", brokers, default=brokers)
    first = trades["open_ts"].min().date()
    last = max(trades["close_ts"].max().date() if trades["close_ts"].notna().any() else first, date.today())
    period = st.sidebar.date_input("Period (by close date)", (first, last), min_value=first, max_value=last)
    kinds = st.sidebar.multiselect("Instrument", ["CE", "PE", "FUT", "EQ"], default=["CE", "PE", "FUT", "EQ"])
    underlyings = st.sidebar.multiselect("Underlying", sorted(trades["underlying"].unique()))
    direction = st.sidebar.radio("Direction", ["All", "LONG", "SHORT"], horizontal=True)

    mask = trades["broker"].isin(sel_brokers) & trades["kind"].isin(kinds)
    if isinstance(period, tuple) and len(period) == 2:
        ref = trades["close_ts"].fillna(trades["open_ts"]).dt.date
        mask &= (ref >= period[0]) & (ref <= period[1])
    if underlyings:
        mask &= trades["underlying"].isin(underlyings)
    if direction != "All":
        mask &= trades["direction"] == direction
    filtered = trades[mask]

tab_over, tab_cal, tab_trades, tab_analysis, tab_import, tab_settle = st.tabs(
    ["Overview", "Calendar", "Trades & Journal", "Analysis", "Import & Sync", "Settlements"])

# ---------------------------------------------------------------- overview
with tab_over:
    s = analytics.summary(filtered)
    if not s.get("trades"):
        st.info("No closed trades in this selection.")
    else:
        c = st.columns(6)
        c[0].metric("Net P&L", inr(s["net_pnl"]), help="After brokerage, taxes and charges")
        c[1].metric("Trades", f"{s['trades']:,}")
        c[2].metric("Win rate", f"{s['win_rate']:.1f}%")
        c[3].metric("Profit factor", "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}")
        c[4].metric("Max drawdown", inr(s["max_drawdown"]))
        c[5].metric("Charges paid", inr(s["charges"]))
        c = st.columns(6)
        c[0].metric("Avg win", inr(s["avg_win"]))
        c[1].metric("Avg loss", inr(s["avg_loss"]))
        c[2].metric("Expectancy / trade", inr(s["expectancy"]))
        c[3].metric("Green days", f"{s['green_days'] / s['trading_days'] * 100:.0f}%",
                    help=f"{s['green_days']} of {s['trading_days']} trading days were profitable")
        c[4].metric("Best day", inr(s["best_day"]))
        c[5].metric("Worst day", inr(s["worst_day"]))

        eq = analytics.equity_curve(filtered)
        fig = go.Figure()
        fig.add_scatter(x=eq["close_date"], y=eq["equity"], mode="lines", name="Cumulative net P&L",
                        line=dict(color=PROFIT, width=2), hovertemplate="%{x}<br>₹%{y:,.0f}<extra></extra>")
        fig.update_layout(title="Equity curve", hovermode="x unified", margin=dict(t=40, b=10, l=10, r=10))
        st.plotly_chart(fig, width="stretch")

        dd = go.Figure()
        dd.add_scatter(x=eq["close_date"], y=eq["drawdown"], fill="tozeroy", mode="lines", name="Drawdown",
                       line=dict(color=LOSS, width=2), hovertemplate="%{x}<br>₹%{y:,.0f}<extra></extra>")
        dd.update_layout(title="Drawdown from peak", height=250, margin=dict(t=40, b=10, l=10, r=10))
        st.plotly_chart(dd, width="stretch")

        col1, col2 = st.columns(2)
        col1.plotly_chart(pnl_bar(analytics.breakdown(filtered, "month"), "month", "Monthly net P&L"),
                          width="stretch")
        fy = analytics.breakdown(filtered, "fy")
        col2.plotly_chart(pnl_bar(fy, "fy", "Net P&L by financial year"), width="stretch")
        st.dataframe(fy.style.format({"net_pnl": inr, "avg_pnl": inr, "win_rate": "{:.1f}%"}),
                     hide_index=True, width="stretch")

        open_pos = filtered[filtered["status"].isin(["OPEN", "NEEDS_SETTLEMENT"])]
        if not open_pos.empty:
            st.subheader("Open positions")
            st.dataframe(open_pos[["broker", "symbol", "direction", "qty", "entry_avg", "open_ts", "status"]],
                         hide_index=True, width="stretch")

# ---------------------------------------------------------------- calendar
with tab_cal:
    r = analytics.realized(filtered)
    if r.empty:
        st.info("No closed trades in this selection.")
    else:
        daily = r.groupby("close_date").agg(net_pnl=("net_pnl", "sum"), trades=("net_pnl", "size")).reset_index()
        daily["close_date"] = pd.to_datetime(daily["close_date"])
        years = sorted(daily["close_date"].dt.year.unique(), reverse=True)
        year = st.selectbox("Year", years)
        d = daily[daily["close_date"].dt.year == year].copy()
        iso = d["close_date"].dt.isocalendar()
        d["week"] = iso.week.astype(int)
        d.loc[(d["close_date"].dt.month == 1) & (d["week"] > 50), "week"] = 0
        d.loc[(d["close_date"].dt.month == 12) & (d["week"] == 1), "week"] = 53
        d["weekday"] = d["close_date"].dt.day_name().str[:3]
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = [x for x in days if x in set(d["weekday"]) or x in days[:5]]
        weeks = list(range(int(d["week"].min()), int(d["week"].max()) + 1))
        z = d.pivot_table(index="weekday", columns="week", values="net_pnl", aggfunc="sum").reindex(
            index=days, columns=weeks)
        label = d.assign(txt=d["close_date"].dt.strftime("%d %b %Y") + " · " + d["trades"].astype(str) + " trades")
        txt = label.pivot_table(index="weekday", columns="week", values="txt", aggfunc="first").reindex(
            index=days, columns=weeks)
        lim = max(float(abs(d["net_pnl"]).max()), 1.0)
        fig = go.Figure(go.Heatmap(
            x=weeks, y=days, z=z.values.tolist(), customdata=txt.fillna("").values.tolist(),
            colorscale=DIVERGING, zmid=0, zmin=-lim, zmax=lim, xgap=2, ygap=2, hoverongaps=False,
            hovertemplate="%{customdata}<br>₹%{z:,.0f}<extra></extra>", colorbar=dict(title="₹")))
        fig.update_yaxes(autorange="reversed", type="category")
        fig.update_layout(title=f"Daily net P&L {year}", height=320, xaxis_title="Week of year",
                          margin=dict(t=40, b=10, l=10, r=10))
        st.plotly_chart(fig, width="stretch")

        r2 = r.copy()
        r2["year"] = r2["close_ts"].dt.year
        r2["month"] = r2["close_ts"].dt.strftime("%m-%b")
        matrix = r2.pivot_table(index="year", columns="month", values="net_pnl", aggfunc="sum").sort_index(ascending=False)
        matrix["Total"] = matrix.sum(axis=1)
        st.subheader("Month × year net P&L")
        peak = max(abs(matrix.drop(columns="Total")).max().max(), 1)

        def tint(v):
            if pd.isna(v) or v == 0:
                return ""
            rgb = "42,120,214" if v > 0 else "227,73,72"
            return f"background-color: rgba({rgb},{0.15 + 0.6 * min(abs(v) / peak, 1):.2f})"

        styles = matrix.map(tint)
        styles["Total"] = "font-weight: 600"
        shown = matrix.map(lambda v: "" if pd.isna(v) else inr(v))   # text, so empty months stay blank
        st.dataframe(shown.style.apply(lambda _: styles, axis=None), width="stretch")

# ---------------------------------------------------------------- trades & journal
with tab_trades:
    if filtered.empty:
        st.info("No trades in this selection.")
    else:
        view = filtered.sort_values("open_ts", ascending=False).reset_index(drop=True)
        cols = ["open_ts", "close_ts", "broker", "symbol", "direction", "qty", "entry_avg", "exit_avg",
                "net_pnl", "charges", "status", "strategy", "tags", "rating"]
        event = st.dataframe(
            view[cols], hide_index=True, width="stretch", height=420, key="trades_table",
            on_select="rerun", selection_mode="single-row",
            column_config={"net_pnl": st.column_config.NumberColumn("Net P&L", format="₹%.0f"),
                           "charges": st.column_config.NumberColumn(format="₹%.0f"),
                           "open_ts": st.column_config.DatetimeColumn("Opened", format="DD-MM-YY HH:mm"),
                           "close_ts": st.column_config.DatetimeColumn("Closed", format="DD-MM-YY HH:mm")})
        st.download_button("Download trades CSV", view.to_csv(index=False), "trades.csv", "text/csv")
        rows = event.selection.rows if event and event.selection else []
        if not rows:
            st.caption("Select a trade to journal it.")
        else:
            t = view.iloc[rows[0]]
            st.subheader(f"{t['symbol']} · {t['direction']} · {inr(t['net_pnl'])}")
            st.caption(f"{t['broker']} · {t['open_ts']} → {t['close_ts']} · gross {inr(t['gross_pnl'])}, "
                       f"charges {inr(t['charges'])}{' (estimated)' if t['charges_estimated'] else ''}")
            fills = pd.read_sql_query(
                "SELECT f.ts, f.side, tf.qty, f.price, tf.role, f.trade_id, f.order_id, f.source FROM trade_fills tf "
                "JOIN fills f ON f.id = tf.fill_id WHERE tf.trade_uid = ? ORDER BY f.ts", conn,
                params=(t["trade_uid"],))
            st.dataframe(fills, hide_index=True, width="stretch")

            def val(k, default=""):
                return t[k] if pd.notna(t[k]) else default

            with st.form(f"journal-{t['trade_uid']}"):
                c1, c2, c3 = st.columns(3)
                strategy = c1.text_input("Strategy", val("strategy"), placeholder="e.g. ORB, short straddle")
                setup = c2.text_input("Setup / reason", val("setup"))
                emotion = c3.selectbox("Emotion", EMOTIONS,
                                       index=EMOTIONS.index(val("emotion")) if val("emotion") in EMOTIONS else 0)
                c1, c2, c3 = st.columns(3)
                tags = c1.text_input("Tags (comma separated)", val("tags"))
                mistakes = c2.text_input("Mistakes (comma separated)", val("mistakes"),
                                         placeholder="e.g. no SL, early exit, overtrade")
                planned_risk = c3.number_input("Planned risk ₹ (for R-multiple)", min_value=0.0,
                                               value=float(val("planned_risk", 0.0)), step=100.0)
                rating = st.slider("Execution rating", 0, 5, int(val("rating", 0)))
                notes = st.text_area("Notes / lessons", val("notes"), height=120)
                shots = st.file_uploader("Chart screenshots", type=["png", "jpg", "jpeg", "webp"],
                                         accept_multiple_files=True)
                if st.form_submit_button("Save journal"):
                    names = [n for n in str(val("screenshots")).split("\n") if n]
                    for f in shots or []:
                        name = f"{t['trade_uid']}_{f.name}"
                        (config.screenshot_dir() / name).write_bytes(f.getvalue())
                        names.append(name)
                    conn.execute(
                        "INSERT INTO journal (trade_uid, strategy, setup, tags, mistakes, emotion, rating, planned_risk,"
                        " notes, screenshots, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                        "ON CONFLICT(trade_uid) DO UPDATE SET strategy=excluded.strategy, setup=excluded.setup, "
                        "tags=excluded.tags, mistakes=excluded.mistakes, emotion=excluded.emotion, "
                        "rating=excluded.rating, planned_risk=excluded.planned_risk, notes=excluded.notes, "
                        "screenshots=excluded.screenshots, updated_at=excluded.updated_at",
                        (t["trade_uid"], strategy, setup, tags, mistakes, emotion, rating,
                         planned_risk or None, notes, "\n".join(dict.fromkeys(names))))
                    conn.commit()
                    st.success("Saved")
                    st.rerun()
            for name in str(val("screenshots")).split("\n"):
                path = config.screenshot_dir() / name
                if name and path.exists():
                    st.image(str(path), caption=name, width="stretch")

# ---------------------------------------------------------------- analysis
with tab_analysis:
    if analytics.realized(filtered).empty:
        st.info("No closed trades in this selection.")
    else:
        f2 = filtered.copy()
        f2["instrument"] = f2["kind"] + " " + f2["direction"].str.lower()
        dims = [("underlying", "By underlying"), ("instrument", "By instrument & direction"),
                ("weekday", "By weekday (entry)"), ("entry_hour", "By entry hour"),
                ("holding", "By holding time"), ("broker", "By broker"),
                ("strategy", "By strategy"), ("tags", "By tag"), ("mistakes", "P&L by mistake")]
        cols = st.columns(2)
        for i, (dim, title) in enumerate(dims):
            b = analytics.breakdown(f2, dim)
            if b.empty:
                continue
            if dim == "underlying":
                b = b.reindex(b["net_pnl"].abs().sort_values(ascending=False).index).head(15)
            if dim == "weekday":
                order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                b = b.set_index(dim).reindex([d for d in order if d in set(b[dim])]).reset_index()
            b[dim] = b[dim].astype(str)
            cols[i % 2].plotly_chart(pnl_bar(b, dim, title, horizontal=dim in ("underlying", "strategy", "tags",
                                                                                  "mistakes", "instrument")),
                                     width="stretch")
        rr = filtered["r_multiple"].dropna()
        if not rr.empty:
            hist = px.histogram(rr, nbins=30, title="R-multiple distribution", color_discrete_sequence=[PROFIT])
            hist.update_layout(showlegend=False, xaxis_title="R-multiple (net P&L ÷ planned risk)",
                               yaxis_title="Trades", bargap=0.05)
            st.plotly_chart(hist, width="stretch")

# ---------------------------------------------------------------- import & sync
with tab_import:
    st.subheader("Import tradebook files")
    st.markdown(
        "- **Zerodha**: console.zerodha.com → Reports → Tradebook → segment *Equity & F&O*, "
        "download year by year from 2016.\n"
        "- **Upstox**: upstox.com → Reports → Trade history / Tradebook (download per financial year).\n"
        "- **Dhan**: web.dhan.co → Statements → Trade history (or use API sync below).")
    c1, c2 = st.columns(2)
    broker = c1.selectbox("Broker", ["zerodha", "dhan", "upstox", "other"])
    account = c2.text_input("Account label", "default")
    uploads = st.file_uploader("CSV / Excel files", type=["csv", "xlsx", "xls"], accept_multiple_files=True)
    if st.button("Import", disabled=not uploads):
        for up in uploads:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / up.name
                path.write_bytes(up.getvalue())
                try:
                    res = importers.import_file(conn, path, broker, account)
                    st.write(f"**{up.name}**: {res['parsed']} fills parsed, {res['inserted']} new, "
                             f"{res['from']} → {res['to']}, {len(res['skipped'])} rows skipped")
                    if res["skipped"]:
                        st.caption("; ".join(f"row {n}: {why}" for n, why in res["skipped"][:5]))
                except ValueError as e:
                    st.error(str(e))
        st.write(matching.rebuild(conn))
        st.rerun()

    st.subheader("API sync")
    st.caption("Zerodha and Upstox tokens expire daily; Dhan tokens last 24 hours. Credentials go in `.env`.")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Zerodha** (today's trades only)")
        try:
            from tradejournal.brokers import zerodha
            st.link_button("1. Login to Kite", zerodha.login_url())
            redirect = st.text_input("2. Paste redirect URL", key="z_redirect")
            if st.button("Connect & sync Zerodha"):
                if redirect:
                    zerodha.complete_login(redirect)
                fills, _ = zerodha.fetch_fills(account)
                st.success(f"{db.insert_fills(conn, fills)} new fills")
                matching.rebuild(conn)
        except AuthError as e:
            st.warning(str(e))
    with c2:
        st.markdown("**Dhan** (full history since 2021)")
        try:
            from tradejournal.brokers import dhan
            token = st.text_input("Access token (optional if in .env)", type="password", key="d_token")
            start = st.date_input("From", date(2021, 1, 1), key="d_from")
            if st.button("Sync Dhan"):
                if token:
                    dhan.save_access_token(token)
                with st.spinner("Fetching…"):
                    fills, _ = dhan.fetch_fills(start, date.today(), account, progress=lambda *_: None)
                st.success(f"{db.insert_fills(conn, fills)} new fills")
                matching.rebuild(conn)
        except AuthError as e:
            st.warning(str(e))
    with c3:
        st.markdown("**Upstox** (≈ last 3 financial years)")
        try:
            from tradejournal.brokers import upstox
            st.link_button("1. Login to Upstox", upstox.login_url())
            redirect = st.text_input("2. Paste redirect URL", key="u_redirect")
            start = st.date_input("From", date(date.today().year - 3, 4, 1), key="u_from")
            if st.button("Connect & sync Upstox"):
                if redirect:
                    upstox.complete_login(redirect)
                with st.spinner("Fetching…"):
                    fills, _ = upstox.fetch_fills(start, date.today(), account, progress=lambda *_: None)
                    today_fills, _ = upstox.fetch_today(account)
                st.success(f"{db.insert_fills(conn, fills + today_fills)} new fills")
                matching.rebuild(conn)
        except AuthError as e:
            st.warning(str(e))

    st.subheader("History coverage")
    st.caption("Fills per broker per year. A missing year means a tradebook still needs importing.")
    st.dataframe(analytics.coverage(conn), hide_index=True, width="stretch")

# ---------------------------------------------------------------- settlements
with tab_settle:
    st.markdown("Positions still open after expiry. Options default to **0** (expired worthless); "
                "enter the final settlement price for in-the-money options and for futures.")
    pending = trades[trades["status"].isin(["EXPIRED", "NEEDS_SETTLEMENT"])]
    if pending.empty:
        st.info("Nothing to settle.")
    else:
        current = {r["instrument_key"]: r["price"] for r in conn.execute("SELECT * FROM settlements")}
        table = pending[["instrument_key", "symbol", "broker", "direction", "qty", "entry_avg", "status"]].copy()
        table["settlement_price"] = table["instrument_key"].map(current)
        edited = st.data_editor(table, hide_index=True, width="stretch",
                                disabled=[c for c in table.columns if c != "settlement_price"])
        if st.button("Save settlement prices"):
            for _, row in edited.dropna(subset=["settlement_price"]).iterrows():
                conn.execute("INSERT INTO settlements (instrument_key, price) VALUES (?, ?) "
                             "ON CONFLICT(instrument_key) DO UPDATE SET price=excluded.price",
                             (row["instrument_key"], float(row["settlement_price"])))
            conn.commit()
            st.write(matching.rebuild(conn))
            st.rerun()
