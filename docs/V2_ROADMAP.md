# v2 Roadmap — multi-sleeve architecture (design only, not deployed)

## Idea
Run independent **sleeves** inside one bot, each with its own signals, risk budget and execution rules,
combined under one portfolio-level risk layer (1× gross, drawdown governor, compliance gate):

| Sleeve | Assets | Signal | Risk budget |
|---|---|---|---|
| Crypto trend (live today) | BTC, ETH, SOL, BNB, XRP | multi-lookback trend gates + dead-band | vol target 30 % |
| Equity-token sleeve (candidate) | Roostoo tokenised US stocks (NVDAB, TSLAB, …) | to be researched (market-hours aware) | e.g. ≤ 20 % NAV |

Required before any equity sleeve goes live: market-hours/weekend handling (tokens trade 24/7, underlying
stocks do not), earnings-gap risk limits, token-vs-underlying tracking checks, multi-year validation on real
stock history (token history is only ~3–4 months), and a combined-portfolio test.

## Evidence so far — `research/equity_sleeve.py` (R8), 13 tokens, 2026-06-11 → 2026-10-02
- Correlation with the crypto basket: +0.53 hourly / +0.46 daily → limited diversification.
- 32 % of token variance falls in the US cash session (27 % of hours); weekends carry ~3 %.
- Trend gates on tokens lose after fees (Sharpe −1.9 to −3.0; negative in the first half).
- Equal-weight hold +22 % (Sharpe 2.4, MaxDD 19 %) = one AI-stock rally regime, not a repeatable rule.
- 80 % crypto + 20 % token sleeve vs crypto alone, same period: return +5.2 % → +3.3 %, Sharpe 1.29 → 0.98.

**Decision:** not deployed during the contest. Revisit with multi-year stock data and an equity-specific signal.
