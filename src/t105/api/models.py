"""Exchange metadata + order-validity helpers (never hard-code precision)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class PairInfo:
    pair: str
    coin: str
    price_precision: int
    amount_precision: int
    min_notional: float      # docs: OK if price*amount > MiniOrder
    can_trade: bool

    def floor_qty(self, qty: float) -> float:
        q = Decimal(str(abs(qty))).quantize(Decimal(1).scaleb(-self.amount_precision), rounding=ROUND_DOWN)
        return float(q)

    def fmt_qty(self, qty: float) -> str:
        return f"{self.floor_qty(qty):.{self.amount_precision}f}"

    def round_price(self, px: float, side: str) -> float:
        """Round a limit price to the tick in the *passive* direction:
        buys round down, sells round up -- never accidentally cross."""
        step = 10 ** -self.price_precision
        f = math.floor if side == "BUY" else math.ceil
        return round(f(px / step + 1e-9 if side == "BUY" else px / step - 1e-9) * step, self.price_precision)

    def fmt_price(self, px: float) -> str:
        return f"{px:.{self.price_precision}f}"

    def valid(self, qty: float, px: float, buffer: float = 1.05) -> bool:
        return self.can_trade and qty > 0 and qty * px > self.min_notional * buffer


def parse_exchange_info(data: dict) -> dict[str, PairInfo]:
    out = {}
    for pair, v in data.get("TradePairs", {}).items():
        out[pair] = PairInfo(pair, v.get("Coin", pair.split("/")[0]), int(v["PricePrecision"]),
                             int(v["AmountPrecision"]), float(v.get("MiniOrder", 1.0)),
                             bool(v.get("CanTrade", False)))
    return out
