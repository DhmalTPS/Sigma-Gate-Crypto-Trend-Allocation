# Team105 · Threshold Crushers (IIT Mandi)

Autonomous trading bot for the **HK vs AU vs IN Quant Trading Hackathon 2026**
(Roostoo mock exchange · Susquehanna · AWS). It runs unattended on AWS EC2, trades
only through the Roostoo REST API, and logs every decision, order and fill.

> **The idea in one paragraph.** With 0.10 % taker / 0.05 % maker fees and only
> 14 days of trading, the main way to lose is *overtrading*. We measured that the
> strongest short-horizon signal in crypto (cross-sectional reversal, t ≈ 17) loses
> money net of fees in **all 64** configurations we tried. So we trade
> **slowly**: a volatility-targeted book of the five most liquid coins, where each
> coin's direction comes from an ensemble of 7/14/30-day trend gates with a
> dead-band. Shorts are half size, a drawdown governor caps losses, and execution
> tries passive limit orders first. Each design choice is backed by a
> statistical test in [`docs/RESEARCH_LOG.md`](docs/RESEARCH_LOG.md), including
> the rejected ideas.

---

## 1. Competition constraints (hard-coded in `config/competition.yaml`)

| Rule | How the bot enforces it |
|---|---|
| $100,000 mock portfolio | NAV from the exchange each cycle, never assumed |
| Spot only, 1× long **and** short, no leverage | `max_gross 0.95`; compliance gate rejects any order with post-trade gross > 1×; shorts use Roostoo's collateralised `/v6` endpoints (collateral ≤ free cash) |
| Taker 0.10 %, maker 0.05 % | Charged in the backtester; the execution policy tries maker first |
| No HFT | Hourly decisions; ≤ 20 orders/cycle, ≤ 40/hour; client-side rate limiter (30 req/min) |
| No market making | One side per pair at a time; every order moves the position *toward* the target; the gate blocks opposite resting orders |
| No arbitrage | One venue, directional positions only |
| Autonomous, no manual API calls | All orders come from `live/runner.py`; `smoke_test.py` only works with the testing keys; audit logs record `OrderSource: PUBLIC_API` |
| ≥ 8 active trading days | Daily activity guard: if nothing has filled by 18:00 UTC, the bot makes the smallest strategy-consistent trade |
| Open source, traceable commits | Every change goes through [`docs/CHANGE_CONTROL.md`](docs/CHANGE_CONTROL.md) and [`CHANGELOG.md`](CHANGELOG.md); the config `version` is stamped on every log line |

Note: the generic Roostoo API docs show $50k and 0.012 %/0.008 % fees as examples.
We use the competition values above. We also verified on the testing account that
real fills charge 0.10 % (taker) and 0.05 % (maker).

## 2. How we arrived at the strategy (research summary)

All research is reproducible (`research/*.py`) on 540 days of hourly data for all
88 Roostoo pairs (Roostoo mirrors Binance spot; BTC matches to about 1 bp).

1. **Signal term structure (R1).** Rank ICs with non-overlapping t-stats:
   reversal is strong at 1–12 h; trend pays only at ≥ 1 week; residual
   momentum is noise; timing total exposure with market trend has a *negative*
   relationship.
2. **Fees kill fast signals (R2).** Reversal earns about +49 %/yr gross at 1 h but
   costs about 456 %/yr in maker fees. Rejected.
3. **Concentrate in majors; target volatility (R3).** Altcoins bled against BTC
   (median coin −57 % vs BTC −19 %). Vol targeting halved tail drawdowns.
4. **Per-coin trend-gate ensemble (R4).** Single lookbacks are fragile
   (Sharpe ranges from −0.6 to 1.0 by lookback). The 7/14/30-day ensemble is
   smooth under ±25 % perturbation (0.59 / 0.68 / 0.80), so we use the centred
   ensemble rather than the best-looking row.
5. **Half-size shorts (R5)** cut max drawdown from 21 % to 16 % and raised Sharpe.
6. **Gate dead-band (R6).** In the full hourly engine, plain sign gates flipped
   whenever a trend sat near zero (110× NAV turnover). A ±0.5σ dead-band fixes
   this; see §6.

## 3. Strategy

For each coin *i* in {BTC, ETH, SOL, BNB, XRP} and each lookback *L* ∈ {168, 336, 720} h:

```
z_iL = log(P_t / P_{t-L}) / (σ_i,1h · √L)            vol-normalised trend
g_iL = +1 if z > +0.5, −1 if z < −0.5, else previous g      (dead-band)
s_i  = mean_L g_iL  ∈ {−1, −⅓, +⅓, +1}
```

**Portfolio construction** (`portfolio/policy.py`, `sizing: absolute`):

```
base_i = (1/σ_i) / Σ_j (1/σ_j)                       inverse-vol basket
scale  = min(1, 30% / σ_basket)                      σ from a shrunk covariance matrix
w_i    = s_i · base_i · scale · (½ if s_i < 0)       half-size shorts
w_i    = clip(w_i, ±40% BTC/ETH, ±30% others);  Σ|w| ≤ 95%
w      = w · DD_multiplier                           1 → 0.25 as drawdown goes 3% → 10%
```

The book is fully invested at the target volatility only when every gate is on.
As trends break, exposure **falls**; it is never re-normalised back to 100 %.
Covariance (not the sum of single-coin risks) sets the scale, because crypto
correlations are about 0.8. A no-trade band (|Δw| > 3 % NAV or > 20 % of the
target) stops the bot paying fees to chase small weight drift.

## 4. Trading engine

```
every hour (+90 s, after the bar closes)
  1. cancel our resting limits from the last hour (and remember which went unfilled)
  2. update bars: Binance klines → fallback to bars built from Roostoo snapshots
  3. read the TRUE book from the exchange: /v3/balance + /v6/short_positions
  4. strategy.compute()   — same code as the backtest
  5. policy.target_weights()
  6. execute: risk-reducing legs first; long legs as passive LIMIT at the touch;
     after 1 unfilled hour → MARKET; short legs via /v6 (always 0.10 %)
  7. activity guard, shadow portfolios, decision log, atomic state save
every 60 s: ticker (one request for all pairs)
every 5 min: NAV snapshot + intra-hour circuit breaker
```

**Failure handling** (`api/client.py`, `execution/broker.py`):

* **Lost response after an order:** never re-sent. The bot looks the order up
  with `query_order` first, which prevents double fills.
* **Restart / crash:** systemd restarts the bot within 20 s. Positions are
  rebuilt from the exchange, so the bot never "forgets" a position (verified
  live: after a restart it placed 0 orders).
* **Clock drift:** server-time offset re-synced every 10 min; chrony runs on EC2.
* **Stale data:** a pair is excluded if its bar close differs from Roostoo's live
  price by more than 2 %; no trading at all on data older than 3 h.
* **Roostoo errors:** returned as HTTP 200 with `Success:false`, so the bot always
  checks the flag; "no order matched" means empty, not failure.

## 5. Fees and risk management

* **Fees:** maker first (5 bps), taker only after one hour of patience or when
  reducing risk urgently. The backtester counts a maker fill **only if the next
  bar trades through the limit by 2 bps**, which builds in adverse selection.
  Unfilled orders pay taker plus the spread implied by the price tick.
* **Universe hygiene:** pairs whose one-tick spread exceeds 5 bps are excluded
  (e.g. PEPE's tick alone is about 22 bps).
* **Risk layers:** vol target → per-coin caps → gross ≤ 95 % → drawdown governor →
  25 % NAV per-order cap (large orders are split across cycles) → **circuit breaker**
  (15 % drawdown: go flat for 12 h) → API-failure and stale-data pauses.
* **Why drawdown gets extra weight:** the composite score is 0.4 Sortino +
  0.3 Sharpe + 0.3 Calmar. Over a 14-day window an annualised Calmar can be
  far larger than the other two, so max drawdown is the most leveraged
  quantity in the score.

## 6. Backtest (production config, full engine, net of fees)

Period 2025‑05‑25 → 2026‑10‑02 (≈ 16 months, hourly decisions, $100k start, all fees):

| Metric | **Team105 v1.1** | EW majors buy & hold |
|---|---|---|
| Total return | **+20.9 %** | −8.7 % |
| Annualised vol | 16.4 % | ≈ 55 % |
| Sharpe / Sortino | **0.94 / 1.33** | 0.12 / 0.17 |
| Max drawdown | **10.5 %** | 63.4 % |
| Calmar (raw) | 1.99 | −0.14 |
| Probabilistic Sharpe (P[SR > 0]) | 0.86 | — |
| Bootstrap P(Sharpe > 0), 90 % CI | 0.85, [−0.48, 2.38] | — |
| Fees paid · turnover | $3,133 · 41× NAV | — |
| Maker share of notional | 46 % | — |

**Contest-length view.** Every 14‑day window in the backtest (481 windows):
10th / 50th / 90th percentile return **−2.6 % / −0.3 % / +4.4 %**; 90th‑percentile
drawdown **4.9 %**, worst drawdown 6.1 %. Outcomes are positively skewed: small
losses, larger wins.

**Fragility table** (each row changes one parameter; base Sharpe 0.94):

| Change | Sharpe | MaxDD | | Change | Sharpe | MaxDD |
|---|---|---|---|---|---|---|
| lookbacks ×0.75 | 1.27 | 10.0 % | | no shorts | 0.80 | 16.5 % |
| lookbacks ×1.25 | 1.26 | 9.2 % | | full-size shorts | 0.91 | 14.8 % |
| vol half-life 36h | 0.97 | 10.9 % | | band 1.5 % | 0.89 | 12.0 % |
| vol half-life 144h | 0.88 | 11.4 % | | band 6 % | 0.56 | 17.4 % |
| vol target 20 % | 0.90 | 9.1 % | | DD soft 2 % | 0.90 | 10.4 % |
| vol target 40 % | 0.71 | 14.4 % | | DD soft 5 % | 0.91 | 11.6 % |
| taker-only execution | 0.88 | 10.9 % | | | | |

Every perturbation stays positive with no cliffs. The chosen values are
not the in-sample best (lookbacks ×0.75 would score higher); we keep the
centred, pre-registered values.

How to read it: these numbers come from the same `compute` → `target_weights`
path that runs live, with conservative fills. We also report the distribution
of 14-day windows, a stationary-bootstrap Sharpe interval and a
parameter-perturbation table (`reports/backtest/<version>/`), because one
backtest number over a single market cycle shows little on its own.

## 7. Live operations during the 14 days

* **Shadow portfolios** (no-shorts, 20 % and 40 % vol target) run alongside the real
  book on the same signals, giving counterfactual evidence before any change.
* **Change control:** five tiers, from infra fixes (always allowed) to
  strategy replacement (needs very strong evidence); never retune because of
  one bad day.
* Logs: `logs/deployment/<UTC day>/{api,decision,order,nav,health,compliance}.jsonl`.

## 8. Repository map

```
config/          competition.yaml (rules) · strategy.yaml (versioned parameters)
src/t105/api/    signed client, credentials (env/files, never logged), exchange metadata
src/t105/data/   history download, universe
src/t105/strategy/ signals, regime features, ensemble core
src/t105/portfolio/ path-dependent policy (hysteresis, sizing, vol target, DD governor, bands)
src/t105/backtest/ execution-aware backtester
src/t105/execution/ broker: exchange-truth book, orders, ambiguous-response resolution
src/t105/risk/   compliance gate + limits
src/t105/live/   market data + autonomous runner
research/        every experiment behind the design
scripts/         download_data · run_backtest · smoke_test (testing keys only) · run_bot
deployment/      install.sh · systemd unit · .env.example · healthcheck.sh
tests/           signing vs Roostoo doc vector, causality (no look-ahead), policy invariants,
                 execution planner (flip long→short, cover→long, slicing), compliance
```

## 9. Reproduce

```bash
pip install -r requirements.txt
python scripts/download_data.py --days 540
python -m pytest -q
python scripts/run_backtest.py
python scripts/smoke_test.py                       # read-only, testing account
python scripts/run_bot.py --profile testing --dry-run --once
```

Deploy on EC2: see `deployment/install.sh` (the keys go in `/etc/t105/deployment.env`, never in git).

## 10. Limitations (honest)

* 17 months is about one market cycle; the edge is suggestive, not proven. Trend
  following loses in choppy markets (our middle third lost money); drawdown control is
  what bounds that.
* We assume Roostoo's limit fills behave like "trade-through" fills; live maker
  ratio and slippage are monitored in the logs.
* External data dependency (Binance klines) has a Roostoo-snapshot fallback.
