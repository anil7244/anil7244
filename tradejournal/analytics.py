"""Performance statistics over the trades table."""
from __future__ import annotations

import sqlite3

import pandas as pd

REALIZED = ("CLOSED", "EXPIRED")


def load_trades(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT t.*, j.strategy, j.setup, j.tags, j.mistakes, j.emotion, j.rating, j.planned_risk, j.notes, "
        "j.screenshots FROM trades t LEFT JOIN journal j USING (trade_uid) ORDER BY t.open_ts", conn)
    for col in ("open_ts", "close_ts"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    df["close_date"] = df["close_ts"].dt.date
    df["r_multiple"] = df["net_pnl"] / df["planned_risk"].where(df["planned_risk"] > 0)
    return df


def load_fills(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM fills ORDER BY ts", conn)


def realized(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["status"].isin(REALIZED)].sort_values("close_ts")


def summary(df: pd.DataFrame) -> dict:
    r = realized(df)
    if r.empty:
        return {"trades": 0}
    wins, losses = r[r["net_pnl"] > 0], r[r["net_pnl"] <= 0]
    equity = r["net_pnl"].cumsum()
    drawdown = equity - equity.cummax().clip(lower=0)
    daily = r.groupby("close_date")["net_pnl"].sum()
    gross_loss = -losses["net_pnl"].sum()
    return {
        "trades": len(r),
        "net_pnl": r["net_pnl"].sum(),
        "gross_pnl": r["gross_pnl"].sum(),
        "charges": r["charges"].sum(),
        "win_rate": len(wins) / len(r) * 100,
        "avg_win": wins["net_pnl"].mean() if len(wins) else 0.0,
        "avg_loss": losses["net_pnl"].mean() if len(losses) else 0.0,
        "largest_win": r["net_pnl"].max(),
        "largest_loss": r["net_pnl"].min(),
        "profit_factor": wins["net_pnl"].sum() / gross_loss if gross_loss > 0 else float("inf"),
        "expectancy": r["net_pnl"].mean(),
        "max_drawdown": drawdown.min(),
        "trading_days": len(daily),
        "green_days": int((daily > 0).sum()),
        "best_day": daily.max(),
        "worst_day": daily.min(),
        "avg_holding_min": r["holding_minutes"].median(),
    }


def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    r = realized(df)
    daily = r.groupby("close_date")["net_pnl"].sum().reset_index()
    daily["equity"] = daily["net_pnl"].cumsum()
    daily["drawdown"] = daily["equity"] - daily["equity"].cummax().clip(lower=0)
    return daily


def breakdown(df: pd.DataFrame, by: str) -> pd.DataFrame:
    r = realized(df).copy()
    if by == "month":
        r[by] = r["close_ts"].dt.to_period("M").astype(str)
    elif by == "year":
        r[by] = r["close_ts"].dt.year
    elif by == "fy":
        ts = r["close_ts"]
        start = ts.dt.year - (ts.dt.month < 4).astype(int)
        r[by] = "FY" + start.astype(str) + "-" + (start + 1).astype(str).str[-2:]
    elif by == "weekday":
        r[by] = r["open_ts"].dt.day_name()
    elif by == "entry_hour":
        r[by] = r["open_ts"].dt.hour
    elif by == "holding":
        r[by] = pd.cut(r["holding_minutes"], [-1, 5, 15, 60, 375, 1440 * 7, float("inf")],
                       labels=["<5m", "5-15m", "15m-1h", "1h-day", "1d-1w", ">1w"])
    elif by in ("tags", "mistakes"):
        r[by] = r[by].fillna("").str.split(",")
        r = r.explode(by)
        r[by] = r[by].str.strip()
        r = r[r[by] != ""]
    g = r.groupby(by, observed=True)["net_pnl"]
    out = pd.DataFrame({
        "trades": g.size(), "net_pnl": g.sum(), "win_rate": g.apply(lambda s: (s > 0).mean() * 100),
        "avg_pnl": g.mean(),
    })
    return out.reset_index()


def coverage(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fill counts per broker per year so you can spot missing history."""
    return pd.read_sql_query(
        "SELECT broker, account, substr(trade_date,1,4) AS year, source, COUNT(*) AS fills, "
        "MIN(trade_date) AS first, MAX(trade_date) AS last FROM fills "
        "GROUP BY broker, account, year, source ORDER BY broker, account, year", conn)
