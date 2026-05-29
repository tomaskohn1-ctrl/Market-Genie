"""
strategy_engine.py  —  Market Genie entry/exit profitability layer
===================================================================

WHY THIS EXISTS
---------------
Analysis of your own trade logs (124 deduplicated completed trades) showed:

    TOTAL  : -$2,006   win-rate 29%
    TARGET :  10 trades, 100% win, +$2,501   (avg +$250)   <- the ONLY profit
    STOP   :  32 trades,   0% win, -$3,918   (avg -$122)   <- biggest leak
    TIME   :  82 trades,  32% win,  -$589    (churn, slow bleed)

    By entry hour (ET):
       9:45 open .... +$257  (43% win)   GOOD
      10:00-10:59 ... -$1,144 ( 0% win)  WORST HOUR OF THE DAY
      11:00-11:59 ... +$385  (30% win)   GOOD
      12:00-12:59 ... +$223  (28% win)   OK
       1:00- 1:59 ... -$634  (13% win)   BAD (post-lunch chop)
       2:00- 2:59 ... -$610  (56% win)   small wins, occasional big loss
       3:00- 3:59 ... -$483  (33% win)   late-day reversals

CONCLUSION: the exits are fine. The losses come from (a) trading the
10am ET and 1pm ET chop windows and (b) too many marginal entries that
never reach target and bleed out on time/stop. The fix is SELECTIVITY,
not bigger size.

This module is ADDITIVE and non-invasive. It exposes two helpers you can
call from market_genie_server.py without changing existing logic:

    strategy_gate(res)            -> (allow: bool, reason: str)
    scale_out_plan(entry, side)   -> dict of partial-exit levels

Wire-in (optional, inside _alp_execute_signal, right before the bracket
order is placed):

    from strategy_engine import strategy_gate
    ok, why = strategy_gate(res)
    if not ok and not res.get("_forced"):
        print(f"[StratGate] {sym} SKIPPED: {why}")
        return

Every threshold is overridable by env var so you can tune from .env
without editing code.

NOTE: This is decision-support tooling, not financial advice. Past trade
statistics do not guarantee future results, and 124 trades is a small
sample — treat these rules as a starting hypothesis and keep logging.
"""

import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


# ───────────────────────── tunables (env-overridable) ─────────────────────
def _f(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _i(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

# Minimum edge score to enter at all (your _wr_edge_score output).
# Marginal signals are what fill the losing TIME/STOP buckets.
STRAT_MIN_EDGE        = _f("STRAT_MIN_EDGE", 95.0)
# Require both models to agree during chop windows.
STRAT_REQUIRE_AGREE   = _i("STRAT_REQUIRE_AGREE_IN_CHOP", 1)
# Confidence bar that must be cleared inside a chop window.
STRAT_CHOP_MIN_CONF   = _f("STRAT_CHOP_MIN_CONF", 85.0)

# Symbols that bled consistently in the logs — benched until they re-earn
# their place. Comma-separated env override available.
_DEFAULT_BENCH = "LABU,SPXS,SQQQ,NUGT,FNGD,DUST,ERY,FAS,TECL"
STRAT_BENCH = {
    s.strip().upper()
    for s in os.getenv("STRAT_BENCH_SYMBOLS", _DEFAULT_BENCH).split(",")
    if s.strip()
}
# Symbols that proved themselves — always allowed (skip the bench check).
_DEFAULT_PROVEN = "TQQQ,TECS,LABD,QQQ,ERX,SPXL"
STRAT_PROVEN = {
    s.strip().upper()
    for s in os.getenv("STRAT_PROVEN_SYMBOLS", _DEFAULT_PROVEN).split(",")
    if s.strip()
}

# Chop windows (ET). Inside these, entries must be high-quality.
# 10:00-10:59 was the single worst hour; 13:00-13:59 the post-lunch fade.
def _chop_windows():
    return [
        (dtime(10, 0), dtime(11, 0)),
        (dtime(13, 0), dtime(14, 0)),
    ]

# Scale-out levels (% move). Take half off at first target, trail the rest.
STRAT_PARTIAL1_PCT = _f("STRAT_PARTIAL1_PCT", 0.0075)   # +0.75% -> sell 50%
STRAT_PARTIAL1_FRAC= _f("STRAT_PARTIAL1_FRAC", 0.50)
STRAT_TRAIL_PCT    = _f("STRAT_TRAIL_PCT", 0.004)        # trail remainder 0.4%


# ── Filter telemetry — counts how many entries the gate skipped today ──────
import threading as _threading
_stats_lock = _threading.Lock()
STRAT_STATS = {"date": None, "allowed": 0, "skipped": 0, "by_reason": {}}


def _stat_record(allowed, reason):
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    with _stats_lock:
        if STRAT_STATS["date"] != today:
            STRAT_STATS.update(date=today, allowed=0, skipped=0, by_reason={})
        if allowed:
            STRAT_STATS["allowed"] += 1
        else:
            STRAT_STATS["skipped"] += 1
            tag = reason.split("(")[0].split(";")[0].strip()[:48]
            STRAT_STATS["by_reason"][tag] = STRAT_STATS["by_reason"].get(tag, 0) + 1


def get_stats():
    """Snapshot of today's gate activity (for /api/strat/status)."""
    with _stats_lock:
        return dict(STRAT_STATS, by_reason=dict(STRAT_STATS["by_reason"]))


def _now_et():
    return datetime.now(_ET)


def _in_chop(now=None):
    t = (now or _now_et()).time()
    for a, b in _chop_windows():
        if a <= t < b:
            return True
    return False


def strategy_gate(res, now=None):
    """
    Returns (allow: bool, reason: str).

    Layered on top of your existing gates — only ever makes entries MORE
    selective, never looser. Designed to remove the trades that historically
    landed in the STOP / TIME-exit buckets.
    """
    sym = (res.get("sym") or "").upper()
    conf = float(res.get("confidence", 0) or 0)
    both_agree = int(res.get("both_agree", 0) or 0)
    edge = res.get("edge_score")
    proven = sym in STRAT_PROVEN

    def _deny(reason):
        _stat_record(False, reason)
        return False, reason

    # 1) Bench chronic losers (unless they're also on the proven list).
    if sym in STRAT_BENCH and not proven:
        return _deny(f"benched: {sym} net loser in logs")

    # 2) Edge-score floor — the marginal-signal filter.
    if edge is not None and float(edge) < STRAT_MIN_EDGE and not proven:
        return _deny(f"edge below floor ({float(edge):.0f} < {STRAT_MIN_EDGE:.0f})")

    # 3) Chop-window quality bar (10am / 1pm ET).
    if _in_chop(now):
        if STRAT_REQUIRE_AGREE and both_agree != 1:
            return _deny("chop window (10am/1pm ET) requires both_agree=1")
        if conf < STRAT_CHOP_MIN_CONF:
            return _deny(f"chop window requires conf>={STRAT_CHOP_MIN_CONF:.0f} (got {conf:.0f})")

    _stat_record(True, "ok")
    return True, "ok"


def scale_out_plan(entry_px, side):
    """
    Suggested partial-exit plan. Since TARGET hits are your only reliable
    profit source and TIME exits bleed, bank a partial early and trail the
    rest so 'went nowhere' trades end green instead of flat/red.
    """
    side = (side or "long").lower()
    sign = 1 if side in ("long", "buy", "bull") else -1
    p1 = entry_px * (1 + sign * STRAT_PARTIAL1_PCT)
    return {
        "partial_1": {
            "price": round(p1, 4),
            "fraction": STRAT_PARTIAL1_FRAC,
            "note": f"sell {int(STRAT_PARTIAL1_FRAC*100)}% at {STRAT_PARTIAL1_PCT*100:.2f}%, "
                    f"move stop on remainder to breakeven",
        },
        "trail_remainder_pct": STRAT_TRAIL_PCT,
        "note": "after partial_1, trail the remaining shares; let winners run to your existing 40-min cap",
    }


# ─────────────────── re-runnable log analysis (CLI) ───────────────────────
def analyze_logs(folder="."):
    """Re-run the dedup + segmentation any time. `python strategy_engine.py`"""
    import json, glob, collections
    trades = {}
    for fn in sorted(glob.glob(os.path.join(folder, "trading_log_*.json"))):
        try:
            d = json.load(open(fn))
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        for snap in d:
            regime = snap.get("breadth", {}).get("regime", "?")
            for t in snap.get("completed_trades", []):
                key = (t.get("sym"), t.get("entry_ts"),
                       round(t.get("exit_px", 0), 3), round(t.get("pnl", 0), 2))
                if key in trades:
                    continue
                t["_regime"] = regime
                trades[key] = t
    T = list(trades.values())
    if not T:
        print("No trades found.")
        return

    def agg(keyfn):
        g = collections.defaultdict(lambda: [0, 0, 0.0])
        for t in T:
            k = keyfn(t); g[k][0] += 1; g[k][2] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                g[k][1] += 1
        return {k: dict(n=v[0], wins=v[1], pnl=round(v[2], 1),
                        wr=round(100 * v[1] / v[0])) for k, v in g.items()}

    pnl = sum(t.get("pnl", 0) for t in T)
    wins = sum(1 for t in T if t.get("pnl", 0) > 0)
    print(f"TRADES {len(T)}  PnL {pnl:+.1f}  win-rate {round(100*wins/len(T))}%")
    print("BY EXIT  :", agg(lambda t: t.get("exit_type")))
    print("BY REGIME:", agg(lambda t: t.get("_regime")))
    hr = lambda t: (t.get("entry_ts", "")[11:13] or "?") + ":00 UTC"
    print("BY HOUR  :", dict(sorted(agg(hr).items())))


if __name__ == "__main__":
    analyze_logs(os.path.dirname(os.path.abspath(__file__)))
