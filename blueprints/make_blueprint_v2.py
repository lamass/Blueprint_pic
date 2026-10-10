"""
Blueprint v2 -- detailed legs + an interactive overlay page.

Legs: a zigzag on every trade print with a fixed 30-point reversal: any move of 30+ points against the
running extreme closes the current leg and starts a new one. So every swing of 30+ points has its own leg.
The last leg (into 16:30) is marked "open" -- it was never confirmed by a 30-point reversal.

Per leg (computed from the trades inside the leg):
  pts, minutes, speed (pts/min)
  fib_ratio      = |this leg| / |previous leg|  (how far it retraced the previous leg) + Fib zone
  delta_ratio    = (buy - sell) / (buy + sell), trades classified against their own bid/ask
  volume, rel_rate = contracts per minute vs the session average
  vwap / sma100  = % of the leg's trades above session VWAP / above SMA100 (1-min), and side at the leg end
  mid            = (start price + end price) / 2 -- the leg's middle line; mid_crosses = how many times price
                   crossed it inside the leg, crosses_per_min = density of that back-and-forth
  max_pullback   = deepest move against the leg inside it, % of the leg
  max_1m_range   = biggest 1-minute high-low inside the leg, % of the leg  (swift legs: big; grinding legs: small)
  max_1m_travel  = most distance travelled tick-to-tick in one minute, % of the leg (>100% = overshooting
                   back and forth more than the whole leg within a minute)
  efficiency     = |net move| / total distance travelled (1 = straight line, small = lots of back-and-forth)

Writes blueprints/BP_<date>_v2.json and blueprints/BP_<date>_v2.html (self-contained page).
Usage: python make_blueprint_v2.py 2026-10-02
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS = os.path.join(HERE, "..", "NinjaTrader", "backtest", "_cache", "NQ 12-26_Sep13-Oct2.txt.npz")
REV_PTS = 30.0
BG_EXTRA = 2            # extra sessions shown behind the BP (the tick file ends Oct 2, so these are the 2 before)
FIBS = [0.236, 0.382, 0.5, 0.618, 0.786]


def fib_zone(r):
    if r is None:
        return ""
    for f in FIBS:
        if r < f:
            return f"<{f*100:.1f}%"
    return "78.6-100% (deep)" if r < 1 else ">100% (new extreme)"


def zigzag(p, rev):
    """Returns pivot indices into p. Direction is set by the first 30-pt move from the start."""
    piv = [0]
    d = 0
    ext = 0
    for i in range(1, len(p)):
        if d == 0:
            if p[i] - p[0] >= rev:
                d = 1; ext = i
                piv[0] = int(np.argmin(p[:i + 1]))
            elif p[0] - p[i] >= rev:
                d = -1; ext = i
                piv[0] = int(np.argmax(p[:i + 1]))
            continue
        if d == 1:
            if p[i] >= p[ext]:
                ext = i
            elif p[ext] - p[i] >= rev:
                piv.append(ext); d = -1; ext = i
        else:
            if p[i] <= p[ext]:
                ext = i
            elif p[i] - p[ext] >= rev:
                piv.append(ext); d = 1; ext = i
    piv.append(ext)                       # final, unconfirmed extreme
    if piv[-1] != len(p) - 1:
        piv.append(len(p) - 1)            # tail from last extreme to the session end
    return piv


def main(day):
    z = np.load(TICKS)
    t_all = pd.to_datetime(z["t"])
    end = pd.Timestamp(day) + pd.Timedelta(hours=16, minutes=30)
    start = pd.Timestamp(day) - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
    m = (t_all >= start) & (t_all < end)
    t = t_all[m]; p = z["price"][m]; v = z["vol"][m]; bid = z["bid"][m]; ask = z["ask"][m]
    sgn = np.where(p >= ask, 1, np.where(p <= bid, -1, 0))
    buy, sell = v * (sgn > 0), v * (sgn < 0)
    mins = (t - start).total_seconds().to_numpy() / 60.0

    vwap = np.cumsum(p * v) / np.cumsum(v)
    hist = (t_all >= start - pd.Timedelta(days=4)) & (t_all < end)
    one = pd.Series(z["price"][hist], index=t_all[hist]).resample("1min").last().dropna()
    sma = one.rolling(100).mean()
    # SMA of the last COMPLETED minute for each trade
    sma_tick = sma.reindex(t.floor("1min") - pd.Timedelta(minutes=1), method="ffill").to_numpy()

    piv = zigzag(p, REV_PTS)
    sess_rate = v.sum() / mins[-1]
    legs = []
    for k in range(len(piv) - 1):
        a, b = piv[k], piv[k + 1]
        seg = slice(a, b + 1)
        pp, tt = p[seg], mins[seg]
        move = pp[-1] - pp[0]
        size = abs(move) or 1e-9
        up = move > 0
        mid = (pp[0] + pp[-1]) / 2
        s = np.sign(pp - mid); s = s[s != 0]
        crosses = int((s[1:] != s[:-1]).sum()) if len(s) > 1 else 0
        dur = max(tt[-1] - tt[0], 1e-6)
        if up:
            pull = np.max(np.maximum.accumulate(pp) - pp)
        else:
            pull = np.max(pp - np.minimum.accumulate(pp))
        mb = np.floor(tt).astype(int)
        df = pd.DataFrame({"m": mb, "p": pp, "d": np.r_[0, np.abs(np.diff(pp))]})
        g = df.groupby("m")
        rng1 = (g.p.max() - g.p.min()).max()
        trav1 = g.d.sum().max()
        travel = df.d.sum()
        vv, bb, ss = v[seg], buy[seg], sell[seg]
        prev = legs[-1] if legs else None
        fib = round(size / abs(prev["pts"]), 3) if prev and prev["pts"] else None
        legs.append({
            "n": k + 1, "dir": "UP" if up else "DOWN",
            "t0": round(float(tt[0]), 3), "p0": float(pp[0]), "t1": round(float(tt[-1]), 3), "p1": float(pp[-1]),
            "start": t[a].strftime("%H:%M:%S"), "end": t[b].strftime("%H:%M:%S"),
            "pts": round(float(move), 2), "minutes": round(float(dur), 2), "speed": round(float(size / dur), 2),
            "fib_ratio": fib, "fib_zone": fib_zone(fib),
            "delta_ratio": round(float((bb.sum() - ss.sum()) / max(1, bb.sum() + ss.sum())), 3),
            "volume": int(vv.sum()), "rel_rate": round(float(vv.sum() / dur / sess_rate), 2),
            "pct_above_vwap": round(float((pp > vwap[seg]).mean() * 100)), "end_vs_vwap": round(float(pp[-1] - vwap[b]), 2),
            "pct_above_sma100": round(float((pp > sma_tick[seg]).mean() * 100)), "end_vs_sma100": round(float(pp[-1] - sma_tick[b]), 2),
            "mid": round(float(mid), 2), "mid_crosses": crosses, "crosses_per_min": round(crosses / dur, 1),
            "max_pullback_pct": round(float(pull / size * 100)), "max_1m_range_pct": round(float(rng1 / size * 100)),
            "max_1m_travel_pct": round(float(trav1 / size * 100)), "efficiency": round(float(size / max(travel, 1e-9)), 3),
            "open": k == len(piv) - 2 and piv[-1] == len(p) - 1,
        })

    # ---- background chart: this session plus the BG_EXTRA sessions before it (1-minute bars)
    bg_start = start
    for _ in range(BG_EXTRA):
        bg_start -= pd.Timedelta(days=1)
        while bg_start.weekday() == 5 or (bg_start.weekday() == 4):   # sessions open Sun-Thu 18:00
            bg_start -= pd.Timedelta(days=1)
    mb = (t_all >= bg_start) & (t_all < end)
    tb = t_all[mb]; pb = z["price"][mb]; vb = z["vol"][mb]
    sb = np.where(pb >= z["ask"][mb], 1, np.where(pb <= z["bid"][mb], -1, 0))
    sess_id = ((tb - pd.Timedelta(hours=18)).floor("1D")).asi8          # VWAP restarts every 18:00
    dfb = pd.DataFrame({"m": np.floor((tb - bg_start).total_seconds().to_numpy() / 60).astype(int), "p": pb, "v": vb,
                        "d": vb * sb, "pv": pb * vb, "s": sess_id})
    dfb["vw"] = dfb.groupby("s").pv.cumsum() / dfb.groupby("s").v.cumsum()
    g = dfb.groupby("m")
    bars = pd.DataFrame({"o": g.p.first(), "h": g.p.max(), "l": g.p.min(), "c": g.p.last(), "v": g.v.sum(), "d": g.d.sum(), "vw": g.vw.last()})
    sma_all = pd.Series(z["price"][(t_all >= bg_start - pd.Timedelta(days=4)) & (t_all < end)],
                        index=t_all[(t_all >= bg_start - pd.Timedelta(days=4)) & (t_all < end)]).resample("1min").last().dropna().rolling(100).mean()
    bars["sma"] = sma_all.reindex([bg_start + pd.Timedelta(minutes=int(i)) for i in bars.index], method="ffill").to_numpy()
    bars = bars.reset_index()
    sessions = []
    d = bg_start
    while d < end:
        sessions.append({"m": round((d - bg_start).total_seconds() / 60), "label": (d + pd.Timedelta(hours=6)).strftime("%a %b %d")})
        d += pd.Timedelta(days=1)
        while d.weekday() in (4, 5):
            d += pd.Timedelta(days=1)

    H, L = float(p.max()), float(p.min())
    meta = {"id": f"BP_{pd.Timestamp(day).date()}_v2", "instrument": "NQ 12-26", "session_start": start.strftime("%Y-%m-%d %H:%M"),
            "session": f"{start:%a %Y-%m-%d %H:%M} -> {end:%a %Y-%m-%d %H:%M} ET", "high": H, "low": L, "range": H - L,
            "open": float(p[0]), "close": float(p[-1]), "rev_pts": REV_PTS, "n_legs": len(legs),
            "minutes": round(float(mins[-1]), 2)}
    bp = {"meta": meta, "legs": legs}
    json.dump(bp, open(os.path.join(HERE, f"{meta['id']}.json"), "w"), indent=1)

    chart = {k: [None if (isinstance(x, float) and np.isnan(x)) else (round(x, 2) if isinstance(x, float) else int(x)) for x in bars[k].tolist()]
             for k in ["m", "o", "h", "l", "c", "v", "d", "vw", "sma"]}
    chart["start"] = bg_start.strftime("%Y-%m-%d %H:%M")
    chart["bp_offset"] = round((start - bg_start).total_seconds() / 60)    # where the BP's own session sits on this chart
    chart["sessions"] = sessions
    chart["desc"] = f"NQ 12-26, {sessions[0]['label']} -> {sessions[-1]['label']} sessions (18:00 -> 16:30 ET)"
    tpl = open(os.path.join(HERE, "bp_viewer_template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__BP__*/null", json.dumps(bp)).replace("/*__CHART__*/null", json.dumps(chart))
    out = os.path.join(HERE, f"{meta['id']}.html")
    open(out, "w", encoding="utf-8").write(html)
    ups = sum(l["dir"] == "UP" for l in legs)
    print(f"{len(legs)} legs ({ups} up / {len(legs) - ups} down), range {H - L:.2f}; wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-10-02")
