# Changelog

All strategy/config changes, newest first. Format and rules: `docs/CHANGE_CONTROL.md`.

## v1.0-trend-gate-majors — 2026-10-02 (pre-competition baseline)

* **Strategy:** 5 majors, per-coin trend-gate ensemble (7/14/30 d), inverse-vol weights,
  30 % annualised vol target, half-size shorts on down-gates, drawdown governor 3 %→10 %.
* **Execution:** passive limit at the touch, one hour of patience, then taker; shorts via `/v6` (taker).
* **Evidence:** `docs/RESEARCH_LOG.md` R1–R5, `reports/backtest/v1.0-trend-gate-majors/`.
* **Rejected before launch:** short-term reversal (R2: loses net of fees in all 64 configs),
  residual XS momentum (R1: unstable sign), market-trend gross scaling (R1/R3: negative).
* **Rollback condition:** n/a (baseline).
