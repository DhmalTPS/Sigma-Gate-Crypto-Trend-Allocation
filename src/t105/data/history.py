"""Historical OHLCV from Binance's public market-data mirror.

Roostoo's mock exchange mirrors Binance spot prices (verified: BTC/USD on
Roostoo vs BTCUSDT on Binance differ by ~1bp), so Binance klines are a
faithful research proxy and a warm-start source for the live bot. The
organizer explicitly allows external data sources.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def binance_symbol(roostoo_pair: str) -> str:
    """'BTC/USD' -> 'BTCUSDT'."""
    return roostoo_pair.split("/")[0] + "USDT"


def fetch_klines(symbol: str, interval: str = "1h", start_ms: int | None = None,
                 end_ms: int | None = None, session: requests.Session | None = None,
                 limit: int = 1000) -> pd.DataFrame:
    """Page through klines in [start_ms, end_ms). Returns UTC-indexed frame."""
    s = session or requests.Session()
    step = _INTERVAL_MS[interval]
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = start_ms or end_ms - 1000 * step
    rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cursor,
                  "endTime": end_ms - 1, "limit": limit}
        for attempt in range(4):
            try:
                r = s.get(BINANCE_URL, params=params, timeout=15)
                if r.status_code == 400:      # unknown symbol
                    return pd.DataFrame()
                r.raise_for_status()
                batch = r.json()
                break
            except requests.RequestException as e:
                log.warning("klines %s attempt %d failed: %s", symbol, attempt, e)
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"klines download failed for {symbol}")
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + step
        if len(batch) < limit:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=_COLS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time")[["open", "high", "low", "close", "volume", "quote_volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Drop the still-forming bar: only closed bars are information we could have had.
    now = pd.Timestamp.now(tz="UTC")
    df = df[df.index + pd.Timedelta(milliseconds=step) <= now]
    return df


def load_universe_history(pairs: list[str], interval: str, days: int, cache_dir: Path,
                          refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Download (or load cached) history for each Roostoo pair."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    def one(pair: str) -> tuple[str, pd.DataFrame]:
        s = requests.Session()
        f = cache_dir / f"{binance_symbol(pair)}_{interval}.csv.gz"
        if f.exists() and not refresh:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            last = int(df.index[-1].timestamp() * 1000) + _INTERVAL_MS[interval]
            if end_ms - last > _INTERVAL_MS[interval]:
                new = fetch_klines(binance_symbol(pair), interval, last, end_ms, s)
                df = pd.concat([df, new]).sort_index()
                df = df[~df.index.duplicated(keep="last")]
        else:
            df = fetch_klines(binance_symbol(pair), interval, start_ms, end_ms, s)
        if df.empty:
            log.warning("no Binance history for %s; excluded", pair)
            return pair, df
        df.to_csv(f, compression="gzip")
        return pair, df

    def safe(pair: str) -> tuple[str, pd.DataFrame]:
        try:
            return one(pair)
        except Exception as e:  # one bad symbol / network blip must not kill the batch
            log.error("history for %s failed: %s", pair, e)
            return pair, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(safe, pairs))
    return {p: df for p, df in results if not df.empty}


def to_panel(hist: dict[str, pd.DataFrame], field: str = "close") -> pd.DataFrame:
    """Wide panel (time x pair) of one field, aligned on a common UTC index."""
    panel = pd.DataFrame({p: df[field] for p, df in hist.items()})
    return panel.sort_index()
