"""
Blueprint core: build ONE session blueprint (BP) from its trades. Shared by the library builder and query tools.

A session is 18:00 ET (previous evening) -> 16:30 ET. Everything is stored in two forms:
  * absolute (prices, minutes since 18:00) so a BP can be laid over a real chart
  * normalised (0 = session low, 1 = session high; RTH 0..1 on its own range) so shapes compare across price levels

Leg layers
  legs      : zigzag with a fixed 30-point reversal -> every swing of 30+ pts is its own leg (as BP v2)
  skeleton  : coarse shape (bottom-up segmentation of 5-min closes, 7% of range tolerance, UP/DOWN >= 12% of range,
              otherwise SIDEWAYS), turning points snapped to real highs/lows (as BP v1)

Categories are assigned in bp_library.py (some need the whole library, e.g. "high volume" = top third of days).
"""
import numpy as np
import pandas as pd

REV_PTS = 30.0
SEG_TOL, TREND_MIN = 0.07, 0.12
FIBS = [0.236, 0.382, 0.5, 0.618, 0.786]
SESSION_MIN = 22.5 * 60                              # 18:00 -> 16:30
RTH_START, RTH_END = 15.5 * 60, SESSION_MIN          # 09:30 and 16:30 in minutes since 18:00
PHASES = [("Asia", 0, 8 * 60), ("London", 8 * 60, 14.5 * 60), ("NY pre", 14.5 * 60, 15.5 * 60),
          ("NY open", 15.5 * 60, 16.5 * 60), ("NY midday", 16.5 * 60, 20 * 60), ("NY close", 20 * 60, SESSION_MIN)]


def fib_zone(r):
    if r is None or not np.isfinite(r):
        return ""
    for f in FIBS:
        if r < f:
            return f"<{f*100:.1f}%"
    return "78.6-100% (deep)" if r < 1 else ">100% (new extreme)"


# --------------------------------------------------------------------------- 30-pt legs
def zigzag(p, rev):
    piv, d, ext = [0], 0, 0
    for i in range(1, len(p)):
        if d == 0:
            if p[i] - p[0] >= rev:
                d = 1; ext = i; piv[0] = int(np.argmin(p[:i + 1]))
            elif p[0] - p[i] >= rev:
                d = -1; ext = i; piv[0] = int(np.argmax(p[:i + 1]))
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
    piv.append(ext)
    if piv[-1] != len(p) - 1:
        piv.append(len(p) - 1)
    out = [piv[0]]
    for x in piv[1:]:
        if x > out[-1]:
            out.append(x)
    return out


def leg_metrics(t, mins, p, v, buy, sell, vwap, sma_tick, piv, sess_rate):
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
        pull = np.max(np.maximum.accumulate(pp) - pp) if up else np.max(pp - np.minimum.accumulate(pp))
        mb = np.floor(tt).astype(np.int64)
        # per-minute high-low inside the leg (vectorised)
        brk = np.r_[0, np.flatnonzero(np.diff(mb)) + 1, len(mb)]
        rng1 = max(pp[i:j].max() - pp[i:j].min() for i, j in zip(brk[:-1], brk[1:]))
        dp = np.r_[0, np.abs(np.diff(pp))]
        trav1 = max(dp[i:j].sum() for i, j in zip(brk[:-1], brk[1:]))
        travel = dp.sum()
        bb, ss = buy[seg].sum(), sell[seg].sum()
        prev = legs[-1] if legs else None
        fib = round(size / abs(prev["pts"]), 3) if prev and prev["pts"] else None
        legs.append({
            "n": k + 1, "dir": "UP" if up else "DOWN",
            "t0": round(float(tt[0]), 3), "p0": float(pp[0]), "t1": round(float(tt[-1]), 3), "p1": float(pp[-1]),
            "start": t[a].strftime("%H:%M:%S"), "end": t[b].strftime("%H:%M:%S"),
            "pts": round(float(move), 2), "minutes": round(float(dur), 2), "speed": round(float(size / dur), 2),
            "fib_ratio": fib, "fib_zone": fib_zone(fib),
            "delta_ratio": round(float((bb - ss) / max(1, bb + ss)), 3),
            "volume": int(v[seg].sum()), "rel_rate": round(float(v[seg].sum() / dur / sess_rate), 2),
            "pct_above_vwap": round(float((pp > vwap[seg]).mean() * 100)), "end_vs_vwap": round(float(pp[-1] - vwap[b]), 2),
            "pct_above_sma100": round(float(np.nanmean(pp > sma_tick[seg]) * 100)) if np.isfinite(sma_tick[b]) else None,
            "end_vs_sma100": round(float(pp[-1] - sma_tick[b]), 2) if np.isfinite(sma_tick[b]) else None,
            "mid": round(float(mid), 2), "mid_crosses": crosses, "crosses_per_min": round(crosses / dur, 1),
            "max_pullback_pct": round(float(pull / size * 100)), "max_1m_range_pct": round(float(rng1 / size * 100)),
            "max_1m_travel_pct": round(float(trav1 / size * 100)), "efficiency": round(float(size / max(travel, 1e-9)), 3),
            "open": k == len(piv) - 2,
        })
    return legs


# --------------------------------------------------------------------------- coarse skeleton
def segment(x, tol):
    bps = list(range(0, len(x), 2)) + ([len(x) - 1] if (len(x) - 1) % 2 else [])

    def cost(a, b):
        return np.abs(x[a:b + 1] - np.linspace(x[a], x[b], b - a + 1)).max()

    costs = [cost(bps[k - 1], bps[k + 1]) for k in range(1, len(bps) - 1)]
    while len(bps) > 2:
        k = int(np.argmin(costs))
        if costs[k] > tol:
            break
        del bps[k + 1]; del costs[k]
        if k - 1 >= 0:
            costs[k - 1] = cost(bps[k - 1], bps[k + 1]) if k + 1 < len(bps) else np.inf
        if k < len(costs):
            costs[k] = cost(bps[k], bps[k + 2]) if k + 2 < len(bps) else np.inf
    return bps


def skeleton(xc, hn, ln):
    def cls(d):
        return "UP" if d >= TREND_MIN else "DOWN" if d <= -TREND_MIN else "SIDEWAYS"

    def merge(lgs):
        out = [list(lgs[0])]
        for a, c, k in lgs[1:]:
            if k == out[-1][2]:
                out[-1][1] = c
            else:
                out.append([a, c, k])
        return out

    def snap(lgs):
        vs = [lgs[0][0]] + [lg[1] for lg in lgs]
        ys = []
        for k, i in enumerate(vs):
            pk = lgs[k - 1][2] if k > 0 else None
            nk = lgs[k][2] if k < len(lgs) else None
            lo = max(0, i - 2, vs[k - 1] + 1 if k > 0 else 0)
            hi = min(len(xc), i + 3, vs[k + 1] if k + 1 < len(vs) else len(xc))
            if (pk == "UP" and nk != "UP") or (pk == "SIDEWAYS" and nk == "DOWN") or (pk is None and nk == "DOWN"):
                j = lo + int(np.argmax(hn[lo:hi])); ys.append(float(hn[j])); vs[k] = j
            elif (pk == "DOWN" and nk != "DOWN") or (pk == "SIDEWAYS" and nk == "UP") or (pk is None and nk == "UP"):
                j = lo + int(np.argmin(ln[lo:hi])); ys.append(float(ln[j])); vs[k] = j
            else:
                ys.append(float(xc[i]))
        return vs, ys

    bps = segment(xc, SEG_TOL)
    legs = merge([[bps[k], bps[k + 1], cls(xc[bps[k + 1]] - xc[bps[k]])] for k in range(len(bps) - 1)])
    for _ in range(5):
        vs, ys = snap(legs)
        new = merge([[vs[k], vs[k + 1], cls(ys[k + 1] - ys[k])] for k in range(len(legs))])
        same = [l[2] for l in new] == [l[2] for l in legs]
        legs = new
        if same:
            break
    vs, ys = snap(legs)
    return [{"i0": int(vs[k]), "i1": int(vs[k + 1]), "y0": round(ys[k], 4), "y1": round(ys[k + 1], 4), "type": legs[k][2]}
            for k in range(len(legs))]


# --------------------------------------------------------------------------- one session
def build_bp(df, start, prior_1m, trade_date, contract):
    """df: trades of the session (index = ET timestamps; price, vol, bid, ask). prior_1m: 1-min closes before the session."""
    t = df.index
    p = df.price.to_numpy(float); v = df.vol.to_numpy(float)
    sgn = np.where(p >= df.ask.to_numpy(), 1, np.where(p <= df.bid.to_numpy(), -1, 0))
    buy, sell = v * (sgn > 0), v * (sgn < 0)
    mins = (t - start).total_seconds().to_numpy() / 60.0
    vwap = np.cumsum(p * v) / np.cumsum(v)

    one = pd.concat([prior_1m, pd.Series(p, index=t).resample("1min").last().dropna()])
    sma = one.rolling(100).mean()
    sma_tick = sma.reindex(t.floor("1min") - pd.Timedelta(minutes=1), method="ffill").to_numpy()

    H, L = float(p.max()), float(p.min()); R = H - L
    sess_rate = v.sum() / max(mins[-1], 1)
    piv = zigzag(p, REV_PTS)
    legs = leg_metrics(t, mins, p, v, buy, sell, vwap, sma_tick, piv, sess_rate)

    # ---- 1-minute bars (for overlay pages) and 5-minute grid (for shapes)
    m1 = np.floor(mins).astype(np.int64)
    d1 = pd.DataFrame({"m": m1, "p": p, "v": v, "d": buy - sell, "vw": vwap}).groupby("m")
    bars = pd.DataFrame({"o": d1.p.first(), "h": d1.p.max(), "l": d1.p.min(), "c": d1.p.last(), "v": d1.v.sum(), "d": d1.d.sum(), "vw": d1.vw.last()})
    bars["sma"] = sma.reindex([start + pd.Timedelta(minutes=int(i)) for i in bars.index], method="ffill").to_numpy()
    bars = bars.reset_index()

    nb = int(SESSION_MIN // 5)
    m5 = np.minimum((mins // 5).astype(np.int64), nb - 1)
    g5 = pd.DataFrame({"b": m5, "p": p, "v": v, "buy": buy, "sell": sell, "vw": vwap}).groupby("b")
    b5 = pd.DataFrame({"o": g5.p.first(), "h": g5.p.max(), "l": g5.p.min(), "c": g5.p.last(), "v": g5.v.sum(),
                       "buy": g5.buy.sum(), "sell": g5.sell.sum(), "vw": g5.vw.last()}).reindex(range(nb))
    b5["c"] = b5.c.ffill().bfill(); b5["vw"] = b5.vw.ffill().bfill()
    for k in ("o", "h", "l"):
        b5[k] = b5[k].fillna(b5.c)
    b5 = b5.fillna(0)
    n = lambda x: (np.asarray(x, float) - L) / R
    xc, hn, ln = n(b5.c), n(b5.h), n(b5.l)

    # mid-point crossings inside each 5-min bucket: how often price crossed that bucket's own (high+low)/2
    mid5 = ((b5.h + b5.l) / 2).to_numpy()[m5]
    side = np.sign(p - mid5)
    keep = side != 0
    s_k, b_k = side[keep], m5[keep]
    flip = (s_k[1:] != s_k[:-1]) & (b_k[1:] == b_k[:-1])
    mid_cross = np.bincount(b_k[1:][flip], minlength=nb)[:nb]
    # SMA100 (1-min) sampled at each bucket's close, and cumulative delta
    sma5 = sma.reindex([start + pd.Timedelta(minutes=5 * i + 4) for i in range(nb)], method="ffill").to_numpy()
    cum_delta = np.cumsum((b5.buy - b5.sell).to_numpy())
    skel = skeleton(xc, hn, ln)

    # ---- RTH (09:30-16:30) and overnight (18:00-09:30)
    rth = mins >= RTH_START
    on = ~rth
    pr, po = p[rth], p[on]
    rH, rL = float(pr.max()), float(pr.min()); rR = max(rH - rL, 1e-9)
    oH, oL = float(po.max()), float(po.min()); oR = max(oH - oL, 1e-9)
    rO, rC = float(pr[0]), float(pr[-1])
    i_rth = int(RTH_START // 5)
    rth_shape = (np.asarray(b5.c[i_rth:], float) - rL) / rR

    def ratio(mask):
        bb, ss = buy[mask].sum(), sell[mask].sum()
        return float((bb - ss) / max(1, bb + ss))

    def crosses(a, ref):
        s = np.sign(np.asarray(a) - np.asarray(ref)); s = s[s != 0]
        return int((s[1:] != s[:-1]).sum())

    phases = []
    for name, a, c in PHASES:
        mk = (mins >= a) & (mins < c)
        if not mk.any():
            phases.append({"phase": name, "net": 0.0, "range": 0.0, "delta_ratio": 0.0, "rel_volume": 0.0}); continue
        pp = p[mk]
        phases.append({"phase": name, "net": round(float((pp[-1] - pp[0]) / R), 3), "range": round(float((pp.max() - pp.min()) / R), 3),
                       "delta_ratio": round(ratio(mk), 3), "rel_volume": round(float(v[mk].sum() / (c - a) / sess_rate), 2)})

    # opening range 09:30-10:00: which side broke first
    orm = (mins >= RTH_START) & (mins < RTH_START + 30)
    orH, orL = p[orm].max(), p[orm].min()
    after = np.flatnonzero(mins >= RTH_START + 30)
    up_i = after[p[after] > orH]; dn_i = after[p[after] < orL]
    or_first = "none" if not len(up_i) and not len(dn_i) else ("up" if (len(up_i) and (not len(dn_i) or up_i[0] < dn_i[0])) else "down")

    fibs = np.array([l["fib_ratio"] for l in legs[1:] if l["fib_ratio"] is not None], float)
    rth5 = b5.iloc[i_rth:]
    feats = {
        "open": float(p[0]), "high": H, "low": L, "close": float(p[-1]), "range": R,
        "open_pos": round((p[0] - L) / R, 4), "close_pos": round((p[-1] - L) / R, 4), "net": round((p[-1] - p[0]) / R, 4),
        "net_pts": round(float(p[-1] - p[0]), 2),
        "t_high": round(float(mins[int(np.argmax(p))]), 1), "t_low": round(float(mins[int(np.argmin(p))]), 1),
        "volume": int(v.sum()), "delta_ratio": round(ratio(np.ones_like(rth)), 4),
        "rth_delta_ratio": round(ratio(rth), 4), "on_delta_ratio": round(ratio(on), 4),
        "pct_above_vwap": round(float((b5.c > b5.vw).mean() * 100), 1),
        "rth_pct_above_vwap": round(float((rth5.c > rth5.vw).mean() * 100), 1),
        "rth_vwap_crosses": crosses(rth5.c, rth5.vw),
        "rth_close_vs_vwap": round(float(rC - vwap[-1]), 2),
        "rth_pct_above_sma100": round(float(np.nanmean(pr > sma_tick[rth]) * 100), 1),
        "rth": {"open": rO, "high": rH, "low": rL, "close": rC, "range": rR, "net": round((rC - rO) / rR, 4),
                "open_pos": round((rO - rL) / rR, 4), "close_pos": round((rC - rL) / rR, 4),
                "t_high": round(float(mins[rth][int(np.argmax(pr))]), 1), "t_low": round(float(mins[rth][int(np.argmin(pr))]), 1),
                "volume": int(v[rth].sum())},
        "on": {"high": oH, "low": oL, "range": oR, "net": round((po[-1] - po[0]) / R, 4)},
        "rth_open_in_on_range": round((rO - oL) / oR, 3),
        "or_break_first": or_first,
        "n_legs": len(legs), "rth_legs_per_hour": round(sum(1 for l in legs if l["t0"] >= RTH_START) / 7.0, 2),
        "zz_efficiency": round(R / max(1e-9, sum(abs(l["pts"]) for l in legs)), 4),
        "median_fib": round(float(np.median(fibs)), 3) if len(fibs) else None,
        "share_shallow": round(float((fibs < 0.382).mean()), 3) if len(fibs) else None,
        "share_mid": round(float(((fibs >= 0.382) & (fibs < 0.618)).mean()), 3) if len(fibs) else None,
        "share_deep": round(float(((fibs >= 0.618) & (fibs < 1)).mean()), 3) if len(fibs) else None,
        "share_extension": round(float((fibs >= 1).mean()), 3) if len(fibs) else None,
        "median_leg_crosses_per_min": round(float(np.median([l["crosses_per_min"] for l in legs])), 2),
        "median_leg_efficiency": round(float(np.median([l["efficiency"] for l in legs])), 4),
        "median_leg_speed": round(float(np.median([l["speed"] for l in legs])), 2),
        "phases": phases,
        "skeleton_types": "".join(s["type"][0] for s in skel),
        "max_skeleton_up": round(max([s["y1"] - s["y0"] for s in skel] + [0]), 3),
        "max_skeleton_down": round(max([s["y0"] - s["y1"] for s in skel] + [0]), 3),
    }
    meta = {"id": f"BP_{trade_date}", "trade_date": str(trade_date), "weekday": pd.Timestamp(trade_date).day_name(),
            "contract": contract, "session_start": start.strftime("%Y-%m-%d %H:%M"),
            "session_end": (start + pd.Timedelta(minutes=SESSION_MIN)).strftime("%Y-%m-%d %H:%M"),
            "first_trade": t[0].strftime("%Y-%m-%d %H:%M:%S"), "last_trade": t[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "n_trades": int(len(p)), "rev_pts": REV_PTS, "minutes": round(float(mins[-1]), 2)}
    shape = {"session": np.round(xc, 4).tolist(), "rth": np.round(rth_shape, 4).tolist(),
             "high": np.round(hn, 4).tolist(), "low": np.round(ln, 4).tolist(),
             "vwap": np.round(n(b5.vw), 4).tolist(),
             "rel_volume": np.round(b5.v / max(b5.v.mean(), 1e-9), 3).tolist(),
             "delta_ratio": np.round(((b5.buy - b5.sell) / (b5.buy + b5.sell).replace(0, np.nan)).fillna(0), 3).tolist(),
             # added 2026-10-09: so every BP carries the same lower panels as the first BP picture
             "sma100": [None if not np.isfinite(x) else round(float(x), 4) for x in n(sma5)],
             "mid_cross": mid_cross.astype(int).tolist(),
             "cum_delta": np.round(cum_delta / max(np.abs(cum_delta).max(), 1e-9), 4).tolist(),
             "cum_delta_max_abs": float(np.abs(cum_delta).max())}
    chart = {k: [None if (isinstance(x, float) and np.isnan(x)) else (round(x, 2) if isinstance(x, float) else int(x)) for x in bars[k].tolist()]
             for k in ["m", "o", "h", "l", "c", "v", "d", "vw", "sma"]}
    return {"meta": meta, "features": feats, "skeleton": skel, "legs": legs, "shape": shape}, chart, one.iloc[-300:]
