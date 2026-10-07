# Changelog

All strategy/config changes, newest first. Format and rules: `docs/CHANGE_CONTROL.md`.

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
