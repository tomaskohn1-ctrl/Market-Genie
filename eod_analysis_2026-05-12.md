# End-of-Day Analysis - 2026-05-12

## Final P&L and Equity
- Equity: $95,631.83
- Day P&L: $-1,525.40
- Cash: $80,801.67
- Realized P&L from completed trades: $-1,376.10

## Trade Performance
- Total trades: 19
- Wins: 3  |  Losses: 16
- Win rate: 15.8%
- Avg win: $23.07
- Avg loss: $-90.33
- Reward:Risk ratio: 0.26

## Exit Breakdown
- Stops: 10
- Targets: 0
- Time/Conv exits: 9
- Instant stops under 2 min: 1

## Top 5 Winners
- SPXL: +$31.13 (+0.21%) - entry 2026-05-12T19:35, qty 56, exit TIME/CONV, dur 1202s
- FAZ: +$21.29 (+0.14%) - entry 2026-05-12T19:15, qty 336, exit TIME/CONV, dur 2389s
- TECS: +$16.79 (+0.11%) - entry 2026-05-12T18:43, qty 1679, exit TIME/CONV, dur 405s

## Top 5 Losers
- TECS: $-186.56 (-1.24%) - entry 2026-05-12T19:33, qty 1696, exit STOP, dur 1319s
- SQQQ: $-184.14 (-1.23%) - entry 2026-05-12T19:29, qty 341, exit STOP, dur 1518s
- TECS: $-132.72 (-0.89%) - entry 2026-05-12T17:44, qty 1659, exit STOP, dur 2289s
- DUST: $-124.69 (-0.83%) - entry 2026-05-12T17:56, qty 337, exit STOP, dur 1211s
- TECS: $-117.18 (-0.78%) - entry 2026-05-12T18:51, qty 1674, exit STOP, dur 1282s

## Repeat Losers (stopped 2+ times)
- TECS: stopped out 4 times
- FNGD: stopped out 2 times

## Instant Stop-Outs (under 2 minutes)
- FNGD: $-102.83 (-0.69%) - entry 2026-05-12T17:43, dur 0s

## Breadth Regime Through the Day
- 14:09 UTC: score 31.0, regime BEARISH, SPY -0.529%, QQQ -0.977%
- 16:09 UTC: score 17.0, regime BEARISH, SPY -0.844%, QQQ -1.845%
- 17:09 UTC: score 16.0, regime BEARISH, SPY -0.802%, QQQ -2.006%
- 18:09 UTC: score 24.0, regime BEARISH, SPY -0.575%, QQQ -1.657%
- 19:09 UTC: score 34.0, regime BEARISH, SPY -0.387%, QQQ -1.443%
- 20:08 UTC: score 38.0, regime NEUTRAL, SPY -0.153%, QQQ -0.848%

## Observations for Tomorrow
- Heavy bias toward inverse/short ETFs (17 of 19 trades, P&L $-1290.64) vs. long-leveraged (2 trades, P&L $-85.46). System is fighting the tape - breadth held NEUTRAL with positive bull_conf, yet the model kept buying inverse ETFs.
- Win rate 15.8% with 10 stops vs. 0 targets means the system is hitting stops but never reaching profit targets. Reward:risk of 0.26 is far below the 1.0 floor needed for break-even.
- TECS stopped out 4 times today - add a same-symbol cooldown (30+ min after a stop) to avoid re-entering the same loser.
- Zero target hits all day - either the 0.8% target is too far given today's range, or time-based exits are firing too soon. Consider a trailing stop once trade is +0.3% to lock partial gains.
- Carrying overnight: FNGD qty 394 @ $38.02, now $37.64 (-1.00%, P&L $-149.72). Confirm intent - system should normally flat by close.
- Avg loss ($-90.33) is ~3.9x avg win ($23.07) - asymmetry is the single biggest drag on equity.