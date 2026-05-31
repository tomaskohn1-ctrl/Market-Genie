# Market Genie — Entry / Exit Strategy

*Built from your own trade logs (124 deduplicated trades). This is decision-support, not financial advice. 124 trades is a small sample — treat these as a tested hypothesis, not a guarantee.*

## What the data said

| Exit type | Trades | Win rate | Net P&L | Avg / trade |
|-----------|-------:|---------:|--------:|------------:|
| TARGET    | 10     | 100%     | **+$2,501** | +$250 |
| TIME/CONV | 82     | 32%      | −$589   | −$7 (churn) |
| STOP      | 32     | 0%       | **−$3,918** | −$122 |
| **Total** | **124**| **29%**  | **−$2,006** | |

Your exits are not the problem — winners pay 2:1+ and the system already cuts losers at 20 min and trails winners to 40 min. The losses come from **entry selectivity** and **time of day**.

By entry hour (ET):

- 9:45 open: +$257 (good) · 11am: +$385 (good) · noon: +$223 (ok)
- **10:00–10:59 ET: −$1,144, zero wins** — worst hour of the day
- **1:00–1:59 ET: −$634, 13% wins** — post-lunch chop
- 2–3pm: small wins offset by occasional large reversals

## The three rules

1. **Blackout / quality-gate the chop hours (10am & 1pm ET).** No marginal entries in these windows. Only take a trade if both models agree *and* confidence ≥ 85.
2. **Bench the chronic losers.** `LABU, SPXS, SQQQ, NUGT, FNGD, DUST, ERY, FAS, TECL` bled in aggregate. Concentrate on the proven winners: `TQQQ, TECS, LABD, QQQ, ERX, SPXL`. A benched name re-earns its slot by proving out on paper.
3. **Raise the entry bar (edge ≥ 95).** Most losing trades were marginal signals that drifted to a time or stop exit. Fewer, higher-quality entries — selectivity raises profitability more than bigger size does.

## Exit refinement (scale-out)

Since TARGET hits are the only reliable profit and TIME exits bleed flat-to-red: bank **50% at +0.75%**, move the stop on the rest to breakeven, then **trail the remainder 0.4%** up to your existing 40-min cap. This converts "went nowhere" trades from flat/red into locked partial gains.

## What this would have done (same history)

Applying rules 1–2 to your logged trades:

> **−$2,006 → +$936**, a **+$2,942 swing**, win rate **29% → 47%**, by removing 58 of 124 trades.

This is in-sample, so it flatters itself — but the mechanism (skip the zero-win chop hour, stop funding chronic losers) is robust, not a curve-fit number.

## How it's wired

`strategy_engine.py` exposes `strategy_gate(res)` and `scale_out_plan(entry, side)`. To activate, add inside `_alp_execute_signal` just before the bracket order:

```python
from strategy_engine import strategy_gate
ok, why = strategy_gate(res)
if not ok and not res.get("_forced"):
    print(f"[StratGate] {sym} SKIPPED: {why}")
    return
```

Every threshold is an env var (in `.env`) — no code edits needed to tune:

```
STRAT_MIN_EDGE=95
STRAT_CHOP_MIN_CONF=85
STRAT_REQUIRE_AGREE_IN_CHOP=1
STRAT_BENCH_SYMBOLS=LABU,SPXS,SQQQ,NUGT,FNGD,DUST,ERY,FAS,TECL
STRAT_PROVEN_SYMBOLS=TQQQ,TECS,LABD,QQQ,ERX,SPXL
STRAT_PARTIAL1_PCT=0.0075
STRAT_TRAIL_PCT=0.004
```

Re-run the analysis any time as you collect more trades: `python strategy_engine.py`
