# Research Log — Team105 Threshold Crushers

Every hypothesis we tested, including the ones we **rejected**. Data: Binance hourly
klines for all 88 Roostoo pairs (Roostoo mirrors Binance prices; verified BTC within
~1 bp), 540 days to 2026‑10‑02, 66 crypto pairs after excluding tokenised equities.
All results are net of the competition fee schedule unless stated otherwise.

Reproduce: `python scripts/download_data.py --days 540` then the scripts named below.

---

## R1. Signal term structure (rank IC) — `research/signal_research.py`

Cross-sectional Spearman IC between each sleeve's score and forward log returns,
t‑stats on **non-overlapping** samples (overlapping horizons inflate t‑stats).

| Sleeve | 1h IC (t) | 4h IC (t) | 24h IC | 168h IC (t) | Stable across halves? |
|---|---|---|---|---|---|
| Short-term residual reversal (6h) | **0.037 (17.7)** | **0.037 (8.5)** | 0.019 | −0.007 | yes |
| Multi-horizon trend | −0.017 | −0.019 | −0.006 | **0.037 (2.7)** | weaker in 2nd half |
| Residual XS momentum (168h) | −0.003 | −0.004 | −0.001 | 0.030 (0.7) | **no** (sign flips) |
| Market trend → market fwd return | — | −0.010 | −0.031 | (72h: −0.083) | — |

**Take-aways.** Reversal is real but lives at 1–12 h. Trend only pays at ≥1 week.
Residual momentum is noise. Timing *aggregate* exposure with market trend had a
**negative** relationship at 1–3 day horizons → we do **not** scale gross with trend.

## R2. Can reversal pay its turnover? — `research/reversal_economics.py`

Long-only tilt into the K most oversold coins vs the EW basket, every combination of
lookback L ∈ {3,6,12,24}h, K ∈ {5,10}, holding H ∈ {1,4,8,24}h, cost 5/10 bps.

* Gross excess ≈ +49 %/yr at H = 1h, but turnover ≈ 9,000×/yr ⇒ ≈ 456 %/yr in **maker** fees.
* **All 64 configurations are negative net of costs** (best: −21 %/yr).

**Rejected.** Full-engine confirmation (`research/backtest_variants.py`): every reversal
variant lost 18–43 % over 17 months with $14k–$33k of fees on $100k. This is the
over-trading trap most bots will fall into; avoiding it is itself an edge.

## R3. Low-turnover allocations — `research/beta_research.py`

| Variant | Return | Sharpe | MaxDD | 14d-window q90 MaxDD |
|---|---|---|---|---|
| EW all-coins hold | −49.8 % | −0.41 | 77 % | 24 % |
| BTC hold | −19.5 % | −0.20 | 54 % | 15 % |
| EW vol-target 30 % | −11.8 % | −0.14 | 46 % | 11 % |
| EW + market-trend filter (any lookback) | −18 % … −63 % | < 0 | 46–78 % | — |
| Top‑5 by 168h trend + filters | +7.9 % | 0.35 | 38 % | 9 % |

**Take-aways.** (i) Alts bled vs BTC (median coin −57 % vs BTC −19 %) → concentrate in
majors. (ii) Vol targeting is the one robust improvement — it halves tail drawdowns.
(iii) Index-level trend filters whipsawed.

## R4. Majors, per-coin trend gates — `research/core_allocation.py`, `research/trend_ensemble.py`

Per-coin gate on the 5 majors (BTC, ETH, SOL, BNB, XRP), inverse-vol weights, vol target.

| Gate lookbacks (vt 25 %) | Sharpe | MaxDD | PSR | Sharpe by thirds |
|---|---|---|---|---|
| single 72h | −0.60 | 43 % | 0.25 | — |
| single 336h | 0.10 | 32 % | 0.55 | — |
| single 720h | 0.99 | 22 % | 0.87 | 1.55 / −1.92 / 2.41 |
| **ensemble 168/336/720h** | **0.68** | **21 %** | **0.78** | 1.13 / −1.28 / 1.87 |
| ensemble ×0.75 | 0.59 | 22 % | 0.75 | |
| ensemble ×1.25 | 0.80 | 21 % | 0.82 | |
| ensemble, top‑10 liquid universe | 0.39 | 25 % | 0.67 | |

Single lookbacks are fragile (336h ≈ 0, 720h ≈ 1.0 — a cliff). The ensemble's Sharpe
surface is **smooth under ±25 % perturbation**, so we use the centred ensemble, *not*
the best-looking row (choosing the max of a grid is how backtests get overfit).

## R5. Shorts

| Short exposure when gates point down | Return | Sharpe | MaxDD | P(14d > 0) |
|---|---|---|---|---|
| none (flat) | +10.7 % | 0.56 | 21.4 % | 0.33 |
| **half size** | **+12.6 %** | **0.61** | **15.7 %** | **0.45** |
| full size | +13.5 % | 0.55 | 20.5 % | 0.42 |

Half-size shorts cut MaxDD by a quarter (Calmar is 30 % of the score) — adopted.
Shorts on Roostoo cost 0.1 % per leg regardless of order type, so they are sized smaller.

## R6. Hourly gate churn and the dead-band — `research/gate_churn.py`

The vectorised research (R4/R5) rebalanced daily. The full hourly engine exposed a gap:
a gate whose trend sits near zero flips sign every few hours, and each flip turns over a
whole position. Plain sign gates: **110× NAV turnover, $8.5k fees, Sharpe 0.10**.

| Dead-band (σ) | Turnover | Fees | Sharpe | MaxDD |
|---|---|---|---|---|
| 0 | 110× | $8,533 | 0.10 | 13.1 % |
| 0.25 | 39× | $2,857 | 0.49 | 10.8 % |
| **0.5** | 19× | $1,375 | 0.44 | 11.5 % |
| 1.0 | 13× | $964 | 0.56 | 10.7 % |

(before the rolling-peak fix below). Plateau 0.25–1.0 → we take the middle, 0.5σ.

**Drawdown governor peak.** Measured from the all-time peak, the governor kept a
16-month backtest de-risked for months (avg gross 0.16). The contest is 14 days, so the
governor now uses the **rolling 14‑day peak**, which gives the same behaviour during the
contest and an honest backtest. Result (v1.1): Sharpe 0.94, MaxDD 10.5 %, fees $3.1k;
all 13 single-parameter perturbations positive (README §6).

## Honest limitations

* 17 months is ~1 bull/bear cycle; PSR 0.78 is suggestive, not conclusive.
* The middle third (choppy bear) lost money — trend-following's known failure mode.
  The drawdown governor and half-size shorts are what bound it.
* The backtest assumes Roostoo fills a passive limit only when the next bar trades
  *through* it; live fill behaviour is monitored and the assumption revisited.
