#!/usr/bin/env python3
"""
Market Genie — Parameter Backtester
=====================================
Fetches your paper trades from Alpaca, re-simulates each one with your
current exit parameters AND a test set, then shows a side-by-side P&L comparison.

HOW TO USE
----------
1. Edit the TEST block below (change whatever you want to test)
2. Open a terminal in this folder and run:
       python backtest.py
3. Results are printed to the terminal and saved to backtest_results.csv

DATA NOTES
----------
- 1-minute bars available for the last 7 days (yfinance limit)
- Older trades use 5-minute bars (slightly less accurate)
- Simulation is conservative: within any bar, stop is checked before target
"""

import os, sys
from datetime import datetime, timedelta, time as dtime, date as date_type
import pytz, requests, pandas as pd
import yfinance as yf

ET = pytz.timezone("America/New_York")

# ── Load .env file if present ─────────────────────────────────────────────────
def _load_env():
    for path in [".env", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]:
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return
_load_env()

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", os.getenv("ALPACA_API_SECRET", ""))
ALPACA_URL    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
HDR           = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

if not ALPACA_KEY or not ALPACA_SECRET:
    print("❌  Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env or environment")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  PARAMETERS  —  edit TEST to compare against CURRENT
# ══════════════════════════════════════════════════════════════════════════════
CURRENT = dict(
    stop_pct          = 0.0075,   # 0.75% stop loss
    target_pct        = 0.015,    # 1.5% base take-profit target
    loser_exit_mins   = 20,       # cut losing/flat positions at 20 min
    winner_max_mins   = 40,       # hard max hold time (40 min)
    flat_exempt_pct   = -0.15,    # positions better than this ride to 40 min
    early_cut_pct     = -0.35,    # early cut-and-run threshold (5–10 min)
    early_cut_min_age = 5,        # earliest the early cut can fire (minutes)
    early_cut_max_age = 10,       # after this age, normal loser exit takes over
)

TEST = {
    **CURRENT,
    # ↓↓↓  Change anything here to test it  ↓↓↓
    "early_cut_pct":  -0.25,   # tighter early cut (−0.25% vs −0.35%)
    "stop_pct":        0.006,   # tighter stop (0.6% vs 0.75%)
}

LOOKBACK_DAYS = 30   # how many days back to pull trades
# ══════════════════════════════════════════════════════════════════════════════


# ── Step 1: Fetch orders from Alpaca ─────────────────────────────────────────
def fetch_orders():
    since  = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    params = {"status": "filled", "limit": 500, "after": since, "direction": "asc"}
    r = requests.get(f"{ALPACA_URL}/v2/orders", headers=HDR, params=params, timeout=15)
    if r.status_code != 200:
        print(f"❌  Alpaca orders API failed: {r.status_code} — {r.text[:200]}")
        return []
    return r.json()


# ── Step 2: Identify entry orders ────────────────────────────────────────────
def parse_entries(orders):
    """
    Entry orders are standalone limit fills (not OCO legs, stops, or market closes).

    The Market Genie flow:
      Phase 1/2: limit entry order  (type=limit, no order_class, side=buy/sell)
      Phase 3:   OCO bracket order  (order_class=oco, contains TP + stop legs)
      Time exit: market close order (type=market)

    Strategy: collect all OCO leg IDs first, then exclude them.
    This correctly handles both long entries (buy) and short entries (sell).
    """
    # Collect OCO leg IDs to exclude (take-profit and stop legs)
    leg_ids = set()
    for o in orders:
        if (o.get("order_class") or "") == "oco":
            for leg in (o.get("legs") or []):
                if leg.get("id"):
                    leg_ids.add(leg["id"])

    entries = []
    for o in orders:
        oid   = o.get("id", "")
        oc    = o.get("order_class", "") or ""
        otype = o.get("type",        "") or ""

        if oc == "oco":                                        continue  # bracket parent
        if oid in leg_ids:                                     continue  # TP or stop leg
        if otype in ("stop", "stop_limit", "trailing_stop"):   continue  # stop exits
        if otype == "market":                                   continue  # time/EOD closes

        status = o.get("status", "")
        side   = o.get("side",   "")
        qty    = float(o.get("filled_qty",       0) or 0)
        price  = float(o.get("filled_avg_price", 0) or 0)
        sym    = o.get("symbol",    "")
        fat    = o.get("filled_at", "")

        if status != "filled" or qty <= 0 or price <= 0 or not fat or not sym:
            continue

        try:
            ts = datetime.fromisoformat(fat.replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue

        # Only regular market hours entries (9:30–16:00 ET, weekdays)
        if not (ts.weekday() <= 4 and dtime(9, 30) <= ts.time() < dtime(16, 0)):
            continue

        entries.append({
            "sym":       sym,
            "direction": "bull" if side == "buy" else "bear",
            "price":     round(price, 4),
            "qty":       int(qty),
            "ts":        ts,
            "date":      ts.date(),
        })

    # Deduplicate same sym+dir+date — Phase 2 may create a second fill;
    # keep the later timestamp (that's the actual executed entry)
    seen = {}
    for e in entries:
        key = (e["sym"], e["date"], e["direction"])
        if key not in seen or e["ts"] > seen[key]["ts"]:
            seen[key] = e

    return sorted(seen.values(), key=lambda x: x["ts"])


# ── Step 3: Download 1-min OHLCV ─────────────────────────────────────────────
_price_cache = {}   # (sym, date) → (df, interval)

def get_price_data(sym, trade_date):
    key = (sym, trade_date)
    if key in _price_cache:
        return _price_cache[key]

    age_days = (datetime.now().date() - trade_date).days
    interval = "1m" if age_days <= 6 else "5m"
    start    = trade_date.strftime("%Y-%m-%d")
    end      = (trade_date + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = yf.download(sym, start=start, end=end,
                         interval=interval, progress=False, auto_adjust=True)
        if df.empty:
            _price_cache[key] = (None, interval)
            return None, interval

        # Flatten MultiIndex columns (newer yfinance versions use (field, ticker) tuples)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]

        # Ensure timezone-aware index in ET
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)

        _price_cache[key] = (df, interval)
        return df, interval

    except Exception as e:
        _price_cache[key] = (None, interval)
        return None, interval


# ── Step 4: Simulate one trade ────────────────────────────────────────────────
def simulate(entry, df, params):
    """
    Replay a trade bar-by-bar using the supplied exit params.
    Returns: {pnl_pct, pnl_usd, exit_reason, hold_mins}
    """
    direction = entry["direction"]
    ep        = entry["price"]
    qty       = entry["qty"]
    ts        = entry["ts"]

    sp  = params["stop_pct"]
    tp  = params["target_pct"]
    lem = params["loser_exit_mins"]
    wmm = params["winner_max_mins"]
    fep = params["flat_exempt_pct"]
    ecp = params["early_cut_pct"]
    eca = params["early_cut_min_age"]
    ecb = params["early_cut_max_age"]

    if direction == "bull":
        stop_px   = ep * (1 - sp)
        target_px = ep * (1 + tp)
    else:
        stop_px   = ep * (1 + sp)
        target_px = ep * (1 - tp)

    bars = df[df.index >= ts]
    if bars.empty:
        return dict(pnl_pct=0.0, pnl_usd=0.0, exit_reason="no_data", hold_mins=0.0)

    exit_px     = None
    exit_reason = "eod"
    hold_mins   = 0.0

    for bar_ts, bar in bars.iterrows():
        age = (bar_ts - ts).total_seconds() / 60
        try:
            o = float(bar["Open"]);  h = float(bar["High"])
            l = float(bar["Low"]);   c = float(bar["Close"])
        except Exception:
            continue

        # Early cut-and-run (5–10 min window)
        if eca <= age < ecb:
            pnl_now_pct = ((c - ep) / ep if direction == "bull" else (ep - c) / ep) * 100
            if pnl_now_pct <= ecp:
                exit_px, exit_reason, hold_mins = c, "early_cut", age
                break

        # Stop (conservative: checked before target within same bar)
        if direction == "bull" and l <= stop_px:
            exit_px, exit_reason, hold_mins = stop_px, "stop", age
            break
        if direction == "bear" and h >= stop_px:
            exit_px, exit_reason, hold_mins = stop_px, "stop", age
            break

        # Target
        if direction == "bull" and h >= target_px:
            exit_px, exit_reason, hold_mins = target_px, "target", age
            break
        if direction == "bear" and l <= target_px:
            exit_px, exit_reason, hold_mins = target_px, "target", age
            break

        # Loser exit at 20 min (flat-exempt: positions > fep ride to 40 min)
        if age >= lem:
            pnl_now_pct = ((c - ep) / ep if direction == "bull" else (ep - c) / ep) * 100
            if pnl_now_pct < fep:
                exit_px, exit_reason, hold_mins = c, "loser_exit", age
                break

        # Hard max at 40 min
        if age >= wmm:
            exit_px, exit_reason, hold_mins = c, "hard_max", age
            break

    # Fallback: use last bar close if nothing triggered
    if exit_px is None:
        try:
            exit_px = float(bars.iloc[-1]["Close"])
        except Exception:
            exit_px = ep
        hold_mins = (bars.index[-1] - ts).total_seconds() / 60

    pnl_pct = ((exit_px - ep) / ep if direction == "bull" else (ep - exit_px) / ep) * 100
    pnl_usd = pnl_pct / 100 * ep * qty

    return dict(
        pnl_pct=round(pnl_pct, 3),
        pnl_usd=round(pnl_usd, 2),
        exit_reason=exit_reason,
        hold_mins=round(hold_mins, 1),
    )


# ── Step 5: Run both param sets and collect results ───────────────────────────
def run(entries, label_a, params_a, label_b, params_b):
    rows  = []
    total = len(entries)

    for i, e in enumerate(entries):
        sym  = e["sym"]
        date = e["date"]
        tag  = f"{'LONG' if e['direction']=='bull' else 'SHORT'} @ ${e['price']:.2f}"
        print(f"  [{i+1:>2}/{total}] {e['ts'].strftime('%m/%d %H:%M')}  {sym:<6} {tag:<20}", end="", flush=True)

        df, interval = get_price_data(sym, date)
        if df is None:
            print("  ⚠️  no price data")
            continue

        ra = simulate(e, df, params_a)
        rb = simulate(e, df, params_b)
        diff = rb["pnl_usd"] - ra["pnl_usd"]

        print(f"  {label_a}: {ra['pnl_pct']:+.2f}% ({ra['exit_reason']:<11})  "
              f"{label_b}: {rb['pnl_pct']:+.2f}% ({rb['exit_reason']:<11})  "
              f"diff: {diff:+.0f}  [{interval}]")

        rows.append({
            "date":           e["ts"].strftime("%m/%d"),
            "time":           e["ts"].strftime("%H:%M"),
            "sym":            sym,
            "dir":            "LONG" if e["direction"] == "bull" else "SHORT",
            "entry_px":       e["price"],
            "qty":            e["qty"],
            "bar_interval":   interval,
            f"{label_a}_pnl%":  ra["pnl_pct"],
            f"{label_a}_$":     ra["pnl_usd"],
            f"{label_a}_exit":  ra["exit_reason"],
            f"{label_a}_mins":  ra["hold_mins"],
            f"{label_b}_pnl%":  rb["pnl_pct"],
            f"{label_b}_$":     rb["pnl_usd"],
            f"{label_b}_exit":  rb["exit_reason"],
            f"{label_b}_mins":  rb["hold_mins"],
            "diff_$":         round(diff, 2),
        })

    return pd.DataFrame(rows)


# ── Step 6: Print summary ─────────────────────────────────────────────────────
def summary(df, label_a, label_b):
    if df.empty:
        print("  (no trades to summarise)")
        return

    def stats(col_pct, col_usd, col_exit):
        wins  = df[df[col_pct] > 0]
        loss  = df[df[col_pct] < 0]
        wr    = len(wins) / len(df) * 100
        aw    = wins[col_usd].mean() if len(wins) else 0
        al    = loss[col_usd].mean() if len(loss) else 0
        total = df[col_usd].sum()
        exits = df[col_exit].value_counts().to_dict()
        return wr, aw, al, total, exits

    wr_a, aw_a, al_a, tot_a, ex_a = stats(f"{label_a}_pnl%", f"{label_a}_$", f"{label_a}_exit")
    wr_b, aw_b, al_b, tot_b, ex_b = stats(f"{label_b}_pnl%", f"{label_b}_$", f"{label_b}_exit")

    W = 66
    print("\n" + "═" * W)
    print(f"  BACKTEST SUMMARY  —  {len(df)} trades over last {LOOKBACK_DAYS} days")
    print("═" * W)
    print(f"  {'Metric':<26} {label_a:>16}   {label_b:>16}")
    print("─" * W)
    print(f"  {'Win Rate':<26} {wr_a:>15.1f}%   {wr_b:>15.1f}%")
    print(f"  {'Avg Win  $':<26} {aw_a:>+16.0f}   {aw_b:>+16.0f}")
    print(f"  {'Avg Loss $':<26} {al_a:>+16.0f}   {al_b:>+16.0f}")
    print(f"  {'Total P&L $':<26} {tot_a:>+16.0f}   {tot_b:>+16.0f}")
    diff = tot_b - tot_a
    print(f"  {'Improvement $':<26} {'':>17}   {diff:>+16.0f}")
    print("─" * W)

    all_reasons = sorted(set(list(ex_a.keys()) + list(ex_b.keys())))
    print(f"  {'Exit type':<22} {label_a:>8}   {label_b:>8}")
    for r in all_reasons:
        ca = ex_a.get(r, 0)
        cb = ex_b.get(r, 0)
        print(f"    {r:<20} {ca:>7}   {cb:>7}")

    print("═" * W)
    if diff > 50:
        print(f"\n  ✅  TEST params would have earned ${diff:,.0f} MORE — consider applying")
    elif diff < -50:
        print(f"\n  ❌  TEST params would have earned ${abs(diff):,.0f} LESS — keep CURRENT")
    else:
        print(f"\n  ➡️  Negligible difference (${diff:+.0f}) — params are roughly equivalent")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\n🔍  Market Genie Backtester")
    print(f"    Comparing: CURRENT params vs TEST params")
    print(f"    Lookback:  {LOOKBACK_DAYS} days\n")

    print("  Fetching Alpaca order history...")
    orders  = fetch_orders()
    entries = parse_entries(orders)

    if not entries:
        print("  No entry trades found — check your API keys or widen LOOKBACK_DAYS")
        return

    print(f"  Found {len(entries)} entry trades\n")

    # Print what we're testing so it's obvious
    diffs = {k: (CURRENT[k], TEST[k]) for k in CURRENT if CURRENT[k] != TEST[k]}
    if diffs:
        print("  Parameter changes being tested:")
        for k, (cur, tst) in diffs.items():
            print(f"    {k:<26} CURRENT={cur}  →  TEST={tst}")
        print()
    else:
        print("  ⚠️  TEST params are identical to CURRENT — edit the TEST block above\n")

    print("  Simulating trades...")
    print()
    df = run(entries, "CURRENT", CURRENT, "TEST", TEST)

    summary(df, "CURRENT", "TEST")

    # Save CSV
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.csv")
    df.to_csv(out, index=False)
    print(f"  📄  Full results saved → backtest_results.csv\n")


if __name__ == "__main__":
    main()
