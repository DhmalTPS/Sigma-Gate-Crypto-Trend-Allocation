"""Universe definition + loading the research panel from cache."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .history import binance_symbol

# Tokenised equities listed on Roostoo (suffix "B"): short histories, equity-market
# hours drive their price discovery, and our crypto signals were not designed for them.
TOKENISED_EQUITIES = {
    "AMDB", "SNDKB", "QCOMB", "NBISB", "MSFTB", "GOOGLB", "INTCB", "PLTRB", "METAB", "CRCLB",
    "MUB", "SKHYB", "MSTRB", "CBRSB", "LITEB", "TSLAB", "COINB", "NVDAB", "GLWB", "WDCB", "SPCXB",
}
# Gold token: near-zero crypto beta, fine as an asset but excluded from the crypto
# factor so it does not distort the market-return estimate.
EXCLUDE = TOKENISED_EQUITIES | {"PAXG"}


def crypto_pairs(exchange_info: dict) -> list[str]:
    return sorted(p for p, v in exchange_info["TradePairs"].items()
                  if v.get("CanTrade") and p.split("/")[0] not in EXCLUDE)


def price_precisions(exchange_info: dict) -> dict[str, int]:
    return {p: int(v["PricePrecision"]) for p, v in exchange_info["TradePairs"].items()}


def load_panels(root: Path, interval: str = "1h") -> tuple[dict[str, pd.DataFrame], dict]:
    """Returns ({'close','high','low','quote_volume'} -> time x pair frames, exchange_info)."""
    cache = root / "data" / "cache"
    info = json.loads((cache / "exchange_info.json").read_text())
    fields = {"close": {}, "high": {}, "low": {}, "quote_volume": {}}
    for p in crypto_pairs(info):
        f = cache / f"{binance_symbol(p)}_{interval}.csv.gz"
        if not f.exists():
            continue
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        for k in fields:
            fields[k][p] = df[k]
    close = pd.DataFrame(fields["close"]).sort_index()
    full = pd.date_range(close.index[0], close.index[-1], freq="1h", tz="UTC")
    panels = {k: pd.DataFrame(v).reindex(full) for k, v in fields.items()}
    # forward-fill at most 3h of missing prints (exchange maintenance); never across longer gaps
    panels["close"] = panels["close"].ffill(limit=3)
    for k in ("high", "low"):
        panels[k] = panels[k].fillna(panels["close"])
    panels["quote_volume"] = panels["quote_volume"].fillna(0.0)
    return panels, info
