# Changelog

All strategy/config changes, newest first. Format and rules: `docs/CHANGE_CONTROL.md`.

## v1.3-endgame-long — 2026-10-10 (live, tournament end-game, team decision)

* **Situation:** contest return −2.47 % (15th of 24 Indian teams), 4–7 days left. Every gate needs a +3 % … +13 %
  rise to turn up, so the trend book (≈ 13 % net short) had ~1–2 % chance to finish positive. A negative
  return makes every scored ratio negative, so only a positive finish keeps a top-5 chance.
* **Change:** `endgame_long: 0.60` — hold a fixed 60 % net-long inverse-vol basket of the 5 coins (gates
  ignored); `endgame_floor_return: −0.045` — if the contest return reaches −4.5 % the book goes flat with
  market orders and stays flat (sticky), to protect the top-20 regional cut (≈ −5 %). Caps, 1× limit,
  compliance gate, drawdown governor and activity guard unchanged.
* **Evidence** (540 days of hourly history, every 4/7-day window, from −2.47 %): P(finish > 0) current book
  1–2 %; 60 % long 20–29 % but P(< −5 %) 17–27 %; 60 % long **with the floor 17–22 %, P(< −5 %) 9–10 %**.
  There is no directional edge (median unchanged); this is an explicit, documented tournament bet.
* **Rollback condition:** none (floor is the risk control).

## v1.2-endgame — 2026-10-08 (live, Tier 4 parameter change with evidence)

* **Situation:** NAV −1.95 % after the Oct 7 market drop, drawdown 2.8 % (just below the 3 % governor
  trigger), ~9 days left. With a negative return every ratio is negative, so the objective is now
  P(finish > 0), which requires ≈ +2.1 % from here.
* **Hypothesis:** (1) the 30-day gate barely moves within the remaining horizon; shorter gates match it.
  (2) the drawdown governor (3 %→10 %) was designed to protect a lead and would cut exposure exactly
  when recovery needs it.
* **Change:** `gate_lookbacks` [168, 336, 720] → [126, 252, 540] (×0.75, a point on the pre-launch
  robustness plateau); `dd_soft/dd_hard` 0.03/0.10 → 0.06/0.15. Everything else unchanged.
* **Evidence** (`research/endgame.py`, full engine, every 10-day window, `reports/backtest/endgame.csv`):
  P(10-day > +2.1 %) from a ≥2 % drawdown 13 % → 16 %; median from a drawdown −0.16 % → −0.07 %;
  q10 −2.5 % → −2.4 %; worst 10-day −5.1 % → −5.6 %; full-history Sharpe 0.94 → 1.28, MaxDD 10.5 % → 10.1 %.
  More aggressive variants (40 % vol, full shorts) raised P by ≤ 6 pts but worsened the worst case to
  −7 % … −12 %, so they were rejected.
* **Immediate effect:** one position changes (SOL +4.5 % long → −2.4 % short, its 5-day gate is down).
* **Rollback condition:** none planned for the remaining days (no reactive re-tuning).

## v1.1.2 — 2026-10-07 (live, Tier 2 bug fix)

* **Incident:** at 02:01 UTC one coin's (XRP) newest hourly bar was missing, so its last close disagreed
  with Roostoo's live price by > 2 % and the integrity check fired. The check set that coin's alpha to
  NaN, which (a) set its target to 0 -> closed a profitable XRP short, and (b) removed it from the
  inverse-vol normalisation -> other coins' targets were inflated (BNB 19.1 % -> 24.7 %, SOL 13.2 % -> 16.8 %)
  and the bot bought them, reversing an hour later when data recovered. Cost: ~$25 fees + a closed short.
  Confirmed from `decision.jsonl` (XRP absent from alpha at 02:01 only).
* **Root cause 2:** in `update_bars`, the last-resort bundled snapshot returns *old* bars; treating them
  as "new data" prevented the Roostoo-snapshot bar from being built, so the coin stayed stale.
* **Fix:** (1) unreliable coins are *held* at their current weight; their alpha stays in the sizing so
  other coins are unaffected; the activity guard skips them. (2) only bars newer than the last held bar
  count; otherwise the bar is built from Roostoo snapshots. Per-coin bar source logged every cycle.
* **Evidence:** 3 regression tests reproduce the incident; strategy and parameters unchanged.

## v1.1.1 — 2026-10-06 (live, Tier 2 bug fix)

* **Bug:** the daily activity guard compared against the *banded* target, so on quiet days where
  holdings already sat inside the no-trade band it found no gap and placed nothing. Found via
  `scripts/report.py` (active trading days = 2 after 3 days). Risk: failing the ">= 8 active trading
  days with enough trades" rule.
* **Fix:** guard uses the unbanded target, moves the most mis-weighted coin 0.5-2% NAV toward it as a
  MARKET order (certain fill, ~$0.50 fee), targets >= 2 fills/UTC day, retries hourly from 12:00 UTC.
  Never trades if today's fills cannot be counted. Strategy and parameters unchanged.
* **Also:** contest start corrected to 2026-10-04 12:00 UTC; "no permission" no longer disables shorts.

## v1.1-gate-deadband — 2026-10-02 (pre-competition)

* **Hypothesis:** hourly evaluation of sign gates causes noise flips; a ±0.5σ dead-band
  removes them. All-time-peak drawdown governor over-de-risks; contest-length peak is correct.
* **Change:** `gate_dead_band: 0.5`, `dd_peak_window_h: 336`; data fallback Binance→OKX→snapshot;
  no orders on deployment keys before 2026-10-03T16:00Z; order slicing at 25 % NAV.
* **Evidence:** R6 — full-engine Sharpe 0.10 → 0.94, turnover 110× → 41×, fees $8.5k → $3.1k;
  perturbation table all positive.
* **Rollback condition:** if live maker fill rate < 20 % or realised turnover > 3× backtest pace.

## v1.0-trend-gate-majors — 2026-10-02 (pre-competition baseline)

* **Strategy:** 5 majors, per-coin trend-gate ensemble (7/14/30 d), inverse-vol weights,
  30 % annualised vol target, half-size shorts on down-gates, drawdown governor 3 %→10 %.
* **Execution:** passive limit at the touch, one hour of patience, then taker; shorts via `/v6` (taker).
* **Evidence:** `docs/RESEARCH_LOG.md` R1–R5, `reports/backtest/v1.0-trend-gate-majors/`.
* **Rejected before launch:** short-term reversal (R2: loses net of fees in all 64 configs),
  residual XS momentum (R1: unstable sign), market-trend gross scaling (R1/R3: negative).
* **Rollback condition:** n/a (baseline).
