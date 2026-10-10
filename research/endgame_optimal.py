"""R9: optimal end-game bet = maximise P(finish > 0) from x0 < 0, subject to a floor (top-20 cut).

Model. Log contest return X_t with constant exposure theta to the inverse-vol majors basket:
    dX = (theta*mu - 0.5*theta^2*sigma^2) dt + theta*sigma dW,   X_0 = x0
Policy: "goal-lock" -- trade until X hits G (>= 0, lock in: go flat) or L (floor: go flat), else hold to T.
Success probability u(t,x) = P(X_T >= 0 | X_t = x) solves the Kolmogorov backward PDE
    u_t + b u_x + 0.5 s^2 u_xx = 0  on (L,G),   u(t,G)=1, u(t,L)=0 (or 1{L>=0}), u(T,x)=1{x>=0}
(b = theta*mu - 0.5*theta^2*sigma^2, s = theta*sigma), solved by Crank-Nicolson.
Closed-form sanity check (mu=0, T->inf, Ito drift ignored): u = (x0-L)/(G-L)  (optional stopping).

Robustness: the PDE assumes Gaussian, continuous paths. We also run a stationary block bootstrap of REAL
hourly basket returns (fat tails, jumps, vol clustering; hourly monitoring => gap risk through barriers),
optionally rescaled to the current volatility, with taker fees on entry/exit.

usage: python research/endgame_optimal.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105.data.universe import load_panels  # noqa: E402

U = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]
X0 = -0.0247
FLOOR = -0.045
FEE = 0.0010


def basket_returns() -> pd.Series:
    P, _ = load_panels(ROOT)
    c = P["close"][U].dropna()
    r = c.pct_change().dropna()
    vol = np.sqrt((np.log(c).diff() ** 2).ewm(halflife=72).mean())
    w = (1 / vol).div((1 / vol).sum(axis=1), axis=0).shift(1).loc[r.index]
    return (w * r).sum(axis=1).dropna()


def pde_success(theta, sig_d, mu_d, days, G, L=FLOOR, x0=X0, nx=801, nt=2000):
    """Crank-Nicolson for u_t + b u_x + .5 s^2 u_xx = 0 backward from T."""
    s2 = (theta * sig_d) ** 2
    b = theta * mu_d - 0.5 * s2
    hi = G if G is not None else 0.25
    x = np.linspace(L, hi, nx)
    dx = x[1] - x[0]
    dt = days / nt
    u = (x >= 0).astype(float)
    a = 0.5 * s2 / dx**2
    c1 = b / (2 * dx)
    lo_c, di_c, up_c = a - c1, -2 * a, a + c1
    n = nx - 2
    Ai = np.zeros((3, n))  # banded implicit matrix (I - dt/2 A)
    Ai[0, 1:] = -0.5 * dt * up_c
    Ai[1, :] = 1 - 0.5 * dt * di_c
    Ai[2, :-1] = -0.5 * dt * lo_c
    from scipy.linalg import solve_banded

    top = 1.0 if G is not None else None
    for _ in range(nt):
        rhs = u[1:-1] + 0.5 * dt * (lo_c * u[:-2] + di_c * u[1:-1] + up_c * u[2:])
        u_new = u.copy()
        u_new[0] = 0.0
        u_new[-1] = top if top is not None else 1.0
        rhs[0] += 0.5 * dt * lo_c * u_new[0]
        rhs[-1] += 0.5 * dt * up_c * u_new[-1]
        u_new[1:-1] = solve_banded((1, 1), Ai, rhs)
        u = u_new
    return float(np.interp(x0, x, u))


def mc(br: np.ndarray, theta, days, G, n=6000, block=24, scale=1.0, seed=11):
    """Bootstrap paths of real hourly basket returns; hourly monitoring; fees on entry and exit."""
    rng = np.random.default_rng(seed)
    H = 24 * days
    N = len(br)
    starts = rng.integers(0, N, size=(n, H // block + 1))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n, -1)[:, :H] % N
    R = br[idx] * scale
    x = np.full(n, np.log1p(X0) - theta * FEE)  # entry fee
    live = np.ones(n, bool)
    for h in range(H):
        x = np.where(live, x + np.log1p(theta * R[:, h]), x)
        hit_g = live & (G is not None) & (x >= np.log1p(G if G is not None else 9))
        hit_l = live & (x <= np.log1p(FLOOR))
        stop = hit_g | hit_l
        x = np.where(stop, x - theta * FEE, x)  # exit fee
        live &= ~stop
    ret = np.expm1(x)
    return (ret > 0).mean(), (ret < -0.05).mean(), np.median(ret), ret.min()


def main() -> None:
    br = basket_returns().values
    sig_h = br.std()
    cur_sig_h = pd.Series(br).ewm(halflife=72).std().iloc[-1]
    mu_h = br.mean()
    print(f"basket hourly sigma: full-sample {sig_h:.4%}, current {cur_sig_h:.4%}; mean {mu_h * 24:+.3%}/day")
    cur_d = cur_sig_h * np.sqrt(24)
    print(f"closed form, mu=0, T->inf, G=0.15%: (x0-L)/(G-L) = {(X0 - FLOOR) / (0.0015 - FLOOR):.1%}  (upper bound)")
    thetas = [0.3, 0.6, 0.95]
    goals = [None, 0.0015, 0.005]
    for days in (4, 7):
        print(f"\n=== {days} days left, start {X0:+.2%}, floor {FLOOR:+.1%} ===")
        print(
            f"{'theta':>6}{'goal':>8} | {'PDE mu=0':>9}{'PDE mu=hist':>12} | "
            f"{'MC P>0':>7}{'P<-5%':>7}{'median':>8}{'worst':>8} | {'MC@curvol P>0':>13}{'P<-5%':>7}"
        )
        for th in thetas:
            for G in goals:
                p0 = pde_success(th, cur_d, 0.0, days, G)
                p1 = pde_success(th, cur_d, mu_h * 24, days, G)
                a = mc(br, th, days, G)
                b = mc(br, th, days, G, scale=cur_sig_h / sig_h)
                gl = "none" if G is None else f"{G:+.2%}"
                print(
                    f"{th:>6.2f}{gl:>8} | {p0:>9.0%}{p1:>12.0%} | {a[0]:>7.0%}{a[1]:>7.0%}{a[2]:>+8.2%}{a[3]:>+8.2%}"
                    f" | {b[0]:>13.0%}{b[1]:>7.0%}"
                )


if __name__ == "__main__":
    main()
