# End-of-Day Analysis — 2026-05-06

_Generated at 20:09 UTC from 8 hourly snapshots_

## Account
- Final equity: **$97,652.89**
- Day P&L: **$-185.27**
- Cash: $48,146.18
- Open positions at close: 5

## Trade Stats
- Total trades: 21
- Wins / Losses: 12 / 9
- Win rate: 57.1%
- Total realized P&L: $-67.59
- Avg win: $+13.04 | Avg loss: $-24.90
- Reward:Risk ratio: 0.52

## Exit Breakdown
- Stops: 1
- Targets: 0
- Time/conv exits: 20
- Instant stops (<2 min): 1

## Top 5 Winners
- COF: $+30.09 (+0.30%) entry $193.41 → exit $194.00, 1201s, TIME/CONV
- TQQQ: $+27.77 (+0.28%) entry $70.77 → exit $70.97, 1292s, TIME/CONV
- COP: $+22.24 (+0.22%) entry $118.40 → exit $118.66, 1203s, TIME/CONV
- TQQQ: $+16.81 (+0.17%) entry $70.99 → exit $71.11, 1240s, TIME/CONV
- QQQ: $+13.45 (+0.14%) entry $691.35 → exit $692.31, 0s, TIME/CONV

## Top 5 Losers
- SOXL: $-75.36 (-0.76%) entry $162.73 → exit $161.49, 0s, STOP
- COP: $-45.58 (-0.46%) entry $119.06 → exit $118.51, 1247s, TIME/CONV
- SOFI: $-37.02 (-0.37%) entry $16.22 → exit $16.16, 1207s, TIME/CONV
- COP: $-28.55 (-0.29%) entry $118.71 → exit $118.37, 0s, TIME/CONV
- SLV: $-14.30 (-0.14%) entry $69.71 → exit $69.61, 1269s, TIME/CONV

## Repeat Losers (stopped/lost 2+ times)
- COP: 4 losing trades

## Instant Stop-Outs (<2 minutes)
- SOXL: $-75.36 (-0.76%) in 0s — entered at 2026-05-06T18:46

## Per-Ticker Performance
- TQQQ: 2 trades, 2W/0L, $+44.58
- QQQ: 5 trades, 5W/0L, $+42.54
- COF: 2 trades, 2W/0L, $+35.70
- IWM: 1 trades, 0W/1L, $-3.78
- PLTR: 1 trades, 0W/1L, $-5.92
- SLV: 1 trades, 0W/1L, $-14.30
- SOFI: 1 trades, 0W/1L, $-37.02
- COP: 6 trades, 2W/4L, $-57.04
- SOXL: 2 trades, 1W/1L, $-72.35

## Breadth Regime Through the Day
- 00:04 UTC: BULLISH (score 77.0, SPY 0.802%, QQQ 1.297%)
- 14:08 UTC: NEUTRAL (score 63.0, SPY 0.709%, QQQ 0.996%)
- 15:08 UTC: BULLISH (score 73.0, SPY 1.179%, QQQ 1.47%)
- 16:08 UTC: BULLISH (score 80.0, SPY 1.111%, QQQ 1.576%)
- 17:08 UTC: BULLISH (score 80.0, SPY 1.067%, QQQ 1.501%)
- 18:09 UTC: BULLISH (score 80.0, SPY 1.191%, QQQ 1.686%)
- 19:09 UTC: BULLISH (score 80.0, SPY 1.369%, QQQ 1.85%)
- 20:09 UTC: BULLISH (score 80.0, SPY 1.394%, QQQ 2.075%)

## Open Positions Carried
- COF long 51 @ $193.12 → $193.40 | UPL $+14.28 (+0.14%)
- COP long 84 @ $118.60 → $118.90 | UPL $+25.20 (+0.25%)
- QQQ long 14 @ $694.85 → $696.37 | UPL $+21.34 (+0.22%)
- SLV long 142 @ $70.12 → $70.18 | UPL $+8.52 (+0.09%)
- SOFI long 611 @ $16.34 → $16.27 | UPL $-42.77 (-0.43%)

## Observations for Tomorrow
- **Win rate 57.1% but net loss of $-67.59** — losers are larger than winners (avg win $13.04 vs avg loss $-24.90). Reward:risk = 0.52. Tighten exits or let winners run longer.
- **20/21 trades closed via TIME/CONV** — bracket targets/stops aren't getting hit. System exits flat too often, capping upside on movers.
- **COP traded 6x with 4 losers** — overtrading the same ticker after losses. Add a cooldown after a losing trade in the same symbol.
- Breadth was BULLISH for 7/8 of the day yet system lost money — the long-side strategy is underperforming a strong tape (SPY +1.394%, QQQ +2.075%). Diagnose entry timing.
- Instant stop on SOXL (<2 min) — entering into noise/late. Add a 'wait for first candle close' filter or VWAP confirmation.