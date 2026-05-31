# Market Genie End-of-Day Analysis — 2026-05-07

**Generated:** 2026-05-07T20:09:16.794419+00:00

## Account & P&L

- **Final Equity:** $97,548.17
- **Day P&L:** $-106.90
- **Realized P&L (closed trades):** $-192.79
- **Cash:** $97,548.17
- **Open positions at close:** 0

## Trade Performance

- **Total Trades:** 28
- **Wins / Losses:** 8 / 20
- **Win Rate:** 28.6%
- **Avg Win:** $80.38
- **Avg Loss:** $-41.79
- **Reward:Risk Ratio:** 1.92

## Exit Breakdown

- **STOPs:** 8
- **TARGETs:** 3
- **TIME/CONV exits:** 17
- **Instant stop-outs (<2 min):** 2

## Top 5 Winners

| Sym | Entry | Exit | Qty | P&L | % | Dur(s) | Type |
|---|---|---|---|---|---|---|---|
| TECS | 9.43 | 9.71 | 1060 | $+296.80 | +2.97% | 1531 | TARGET |
| TSLL | 13.96 | 14.13 | 716 | $+121.72 | +1.22% | 2416 | TARGET |
| FNGD | 39.89 | 40.23 | 250 | $+85.00 | +0.85% | 0 | TARGET |
| TECS | 9.68 | 9.75 | 1033 | $+72.31 | +0.72% | 2413 | TIME/CONV |
| TZA | 4.72 | 4.74 | 2118 | $+42.36 | +0.42% | 0 | TIME/CONV |

## Top 5 Losers

| Sym | Entry | Exit | Qty | P&L | % | Dur(s) | Type |
|---|---|---|---|---|---|---|---|
| LABU | 186.10 | 184.05 | 53 | $-108.84 | -1.10% | 1295 | STOP |
| LABD | 13.92 | 13.77 | 718 | $-107.70 | -1.08% | 1261 | STOP |
| TECL | 177.57 | 176.02 | 55 | $-85.25 | -0.87% | 2058 | STOP |
| HOOD | 77.78 | 77.13 | 128 | $-83.15 | -0.83% | 770 | STOP |
| SOXL | 161.08 | 159.80 | 62 | $-79.36 | -0.80% | 0 | STOP |

## Repeat Losers (stopped 2+ times)

- **TECL:** stopped 2x

## Symbols with 2+ Losing Trades (any exit type)

- **TECL:** 3 losing trades

## Instant Stop-Outs (<2 minutes)

- **SOXL** — entered 2026-05-07T14:47, stopped after 0s, $-79.36 (-0.80%)
- **TECL** — entered 2026-05-07T18:12, stopped after 0s, $-74.10 (-0.74%)

## Breadth Regime Throughout the Day

| Time | Score | Regime | SPY% | QQQ% |
|---|---|---|---|---|
| 14:09 UTC | 47.0 | NEUTRAL | +0.08 | +0.32 |
| 15:09 UTC | 51.0 | NEUTRAL | +0.21 | +0.48 |
| 16:09 UTC | 58.0 | NEUTRAL | -0.11 | +0.01 |
| 17:08 UTC | 50.0 | NEUTRAL | -0.25 | -0.11 |
| 18:08 UTC | 42.0 | NEUTRAL | -0.29 | -0.06 |
| 19:08 UTC | 42.0 | NEUTRAL | -0.38 | -0.29 |
| 20:09 UTC | 42.0 | NEUTRAL | -0.31 | -0.12 |

## Observations for Tomorrow

1. Win rate (28.6%) is too low to be profitable at the current reward:risk of 1.92. To break even at this win rate, R:R would need to be roughly 2.5:1 or better. Either tighten entries to raise win rate or let winners run further.
2. TIME/CONV exits (17) dominated targeted outcomes — most trades didn't reach a stop or target before being closed for time. The system is exiting on noise more than on signal; consider widening the target or shortening the holding window.
3. Stops (8) outnumbered targets (3) by 2.7x — entries are getting hit on adverse moves more than they're capturing favorable ones. Suggests entries are too early or too late into momentum.
4. Leveraged ETFs (19 trades, $+37.47) made up most of the activity. Pairs like TECL↔TECS and LABU↔LABD show the system flipping direction repeatedly — this is whipsaw exposure, not edge. Add a directional confirmation check before flipping.
5. Breadth regime was 'NEUTRAL' for 7 of 7 snapshots. With SPY/QQQ slightly negative and a NEUTRAL regime score around the low 40s, the tape did not give clean directional signal — sizing should be reduced on neutral-regime days, especially early.
6. Repeat stop-outs on the same ticker the same day (TECL (2x)) is a red flag — the model is re-entering a losing thesis. Add a cooldown so a stopped symbol can't be re-traded for at least 60 minutes.