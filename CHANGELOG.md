# Changelog

All strategy/config changes, newest first. Format and rules: `docs/CHANGE_CONTROL.md`.

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
