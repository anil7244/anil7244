# Trading Journal: Zerodha + Dhan + Upstox

A personal trading journal built for F&O traders. It brings every trade from all three brokers
into one local database, from 2016 to today. It pairs your buys and sells into complete trades
and works out net P&L after charges. A dashboard shows your stats, and you can add strategy,
mistakes, notes and chart screenshots to each trade.

Everything runs on your own computer. Your data (`data/`) and API keys (`.env`) never leave it
and are git-ignored.

## How to get the full history (2016 to today)

No broker API returns ten years of trades, so history is loaded once from tradebook files.
After that, the APIs keep the journal up to date.

| Broker | Old history (one-time) | Ongoing sync |
|---|---|---|
| **Zerodha** | [Console](https://console.zerodha.com) → Reports → **Tradebook** → segment *Equity* and *F&O* (and *Commodity* if used). Download **one financial year at a time** from FY2016-17 to today, CSV. | Kite API only returns **today's** trades, so run `sync zerodha` after market close on each trading day (or import Console again now and then). |
| **Dhan** | Not needed. The API returns the full history since you opened the account. | `sync dhan` (fetches everything since the last sync). |
| **Upstox** | upstox.com → Reports → **Trade history / Tradebook** for years older than ~3 FYs (Excel/CSV). For very old (RKSV-era) years, ask Upstox support for tradebooks or contract notes. | `sync upstox` (API covers ~3 financial years). |

The journal never double counts. Re-importing the same file adds nothing. When a CSV and the API
both cover the same day for a broker, only one source is used for that day.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in API keys (only needed for API sync)
```

## Usage

```bash
# 1. Load history from tradebook files (a folder works too)
python -m tradejournal import zerodha ~/Downloads/zerodha-tradebooks/
python -m tradejournal import upstox  ~/Downloads/upstox-tradebook-FY2019-20.xlsx

# 2. Check which years are covered for each broker
python -m tradejournal status

# 3. Sync from the APIs
python -m tradejournal login zerodha      # daily: opens Kite login, paste the redirect URL back
python -m tradejournal login upstox       # daily
python -m tradejournal login dhan         # paste a token from web.dhan.co (valid 24h)
python -m tradejournal sync dhan --from 2021-01-01
python -m tradejournal sync all           # daily routine after market close

# 4. Look at the results
python -m tradejournal stats
python -m tradejournal dashboard          # opens http://localhost:8501
```

Have two accounts at one broker (e.g. family accounts)? Add `--account wife` (or any label) to
`import`/`sync`.

## Dashboard

- **Overview**: net P&L after charges, win rate, profit factor, expectancy, max drawdown,
  equity curve, drawdown, monthly P&L and P&L by financial year, open positions.
- **Calendar**: daily P&L heatmap for each year, plus a month × year P&L grid.
- **Trades & Journal**: every trade with its fills. Select one to record strategy, setup,
  emotion, tags, mistakes, planned risk (gives an R-multiple), rating, notes and screenshots.
  Export the list as CSV.
- **Analysis**: P&L by underlying, CE/PE/FUT × long/short, weekday, entry hour, holding time,
  broker, strategy, tag and mistake.
- **Import & Sync**: upload files, log in and sync from the browser, check history coverage.
- **Settlements**: positions held through expiry. Options default to 0 (expired worthless).
  Enter the settlement price for in-the-money options and for futures.

Blue means profit and red means loss throughout. This palette is safe for colour-blind readers.

## How trades are built

- One **trade** = one position cycle on one contract, from flat back to flat. Adding to or
  partly exiting a position stays in the same trade. A fill that flips you from long to short is
  split across two trades.
- **Charges**: Dhan's API gives actual charges, which are used as-is. Everything else is
  estimated from dated rate tables in `tradejournal/charges.py`: brokerage of ₹20 per order,
  STT (including the Oct 2024 and Apr 2026 hikes), exchange fees, SEBI fee, stamp duty and GST.
  Trades with estimated charges are marked in the dashboard. Change `BROKERAGE` there if your
  plan was different.
- Your journal notes are keyed to a stable trade ID, so re-importing or re-syncing never
  erases them.

## If a file doesn't import

The importer finds the header row itself and matches columns by name. If a broker export uses
column names it doesn't know, you'll see
`could not find a header row ...`. Add the names to `ALIASES` in
`tradejournal/importers/__init__.py`, or share the header row and the first data row (with
personal details removed) to get it supported.

## Tests

```bash
python -m pytest -q
```

## Known limitations

- Zerodha's API has no endpoint for past trades. Days you miss syncing must come from a Console
  tradebook (re-importing is safe).
- Physical delivery of stock F&O (since 2019), and the STT on exercised options, are not modelled.
- Commodity and currency trades are imported and matched, but their charges are not estimated.
- No live prices, so open positions show no unrealised P&L.
