"""Typed configuration loading (competition rules + strategy/policy/exec/risk params)."""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import yaml

from .backtest.engine import ExecParams
from .portfolio.policy import PolicyParams
from .risk.compliance import RiskLimits
from .strategy.core import StrategyParams


def _apply(dc, overrides: dict | None):
    if not overrides:
        return dc
    names = {f.name for f in fields(dc)}
    unknown = set(overrides) - names
    if unknown:
        raise KeyError(f"unknown config keys for {type(dc).__name__}: {sorted(unknown)}")
    vals = {k: (tuple(v) if isinstance(v, list) else v) for k, v in overrides.items()}
    return replace(dc, **vals)


def load(root: Path, strategy_file: str = "config/strategy.yaml") -> dict:
    comp = yaml.safe_load((root / "config" / "competition.yaml").read_text())
    st = yaml.safe_load((root / strategy_file).read_text()) or {}
    fees = comp["fees"]
    ex = _apply(ExecParams(maker_fee=fees["maker"], taker_fee=fees["taker"], short_fee=fees["short_open"]),
                st.get("execution"))
    pol = _apply(PolicyParams(), st.get("policy"))
    if not comp["instruments"]["short"]:
        pol = replace(pol, allow_short=False)
    if pol.max_gross > comp["instruments"]["leverage"]:
        raise ValueError("policy.max_gross exceeds competition leverage limit")
    return {"competition": comp, "strategy": _apply(StrategyParams(), st.get("strategy")),
            "policy": pol, "execution": ex, "risk": _apply(RiskLimits(), st.get("risk")),
            "live": st.get("live", {}), "version": st.get("version", "unversioned")}
