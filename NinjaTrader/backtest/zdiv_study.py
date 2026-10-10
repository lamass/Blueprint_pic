"""
Z-Score Hybrid divergence study on NQ 250-volume bars exported by BarExportZScore.

Detector (no look-ahead; everything is known at the signal bar's close):
  - Z swing points via a zigzag on the Z line: a Z peak is CONFIRMED once Z has fallen `rev` below
    the running max (mirror for troughs). The signal fires on that confirmation bar.
  - Price extreme at a Z peak = highest high in [peak-w, min(peak+w, confirm)] (lows for troughs).
  - Bearish divergence at confirmed peak B: some earlier confirmed peak A within `maxgap` bars with
    z[A] >= zmin, z[B] < z[A] - dz, and priceHigh[B] > priceHigh[A]. Most recent such A is used.
    Bullish is the mirror on troughs.

Usage:
  python zdiv_study.py                 # calibrate vs labels, then outcome study
"""
import json
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(os.environ["USERPROFILE"], "Documents", "NinjaTrader 8", "claude_probe", "NQ_12-26_250Volume_bars.csv")
LABELS = os.path.join(HERE, "..", "labels", "divergences_nq250.json")
LABEL_START, LABEL_END = pd.Timestamp("2026-10-04 18:00"), pd.Timestamp("2026-10-08")


def load():
    df = pd.read_csv(CSV, parse_dates=["time"])
    df = df.dropna(subset=["z"]).reset_index(drop=True)
    return df


def swings(z, rev):
    """Zigzag on z. Returns list of (extreme_idx, confirm_idx, +1 peak / -1 trough)."""
    out = []
    d = 0
    ext = 0
    for i in range(1, len(z)):
        if d == 0:
            d = 1 if z[i] > z[0] else -1
            ext = i
            continue
        if d == 1:
            if z[i] >= z[ext]:
                ext = i
            elif z[i] <= z[ext] - rev:
                out.append((ext, i, 1)); d = -1; ext = i
        else:
            if z[i] <= z[ext]:
                ext = i
            elif z[i] >= z[ext] + rev:
                out.append((ext, i, -1)); d = 1; ext = i
    return out


def detect(df, rev=0.3, zmin=1.8, dz=0.15, maxgap=150, w=3):
    z = df["z"].to_numpy(); hi = df["high"].to_numpy(); lo = df["low"].to_numpy()
    sw = swings(z, rev)
    peaks, troughs, sig = [], [], []
    for ext, conf, kind in sw:
        a, b = max(0, ext - w), min(ext + w, conf) + 1
        if kind == 1:
            px = hi[a:b].max()
            for A in reversed(peaks):
                if ext - A[0] > maxgap:
                    break
                if z[A[0]] >= zmin and z[ext] < z[A[0]] - dz and px > A[1]:
                    sig.append(dict(kind="bearish", a=A[0], b=ext, conf=conf, za=z[A[0]], zb=z[ext], pa=A[1], pb=px))
                    break
            peaks.append((ext, px))
        else:
            px = lo[a:b].min()
            for A in reversed(troughs):
                if ext - A[0] > maxgap:
                    break
                if z[A[0]] <= -zmin and z[ext] > z[A[0]] + dz and px < A[1]:
                    sig.append(dict(kind="bullish", a=A[0], b=ext, conf=conf, za=z[A[0]], zb=z[ext], pa=A[1], pb=px))
                    break
            troughs.append((ext, px))
    s = pd.DataFrame(sig)
    if len(s):
        t = df["time"]
        s["t_a"], s["t_b"], s["t_conf"] = t[s.a].values, t[s.b].values, t[s.conf].values
    return s, sw


def labels():
    d = json.load(open(LABELS))
    rows = [dict(kind=p["kind"], t_a=pd.Timestamp(p["price"]["t0"]), t_b=pd.Timestamp(p["price"]["t1"]))
            for p in d["pairs"] if p["kind"] in ("bullish", "bearish") and p["price"] and p["z"]]
    lab = pd.DataFrame(rows)
    # pair 2 in the JSON was a cross-day mis-pairing (Oct 5 price line + Oct 6 Z line) -- drop it
    lab = lab[~lab.t_a.astype(str).str.startswith("2026-10-05 11:43")]
    return lab.reset_index(drop=True)


def match(sig, lab, tol_min=4):
    """A label is hit if a same-direction signal's second extreme is within tol of the label's second price anchor."""
    s = sig[(sig.t_b >= LABEL_START) & (sig.t_b < LABEL_END)]
    hit = 0
    used = set()
    for _, L in lab.iterrows():
        c = s[(s.kind == L.kind) & ((s.t_b - L.t_b).abs() <= pd.Timedelta(minutes=tol_min))]
        c = c[~c.index.isin(used)]
        if len(c):
            hit += 1; used.add(c.index[0])
    return hit, len(s)


def main():
    df = load()
    lab = labels()
    print(f"bars with z: {len(df):,}  {df.time.iloc[0]} -> {df.time.iloc[-1]}   labels: {len(lab)}")

    # --- 1. sanity: exported z at the user's anchors vs the values on his lines
    t = df.set_index("time")["z"]
    d = json.load(open(LABELS))
    errs = []
    for p in d["pairs"]:
        if p["z"]:
            for k in ("0", "1"):
                ts = pd.Timestamp(p["z"]["t" + k]); i = t.index.searchsorted(ts)
                win = t.iloc[max(0, i - 3): i + 4]
                errs.append(min(abs(win - p["z"]["v" + k])))
    print(f"z check: |chart line anchor - nearest exported z (+-3 bars)| median={np.median(errs):.3f}  p90={np.percentile(errs, 90):.3f}")

    # --- 2. calibrate detector on the labelled days
    grid = []
    for rev, zmin, dz, maxgap in product([0.2, 0.3, 0.4, 0.5], [1.5, 1.8, 2.0], [0.0, 0.15, 0.3], [60, 150, 300]):
        s, _ = detect(df, rev, zmin, dz, maxgap)
        hit, n = match(s, lab)
        grid.append((rev, zmin, dz, maxgap, hit, n))
    g = pd.DataFrame(grid, columns=["rev", "zmin", "dz", "maxgap", "hits", "n_detected"])
    g["recall"] = g.hits / len(lab)
    g["precision"] = g.hits / g.n_detected.clip(lower=1)
    g["f1"] = 2 * g.recall * g.precision / (g.recall + g.precision).clip(lower=1e-9)
    print("\ntop detector settings vs your labels (Oct 5-7):")
    print(g.sort_values(["f1", "recall"], ascending=False).head(8).to_string(index=False))
    best = g.sort_values(["f1", "recall"], ascending=False).iloc[0]
    params = dict(rev=best.rev, zmin=best.zmin, dz=best.dz, maxgap=int(best.maxgap))
    json.dump(params, open(os.path.join(HERE, "zdiv_params.json"), "w"))
    print("chosen:", params)

    # --- 3. outcome study on bars BEFORE the labelled days (not used for calibration)
    s, sw = detect(df, **params)
    c = df.close.to_numpy(); hi = df.high.to_numpy(); lo = df.low.to_numpy(); op = df.open.to_numpy()
    N = len(df)

    def outcomes(conf_idx, direction, label):
        rows = []
        for i, dr in zip(conf_idx, direction):
            e = i + 1
            if e + 60 >= N:
                continue
            r = dict(t=df.time[i], dir=dr, kind=label)
            for h in (10, 30, 60):
                r[f"ret{h}"] = dr * (c[e + h - 1] - op[e])
            seg_h, seg_l = hi[e:e + 30], lo[e:e + 30]
            r["mfe30"] = (seg_h.max() - op[e]) if dr > 0 else (op[e] - seg_l.min())
            r["mae30"] = (op[e] - seg_l.min()) if dr > 0 else (seg_h.max() - op[e])
            rows.append(r)
        return pd.DataFrame(rows)

    div = outcomes(s.conf.to_numpy(), np.where(s.kind == "bearish", -1, 1), "divergence")
    # baseline: every confirmed Z extreme beyond +-zmin that is NOT a divergence signal
    sig_conf = set(s.conf)
    base_idx = [(conf, -k) for ext, conf, k in sw if abs(df.z[ext]) >= params["zmin"] and conf not in sig_conf]
    base = outcomes([b[0] for b in base_idx], [b[1] for b in base_idx], "plain Z extreme")
    allres = pd.concat([div, base])
    allres["first_in_chain"] = np.nan
    oos = allres[allres.t < LABEL_START]

    def summary(x):
        return pd.Series(dict(n=len(x), ret10=x.ret10.mean(), ret30=x.ret30.mean(), ret60=x.ret60.mean(),
                              win30=(x.ret30 > 0).mean(), mfe30=x.mfe30.median(), mae30=x.mae30.median()))

    print(f"\nOUTCOMES (points, in the signal's direction; entry next bar open), {oos.t.min():%b %d} - {oos.t.max():%b %d}, not used for tuning:")
    tab = oos.groupby(["kind", "dir"]).apply(summary)
    print(tab.round(2).to_string())
    print("\nall directions pooled:")
    print(oos.groupby("kind").apply(summary).round(2).to_string())

    # chains: is this divergence the first in its run, or the 2nd/3rd in the same direction?
    s = s.sort_values("conf").reset_index(drop=True)
    s["chain_pos"] = 1
    for k in range(1, len(s)):
        if s.kind[k] == s.kind[k - 1] and s.a[k] <= s.b[k - 1]:
            s.loc[k, "chain_pos"] = s.chain_pos[k - 1] + 1
    dv = div.copy(); dv["chain_pos"] = s.chain_pos.values[: len(dv)] if len(dv) == len(s) else np.nan
    dv = dv[dv.t < LABEL_START]
    print("\nby position in chain (divergences only):")
    print(dv.groupby(dv.chain_pos.clip(upper=3)).apply(summary).round(2).to_string())

    # per-bar volatility reference
    print(f"\nreference: median |close[t+30]-open[t+1]| over all bars = {np.median(np.abs(c[30:] - op[1:-29])):.2f} pts")
    s.to_csv(os.path.join(HERE, "zdiv_signals.csv"), index=False)
    allres.to_csv(os.path.join(HERE, "zdiv_outcomes.csv"), index=False)


if __name__ == "__main__":
    main()
