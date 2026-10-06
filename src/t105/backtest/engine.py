"""Execution-aware hourly backtester.

Uses the *same* strategy.compute + portfolio.policy code as the live bot. What
it models, deliberately conservatively:

  * Competition fees: long-side maker 5 bps / taker 10 bps; shorts 10 bps
    on both legs (the /v6 short endpoints charge 0.1% regardless of order type).
  * Maker orders: a passive limit at the decision-time close. It fills ONLY if
    the next bar trades *through* the limit by `touch_bps` (fills at the touch
    are not assumed). This builds in adverse selection: buy limits fill exactly
    when price drops. After `maker_patience` failed hours the remainder is sent
    as a taker order.
  * Taker orders: fill at close +/- half the tick-implied spread + slippage.
  * Signed accounting (shorts add proceeds to cash, liability marked to market),
    which is equivalent to Roostoo's collateral mechanics.
  * Decisions at bar t use information up to the close of t only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..portfolio.policy import PolicyParams, PolicyState, target_weights
from ..strategy.core import StrategyOutput


@dataclass
class ExecParams:
    maker_fee: float = 0.0005
    taker_fee: float = 0.0010
    short_fee: float = 0.0010
    touch_bps: float = 2.0
    slippage_bps: float = 1.0
    maker_patience: int = 1  # hours a limit may rest before crossing
    use_maker: bool = True
    decision_every: int = 1  # hours between strategy decisions


@dataclass
class BacktestResult:
    nav: pd.Series
    weights: pd.DataFrame
    trades: pd.DataFrame
    diag: pd.DataFrame
    fees: float
    turnover: float
    maker_ratio: float = 0.0
    meta: dict = field(default_factory=dict)


def run(
    out: StrategyOutput,
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    pol: PolicyParams,
    ex: ExecParams,
    initial: float = 100_000.0,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    half_spread_bps: pd.Series | None = None,
) -> BacktestResult:
    idx = close.index
    if start is not None:
        idx = idx[idx >= start]
    if end is not None:
        idx = idx[idx < end]
    cols = list(close.columns)
    C = close.reindex(idx)[cols].values
    Hh = high.reindex(idx)[cols].values
    Ll = low.reindex(idx)[cols].values
    hs = (half_spread_bps.reindex(cols).fillna(1.0).values if half_spread_bps is not None else np.ones(len(cols))) / 1e4
    slip = ex.slippage_bps / 1e4
    touch = ex.touch_bps / 1e4

    qty = np.zeros(len(cols))
    cash = initial
    st = PolicyState(peak_nav=initial)
    pending = {}  # j -> dict(qty, limit, age)
    navs, wrows, diags, trades = [], [], [], []
    fees = turnover = maker_notional = taker_notional = 0.0

    alpha, vol, regime, rets = out.alpha, out.vol, out.regime, out.returns

    for ti, t in enumerate(idx):
        px = C[ti]
        valid = ~np.isnan(px)
        # ---- 1) resolve resting maker orders against this bar's range
        for j in list(pending):
            o = pending[j]
            if not valid[j]:
                continue
            filled = (o["qty"] > 0 and Ll[ti, j] <= o["limit"] * (1 - touch)) or (
                o["qty"] < 0 and Hh[ti, j] >= o["limit"] * (1 + touch)
            )
            if filled:
                notional = abs(o["qty"]) * o["limit"]
                cash -= o["qty"] * o["limit"] + notional * ex.maker_fee
                qty[j] += o["qty"]
                fees += notional * ex.maker_fee
                turnover += notional
                maker_notional += notional
                trades.append((t, cols[j], o["qty"], o["limit"], "maker"))
                del pending[j]
            else:
                o["age"] += 1

        mark = np.where(valid, px, 0.0)
        nav = cash + float(np.nansum(qty * mark))
        navs.append(nav)

        if ti % ex.decision_every != 0 or ti == len(idx) - 1:
            continue
        # ---- 2) strategy decision with information up to close t
        cur_w = pd.Series(np.where(valid, qty * mark / nav, 0.0), index=cols)
        a_row = alpha.loc[t].reindex(cols) if t in alpha.index else pd.Series(np.nan, index=cols)
        # never open into an asset with no price this bar
        a_row[~valid] = np.nan
        w_t, d = target_weights(
            a_row, vol.loc[t].reindex(cols), regime.loc[t], rets.loc[:t].tail(pol.cov_window), cur_w, nav, st, pol
        )
        d["t"] = t
        diags.append(d)
        w_t = w_t.reindex(cols).fillna(0.0).values
        wrows.append((t, w_t.copy()))

        # ---- 3) orders
        for j in range(len(cols)):
            if not valid[j]:
                continue
            target_q = w_t[j] * nav / px[j]
            dq = target_q - qty[j]
            if abs(dq * px[j]) < pol.min_trade_usd and w_t[j] != 0:
                pending.pop(j, None)
                continue
            if abs(dq) < 1e-12:
                pending.pop(j, None)
                continue
            # a short leg (opening or covering) is always a /v6 taker
            crosses_short = (qty[j] < 0) or (target_q < 0)
            o = pending.get(j)
            patience_left = (o is None) or (o["age"] < ex.maker_patience)
            if ex.use_maker and not crosses_short and patience_left:
                age = 0 if o is None or np.sign(o["qty"]) != np.sign(dq) else o["age"]
                pending[j] = {"qty": dq, "limit": px[j], "age": age}
                continue
            pending.pop(j, None)
            side = np.sign(dq)
            fill = px[j] * (1 + side * (hs[j] + slip))
            fee_rate = ex.short_fee if crosses_short else ex.taker_fee
            notional = abs(dq) * fill
            cash -= dq * fill + notional * fee_rate
            qty[j] += dq
            fees += notional * fee_rate
            turnover += notional
            taker_notional += notional
            trades.append((t, cols[j], dq, fill, "taker"))

    nav_s = pd.Series(navs, index=idx[: len(navs)])
    W = pd.DataFrame([w for _, w in wrows], index=[t for t, _ in wrows], columns=cols)
    T = pd.DataFrame(trades, columns=["t", "pair", "qty", "price", "liquidity"])
    D = pd.DataFrame(diags).set_index("t") if diags else pd.DataFrame()
    tot = maker_notional + taker_notional
    return BacktestResult(nav_s, W, T, D, fees, turnover / initial, maker_notional / tot if tot else 0.0)
