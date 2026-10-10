"""R10: multivariable end-game optimisation (composition x exposure schedule x lock x floor).

Objective: maximise P(contest return at T > 0) from x0, subject to P(return < -5 %) <= RISK_CAP.
Joint stationary block bootstrap of the 5-coin hourly return MATRIX (keeps the cross-correlation structure,
fat tails, jumps, vol clustering), hourly monitoring (gap risk), fees on every exposure change.
Selection on the FIRST half of history, validation on the SECOND half (out-of-sample).

Policies (exposure theta_t of a fixed-composition basket, capped by per-coin caps and 0.95 gross):
  const(th)             : theta = th
  browne(k, cap)        : theta = clip(k*(G-x)/(sigma_h*sqrt(T-t)), 0, cap)    (more risk as time runs out)
  cppi(th, m)           : theta = min(th, m*(x - L))                              (de-risk near the floor)
Compositions: inverse-vol, equal-weight, BTC/ETH 50/50, min-variance (capped), max-vol tilt.

usage: python research/endgame_multivar.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105.data.universe import load_panels  # noqa: E402

U = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]
CAPS = np.array([0.40, 0.40, 0.30, 0.30, 0.30])
X0, FEE, RISK_CAP, ELIM = -0.0247, 0.0010, 0.15, -0.05


def comps(R: np.ndarray) -> dict:
    vol = R.std(axis=0)
    cov = np.cov(R.T)
    iv = (1 / vol) / (1 / vol).sum()
    # long-only min-variance via projected gradient on the simplex
    w = np.full(5, 0.2)
    for _ in range(3000):
        w = np.clip(w - 0.5 * cov @ w / np.abs(cov).max(), 0, None)
        w /= w.sum()
    mv = vol.argsort()[::-1]
    maxv = np.zeros(5)
    maxv[mv[:3]] = 1 / 3
    return {
        "inverse-vol": iv,
        "equal": np.full(5, 0.2),
        "BTC/ETH": np.array([0.5, 0.5, 0, 0, 0]),
        "min-variance": w,
        "max-vol tilt": maxv,
    }


def max_theta(w):  # largest scale s with s*w within caps and gross <= 0.95
    return float(min(0.95 / w.sum(), np.min(np.where(w > 0, CAPS / np.where(w > 0, w, 1), np.inf))))


def simulate(R, w, policy, H, G, L, n=4000, block=24, seed=3):
    rng = np.random.default_rng(seed)
    N = len(R)
    starts = rng.integers(0, N, size=(n, H // block + 1))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n, -1)[:, :H] % N
    br = R[idx] @ w  # basket return per unit exposure, (n, H)
    sig_h = (R @ w).std()
    x = np.full(n, X0)
    th_prev = np.zeros(n)
    live = np.ones(n, bool)
    for h in range(H):
        th = np.where(live, policy(x, h, H, sig_h, G, L), 0.0)
        x -= np.abs(th - th_prev) * FEE
        th_prev = th
        x = x + np.log1p(th * br[:, h])
        hit = live & ((x >= G) | (x <= L))
        live &= ~hit
    x -= np.abs(th_prev) * FEE  # final flatten / lock exit
    ret = np.expm1(x)
    return (ret > 0).mean(), (ret < ELIM).mean(), float(np.median(ret))


def make_policies(tmax):
    P = {}
    for th in (0.5, 0.6, 0.75, 0.9):
        if th <= tmax + 1e-9:
            P[f"const {th:.2f}"] = (lambda t: lambda x, h, H, s, G, L: np.full_like(x, t))(th)
    for k, cap in itertools.product((0.5, 1.0, 1.5), (0.6, 0.75, 0.9)):
        if cap <= tmax + 1e-9:
            P[f"browne k{k} cap{cap}"] = (
                lambda k, c: lambda x, h, H, s, G, L: np.clip(k * (G - x) / (s * np.sqrt(max(H - h, 1))), 0, c)
            )(k, cap)
    for th, m in itertools.product((0.6, 0.75, 0.9), (20, 40)):
        if th <= tmax + 1e-9:
            P[f"cppi {th} m{m}"] = (lambda t, m: lambda x, h, H, s, G, L: np.clip(m * (x - L), 0, t))(th, m)
    return P


def main():
    Pn, _ = load_panels(ROOT)
    R = Pn["close"][U].dropna().pct_change().dropna().values
    half = len(R) // 2
    IS, OOS = R[:half], R[half:]
    print(f"hourly matrix {R.shape}; corr (full):\n{np.round(np.corrcoef(R.T), 2)}")
    rows = []
    for H, label in ((84, "3.5d (ends Oct 14)"), (156, "6.5d (ends Oct 17)")):
        for cname, w in comps(IS).items():
            tmax = max_theta(w)
            for G in (0.0015, 0.0025, 0.005):
                for L in (-0.045, -0.040):
                    for pname, pol in make_policies(tmax).items():
                        p_is, e_is, m_is = simulate(IS, w, pol, H, G, L, n=1500)
                        rows.append((label, cname, pname, G, L, p_is, e_is, m_is, w, pol, H))
    for label in ("3.5d (ends Oct 14)", "6.5d (ends Oct 17)"):
        cand = [r for r in rows if r[0] == label and r[6] <= RISK_CAP]
        cand.sort(key=lambda r: -r[5])
        print(f"\n=== {label}: top 8 in-sample (P<-5% <= {RISK_CAP:.0%}), then OUT-OF-SAMPLE check ===")
        print(
            f"{'composition':<14}{'policy':<22}{'lock':>7}{'floor':>7} | {'IS P>0':>7}{'P<-5%':>7} | "
            f"{'OOS P>0':>8}{'P<-5%':>7}{'median':>8}"
        )
        for r in cand[:8]:
            p, e, m = simulate(OOS, r[8], r[9], r[10], r[3], r[4], n=4000, seed=7)
            print(
                f"{r[1]:<14}{r[2]:<22}{r[3]:>+7.2%}{r[4]:>+7.1%} | {r[5]:>7.0%}{r[6]:>7.0%} | "
                f"{p:>8.0%}{e:>7.0%}{m:>+8.2%}"
            )
        base = [
            r
            for r in rows
            if r[0] == label and r[1] == "inverse-vol" and r[2] == "const 0.60" and r[3] == 0.0025 and r[4] == -0.045
        ][0]
        p, e, m = simulate(OOS, base[8], base[9], base[10], base[3], base[4], n=4000, seed=7)
        print(
            f"{'DEPLOYED v1.4':<14}{'const 0.60':<22}{0.0025:>+7.2%}{-0.045:>+7.1%} | {base[5]:>7.0%}{base[6]:>7.0%} | "
            f"{p:>8.0%}{e:>7.0%}{m:>+8.2%}"
        )


if __name__ == "__main__":
    main()
