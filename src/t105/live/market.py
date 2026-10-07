"""Live market data: hourly OHLCV panels for the strategy + Roostoo top-of-book.

Primary bar source: Binance public klines (Roostoo mirrors Binance prices).
Fallback: hourly bars built from our own minute snapshots of the Roostoo ticker,
so the bot keeps working if Binance is unreachable from the VM.
Integrity: a pair whose Binance close deviates >2% from Roostoo's last price is
excluded from trading for that cycle (stale/mismatched data must never drive orders).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from ..api.client import RoostooClient
from ..data.history import fetch_with_fallback

log = logging.getLogger(__name__)


class LiveMarket:
    def __init__(
        self, client: RoostooClient, pairs: list[str], lookback_h: int = 24 * 60, local_dir: Path | None = None
    ):
        self.local_dir = local_dir
        self.sources: dict[str, str] = {}
        self.last_update_src: dict[str, str] = {}
        self.c = client
        self.pairs = pairs
        self.lookback_h = lookback_h
        self.bars: dict[str, pd.DataFrame] = {}
        self.ticker: dict[str, dict] = {}
        self.ticker_ts = 0.0
        self.snap = defaultdict(list)  # hour -> pair -> prices (fallback bar builder)
        self.binance_ok = True

    # ------------------------------------------------------------ Roostoo
    def refresh_ticker(self) -> bool:
        r = self.c.ticker()
        if not r.ok:
            return False
        self.ticker = r.data.get("Data", {})
        self.ticker_ts = time.time()
        hour = pd.Timestamp.now(tz="UTC").floor("h")
        for p in self.pairs:
            d = self.ticker.get(p)
            if d and d.get("LastPrice"):
                self.snap[(hour, p)].append(float(d["LastPrice"]))
        # keep 3 hours of snapshots
        for k in [k for k in self.snap if k[0] < hour - pd.Timedelta(hours=3)]:
            del self.snap[k]
        return True

    def mids(self) -> dict[str, float]:
        out = {}
        for p, d in self.ticker.items():
            b, a = d.get("MaxBid"), d.get("MinAsk")
            if b and a:
                out[p] = (float(b) + float(a)) / 2
            elif d.get("LastPrice"):
                out[p] = float(d["LastPrice"])
        return out

    def top(self, pair: str) -> tuple[float, float] | None:
        d = self.ticker.get(pair)
        if not d or not d.get("MaxBid") or not d.get("MinAsk"):
            return None
        return float(d["MaxBid"]), float(d["MinAsk"])

    # ------------------------------------------------------------ bars
    def bootstrap(self) -> None:
        end = int(time.time() * 1000)
        start = end - self.lookback_h * 3_600_000
        for p in self.pairs:
            df, src = fetch_with_fallback(p, start, end, self.local_dir)
            self.sources[p] = src
            if df.empty:
                log.error("bootstrap %s: no history from any source", p)
                continue
            self.bars[p] = df.iloc[-self.lookback_h :]

    def update_bars(self) -> None:
        """Append newly closed hourly bars (Binance, else Roostoo-snapshot fallback)."""
        now_h = pd.Timestamp.now(tz="UTC").floor("h")
        fails = 0
        for p in self.pairs:
            df = self.bars.get(p)
            last = df.index[-1] if df is not None and len(df) else now_h - pd.Timedelta(hours=self.lookback_h)
            if last >= now_h - pd.Timedelta(hours=1):
                continue
            new, src = fetch_with_fallback(
                p, int((last + pd.Timedelta(hours=1)).timestamp() * 1000), int(time.time() * 1000)
            )
            if src != "binance":
                fails += 1
            # Only bars NEWER than what we hold count. (The bundled-snapshot fallback returns old
            # data; treating it as "new" blocked the Roostoo-snapshot bar and left the coin stale.)
            if not new.empty:
                new = new[new.index > last]
            if new.empty:
                new = self._snapshot_bar(p, now_h - pd.Timedelta(hours=1))
                src = "roostoo_snapshot" if not new.empty else "stale"
            self.last_update_src[p] = src
            if not new.empty:
                df = pd.concat([df, new]) if df is not None else new
                df = df[~df.index.duplicated(keep="last")].sort_index()
                self.bars[p] = df.iloc[-self.lookback_h :]
        self.binance_ok = fails < len(self.pairs) // 2

    def _snapshot_bar(self, pair: str, hour: pd.Timestamp) -> pd.DataFrame:
        px = self.snap.get((hour, pair))
        if not px:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "open": [px[0]],
                "high": [max(px)],
                "low": [min(px)],
                "close": [px[-1]],
                "volume": [np.nan],
                "quote_volume": [np.nan],
            },
            index=[hour],
        )

    def panels(self) -> dict[str, pd.DataFrame]:
        out = {}
        for k in ("close", "high", "low", "quote_volume"):
            out[k] = pd.DataFrame({p: df[k] for p, df in self.bars.items()})
        full = pd.date_range(out["close"].index.min(), out["close"].index.max(), freq="1h", tz="UTC")
        for k in out:
            out[k] = out[k].reindex(full)
        out["close"] = out["close"].ffill(limit=3)
        # snapshot-fallback bars have unknown volume: carry the last known value forward
        out["quote_volume"] = out["quote_volume"].ffill(limit=6).fillna(0.0)
        return out

    def integrity_mask(self, close_row: pd.Series, tol: float = 0.02) -> pd.Series:
        """True where the bar data agrees with Roostoo's live price."""
        mids = self.mids()
        ok = {}
        for p, c in close_row.items():
            m = mids.get(p)
            ok[p] = bool(m and c and abs(c / m - 1) < tol)
        return pd.Series(ok)

    def data_age_s(self) -> float:
        if not self.bars:
            return float("inf")
        last = max(df.index[-1] for df in self.bars.values())
        return (pd.Timestamp.now(tz="UTC") - (last + pd.Timedelta(hours=1))).total_seconds()
