"""Pre-trade risk + competition-rule gate. Every order passes through `check`.

Rules enforced (competition.yaml):
  * spot only, 1x: post-trade gross exposure (longs + short collateral) <= NAV
  * allowed universe only (no tokenised equities we did not research)
  * no HFT: hard cap on orders per cycle and per hour; decisions are hourly
  * no market making: we never hold simultaneous resting BUY and SELL orders
    on the same pair, and every order reduces |target - current| (directional)
  * no arbitrage: single venue, no simultaneous offsetting legs on related pairs
Plus our own limits: max single-order size, and the circuit breakers.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_gross: float = 1.0
    max_order_frac: float = 0.25      # single order <= 25% of NAV
    max_orders_per_cycle: int = 40
    max_orders_per_hour: int = 80
    breaker_dd: float = 0.15          # drawdown from peak that triggers flatten + cooldown
    breaker_cooldown_h: float = 12.0
    stale_data_s: float = 3 * 3600    # never trade on market data older than this
    max_api_failures: int = 8         # consecutive failures -> pause new risk


class Compliance:
    def __init__(self, limits: RiskLimits, universe: set[str], audit):
        self.L = limits
        self.universe = universe
        self.audit = audit
        self.sent = deque()

    def _rate_ok(self) -> bool:
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        return len(self.sent) < self.L.max_orders_per_hour

    def check(self, pair: str, side: str, usd: float, nav: float, gross_after: float,
              resting_sides: dict[str, set], reduces_gap: bool, cycle_count: int,
              risk_reducing: bool = False) -> tuple[bool, str]:
        why = None
        if pair not in self.universe:
            why = "pair not in researched universe"
        elif not reduces_gap:
            why = "order does not move position toward target (not directional)"
        elif cycle_count >= self.L.max_orders_per_cycle:
            why = "per-cycle order cap (anti-HFT)"
        elif not self._rate_ok():
            why = "hourly order cap (anti-HFT)"
        elif usd > self.L.max_order_frac * nav and not risk_reducing:
            why = f"order {usd:.0f} > {self.L.max_order_frac:.0%} NAV"
        elif gross_after > self.L.max_gross + 1e-6 and not risk_reducing:
            why = f"post-trade gross {gross_after:.3f} > 1x (no leverage)"
        elif side in ("BUY", "SELL") and ({"BUY", "SELL"} - {side}) & resting_sides.get(pair, set()):
            why = "opposite resting order on pair (would look like market making)"
        if why:
            self.audit.write("compliance", {"pair": pair, "side": side, "usd": round(usd, 2),
                                            "blocked": why})
            return False, why
        self.sent.append(time.time())
        return True, ""
