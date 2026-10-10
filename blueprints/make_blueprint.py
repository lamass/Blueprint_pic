"""
Build ONE blueprint (BP) of an NQ session: 18:00 ET (day before) -> 16:30 ET.

Output:
  blueprints/BP_<date>.json  -- the original BP (never modified by matching; matching works on copies)
  blueprints/BP_<date>.png   -- skeleton picture + meta panel

Normalisation
  x = clock time (time of day is part of the pattern), 5-minute buckets, 18:00 -> 16:30 = 270 buckets
  y = position inside the session range: 0 = session low, 1 = session high
      (the real point range and its size vs recent sessions are kept in meta, so scale is not lost)

Layers stored per 5-minute bucket
  price (close / high / low, normalised), volume (relative to the session's average bucket),
  delta ratio = (buy - sell) / (buy + sell) with trades classified against their own bid/ask,
  cumulative delta (normalised), midpoint crossings = how many times price crossed the bucket's own
  (high+low)/2 inside those 5 minutes (local back-and-forth), VWAP (session, from 18:00), SMA100 (1-min).

Skeleton
  piecewise-linear "legs" from bottom-up segmentation of 5-min closes (max deviation SEG_TOL of range),
  each leg classified UP / DOWN / SIDEWAYS, adjacent same-type legs merged, turning points snapped to the
  real high/low. Each leg records the Fib retracement depth of the move before it.

Flexibility allowance (proposed starting values -- to be tuned visually together)
  stored in meta; drawn as the shaded band (price) and the horizontal whiskers at turning points (time).

Usage: python make_blueprint.py 2026-10-02
"""
import json
import os
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS = os.path.join(HERE, "..", "NinjaTrader", "backtest", "_cache", "NQ 12-26_Sep13-Oct2.txt.npz")

SEG_TOL = 0.07          # skeleton may deviate at most 7% of the session range from the 5-min closes
TREND_MIN = 0.12        # a leg must move >= 12% of the session range to count as UP/DOWN, else SIDEWAYS
FIBS = [0.236, 0.382, 0.5, 0.618, 0.786]
FLEX = {                # how far a BP may be bent when superimposed on live data (starting proposal)
    "price_band": 0.08,             # +-8% of range around the skeleton
    "leg_time_stretch": 0.25,       # each leg may be 25% shorter/longer
    "turn_time_shift_min": 30,      # each turning point may move +-30 min in clock time
    "leg_amplitude_scale": [0.75, 1.33],
    "integrity": ["leg order and types fixed (UP stays UP, SIDEWAYS stays SIDEWAYS)",
                  "no leg may be dropped or added",
                  "retracement depth may move at most one Fib zone"],
}
PHASES = [("Asia", "18:00", "02:00"), ("London", "02:00", "08:30"), ("NY pre", "08:30", "09:30"),
          ("NY open", "09:30", "10:30"), ("NY midday", "10:30", "14:00"), ("NY close", "14:00", "16:30")]


def load_session(day):
    z = np.load(TICKS)
    t = pd.to_datetime(z["t"])
    end = pd.Timestamp(day) + pd.Timedelta(hours=16, minutes=30)
    start = pd.Timestamp(day) - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
    while start.weekday() == 5:                       # Monday session opens Sunday 18:00
        start -= pd.Timedelta(days=1)
    df = pd.DataFrame({"price": z["price"], "bid": z["bid"], "ask": z["ask"], "vol": z["vol"]}, index=t)
    hist = df[(df.index >= start - pd.Timedelta(days=12)) & (df.index < start)]
    return df[(df.index >= start) & (df.index < end)], hist, start, end


def segment(x, tol):
    """Bottom-up piecewise-linear segmentation; returns breakpoint indices (incl. first and last)."""
    bps = list(range(0, len(x), 2)) + ([len(x) - 1] if (len(x) - 1) % 2 else [])

    def cost(a, b):
        line = np.linspace(x[a], x[b], b - a + 1)
        return np.abs(x[a:b + 1] - line).max()

    while len(bps) > 2:
        costs = [cost(bps[k - 1], bps[k + 1]) for k in range(1, len(bps) - 1)]
        k = int(np.argmin(costs))
        if costs[k] > tol:
            break
        del bps[k + 1]
    return bps


def main(day):
    ses, hist, start, end = load_session(day)
    p = ses.price
    sign = np.where(ses.price >= ses.ask, 1, np.where(ses.price <= ses.bid, -1, 0))
    ses = ses.assign(buy=ses.vol * (sign > 0), sell=ses.vol * (sign < 0))

    # ---- 5-minute buckets on a fixed clock grid
    grid = pd.date_range(start, end, freq="5min", inclusive="left")
    g = ses.groupby(pd.Grouper(freq="5min"))
    b = pd.DataFrame({"open": g.price.first(), "high": g.price.max(), "low": g.price.min(), "close": g.price.last(),
                      "vol": g.vol.sum(), "buy": g.buy.sum(), "sell": g.sell.sum()}).reindex(grid)
    b["close"] = b.close.ffill(); b["open"] = b.open.fillna(b.close)
    b["high"] = b.high.fillna(b.close); b["low"] = b.low.fillna(b.close); b = b.fillna({"vol": 0, "buy": 0, "sell": 0})

    # midpoint crossings inside each bucket
    mid = ((b.high + b.low) / 2).reindex(ses.index, method="ffill")
    side = np.sign(ses.price.values - mid.values)
    nz = side != 0
    s_nz, idx_nz = side[nz], ses.index[nz]
    flips = pd.Series((s_nz[1:] != s_nz[:-1]).astype(int), index=idx_nz[1:])
    same_bucket = idx_nz[1:].floor("5min") == idx_nz[:-1].floor("5min")
    b["mid_cross"] = flips[same_bucket].groupby(pd.Grouper(freq="5min")).sum().reindex(grid).fillna(0)

    # VWAP (session) and SMA100 on 1-min closes (warmed up on prior data)
    b["vwap"] = ((ses.price * ses.vol).groupby(pd.Grouper(freq="5min")).sum().reindex(grid).fillna(0).cumsum()
                 / b.vol.cumsum().replace(0, np.nan)).ffill()
    one = pd.concat([hist.price, ses.price]).resample("1min").last().dropna()
    b["sma100"] = one.rolling(100).mean().reindex(grid + pd.Timedelta(minutes=4), method="ffill").values
    b["delta_ratio"] = ((b.buy - b.sell) / (b.buy + b.sell).replace(0, np.nan)).fillna(0)
    b["cum_delta"] = (b.buy - b.sell).cumsum()

    H, L = p.max(), p.min(); R = H - L
    n = lambda v: (v - L) / R
    xc = n(b.close.values)

    # ---- skeleton legs
    bps = segment(xc, SEG_TOL)

    def classify(a, c):
        d = xc[c] - xc[a]
        return "UP" if d >= TREND_MIN else "DOWN" if d <= -TREND_MIN else "SIDEWAYS"

    hn, ln = n(b.high.values), n(b.low.values)

    def merge_same(lgs):
        out = [list(lgs[0])]
        for a, c, k in lgs[1:]:
            if k == out[-1][2]:
                out[-1][1] = c
            else:
                out.append([a, c, k])
        return out

    def snap(lgs):
        """Turning points -> the real high/low within +-2 buckets, never past a neighbouring vertex."""
        vs = [lgs[0][0]] + [lg[1] for lg in lgs]
        ys = []
        for k, i in enumerate(vs):
            prev_k = lgs[k - 1][2] if k > 0 else None
            next_k = lgs[k][2] if k < len(lgs) else None
            lo_i = max(0, i - 2, vs[k - 1] + 1 if k > 0 else 0)
            hi_i = min(len(xc), i + 3, vs[k + 1] if k + 1 < len(vs) else len(xc))
            if (prev_k == "UP" and next_k != "UP") or (prev_k == "SIDEWAYS" and next_k == "DOWN") or (prev_k is None and next_k == "DOWN"):
                j = lo_i + int(np.argmax(hn[lo_i:hi_i])); ys.append(hn[j]); vs[k] = j
            elif (prev_k == "DOWN" and next_k != "DOWN") or (prev_k == "SIDEWAYS" and next_k == "UP") or (prev_k is None and next_k == "UP"):
                j = lo_i + int(np.argmin(ln[lo_i:hi_i])); ys.append(ln[j]); vs[k] = j
            else:
                ys.append(xc[i])
        return vs, ys

    legs = merge_same([[bps[k], bps[k + 1], classify(bps[k], bps[k + 1])] for k in range(len(bps) - 1)])
    for _ in range(5):
        # reclassify on the snapped vertices (a "sideways" leg can't hide a big move) until stable
        verts, vy = snap(legs)
        new = [[verts[k], verts[k + 1], "UP" if vy[k + 1] - vy[k] >= TREND_MIN else "DOWN" if vy[k + 1] - vy[k] <= -TREND_MIN else "SIDEWAYS"]
               for k in range(len(legs))]
        new = merge_same(new)
        if [l[2] for l in new] == [l[2] for l in legs]:
            legs = new
            break
        legs = new
    verts, vy = snap(legs)
    assert all(verts[k] < verts[k + 1] for k in range(len(verts) - 1)), "vertices out of order"

    def fib_zone(r):
        for f in FIBS:
            if r < f:
                return f"<{f*100:.1f}%"
        return ">78.6% (reversal)" if r < 1 else ">100% (new extreme)"

    leg_meta = []
    for k, (a, c, kind) in enumerate(legs):
        d_norm = vy[k + 1] - vy[k]
        rec = {"n": k + 1, "type": kind, "start": grid[verts[k]].strftime("%H:%M"), "end": grid[verts[k + 1]].strftime("%H:%M"),
               "minutes": int((verts[k + 1] - verts[k]) * 5), "move_pts": round(d_norm * R, 2), "move_pct_range": round(d_norm * 100, 1),
               "delta_ratio": round(float((b.buy[verts[k]:verts[k + 1]].sum() - b.sell[verts[k]:verts[k + 1]].sum())
                                          / max(1, (b.buy[verts[k]:verts[k + 1]] + b.sell[verts[k]:verts[k + 1]]).sum())), 3),
               "rel_volume": round(float(b.vol[verts[k]:verts[k + 1]].mean() / b.vol.mean()), 2)}
        # Fib: how deep this leg retraced the previous directional leg
        if k > 0 and kind != "SIDEWAYS":
            pk = next((j for j in range(k - 1, -1, -1) if legs[j][2] != "SIDEWAYS"), None)
            if pk is not None and legs[pk][2] != kind:
                prev_move = vy[pk + 1] - vy[pk]
                r = abs(d_norm) / abs(prev_move) if prev_move else 0
                rec["retrace_of_leg"] = pk + 1; rec["retrace_ratio"] = round(r, 3); rec["fib_zone"] = fib_zone(r)
        leg_meta.append(rec)

    # ---- phase summaries
    phases = []
    for name, a, c in PHASES:
        ta = pd.Timestamp(f"{grid[0].date()} {a}") if a >= "18:00" else pd.Timestamp(f"{pd.Timestamp(day).date()} {a}")
        tc = pd.Timestamp(f"{pd.Timestamp(day).date()} {c}")
        m = (grid >= ta) & (grid < tc)
        x = b[m]
        if not len(x):
            continue
        phases.append({"phase": name, "from": a, "to": c, "net_pct_range": round((n(x.close.iloc[-1]) - n(x.open.iloc[0])) * 100, 1),
                       "range_pct": round((n(x.high.max()) - n(x.low.min())) * 100, 1),
                       "rel_volume": round(float(x.vol.mean() / b.vol.mean()), 2),
                       "delta_ratio": round(float((x.buy.sum() - x.sell.sum()) / max(1, (x.buy + x.sell).sum())), 3),
                       "pct_time_above_vwap": round(float((x.close > x.vwap).mean() * 100)),
                       "pct_time_above_sma100": round(float((x.close > x.sma100).mean() * 100)),
                       "avg_mid_crosses": round(float(x.mid_cross.mean()), 1)})

    # ---- scale context: this range vs prior sessions in the file
    prior = []
    for d in pd.date_range(start - pd.Timedelta(days=12), start - pd.Timedelta(days=1)):
        s0, e0 = d + pd.Timedelta(hours=18), d + pd.Timedelta(days=1, hours=16, minutes=30)
        w = hist.price[(hist.index >= s0) & (hist.index < e0)]
        if len(w) > 10000:
            prior.append(w.max() - w.min())
    avg_prior = float(np.mean(prior)) if prior else float("nan")

    def cross_count(a, ref):
        s = np.sign(a - ref); s = s[s != 0]
        return int((s[1:] != s[:-1]).sum())

    meta = {
        "id": f"BP_{pd.Timestamp(day).date()}", "instrument": "NQ 12-26", "session": f"{start:%a %Y-%m-%d %H:%M} -> {end:%a %Y-%m-%d %H:%M} ET",
        "weekday": pd.Timestamp(day).day_name(),
        "ohlc": [float(p.iloc[0]), float(H), float(L), float(p.iloc[-1])], "range_pts": float(R),
        "range_vs_prior_sessions": round(R / avg_prior, 2), "prior_sessions_used": len(prior),
        "open_pos": round(n(p.iloc[0]), 3), "close_pos": round(n(p.iloc[-1]), 3),
        "time_of_high": p.idxmax().strftime("%H:%M"), "time_of_low": p.idxmin().strftime("%H:%M"),
        "volume": int(ses.vol.sum()), "delta_ratio_session": round(float((ses.buy.sum() - ses.sell.sum()) / max(1, (ses.buy + ses.sell).sum())), 3),
        "vwap_close_pos": round(n(b.vwap.iloc[-1]), 3), "vwap_crosses_5m": cross_count(b.close.values, b.vwap.values),
        "pct_time_above_vwap": round(float((b.close > b.vwap).mean() * 100)),
        "sma100_crosses_5m": cross_count(b.close.values, b.sma100.values), "pct_time_above_sma100": round(float((b.close > b.sma100).mean() * 100)),
        "legs": leg_meta, "phases": phases, "flex": FLEX,
        "params": {"bucket": "5min", "seg_tol": SEG_TOL, "trend_min": TREND_MIN, "fibs": FIBS,
                   "delta": "trade vs its own bid/ask", "sma": "100 x 1-min closes", "vwap": "session from 18:00",
                   "mid_cross": "crossings of the bucket's own (H+L)/2 within that 5 min"},
    }
    series = {"time": [x.strftime("%H:%M") for x in grid], "close": np.round(xc, 4).tolist(), "high": np.round(hn, 4).tolist(),
              "low": np.round(ln, 4).tolist(), "vwap": np.round(n(b.vwap.values), 4).tolist(), "sma100": np.round(n(b.sma100.values), 4).tolist(),
              "rel_volume": np.round(b.vol.values / b.vol.mean(), 3).tolist(), "delta_ratio": np.round(b.delta_ratio.values, 3).tolist(),
              "mid_cross": b.mid_cross.astype(int).tolist(), "skeleton": [[int(i), round(float(y), 4)] for i, y in zip(verts, vy)]}
    out = os.path.join(HERE, f"{meta['id']}.json")
    json.dump({"meta": meta, "series": series}, open(out, "w"), indent=1)

    draw(meta, series, b, grid, verts, vy, legs, R, L)
    print("wrote", out)


def draw(meta, s, b, grid, verts, vy, legs, R, L):
    plt.rcParams.update({"font.size": 9, "axes.facecolor": "#0f1115", "figure.facecolor": "#0f1115", "text.color": "#d8dbe2",
                         "axes.labelcolor": "#d8dbe2", "xtick.color": "#9aa0aa", "ytick.color": "#9aa0aa", "axes.edgecolor": "#3a3f4a"})
    fig = plt.figure(figsize=(19, 10.5))
    gs = GridSpec(4, 2, width_ratios=[3.2, 1.3], height_ratios=[3.2, 0.9, 0.9, 0.8], hspace=0.08, wspace=0.16)
    x = np.arange(len(grid))
    ax = fig.add_subplot(gs[0, 0])
    col = {"UP": "#3ddc84", "DOWN": "#ff5c5c", "SIDEWAYS": "#f2c94c"}

    # session phases
    for k, (name, a, c) in enumerate(PHASES):
        i0 = next((i for i, t in enumerate(s["time"]) if t == a), 0); i1 = next((i for i, t in enumerate(s["time"]) if t == c), len(x))
        ax.axvspan(i0, i1, color="#ffffff", alpha=0.03 if k % 2 else 0.0)
        ax.text((i0 + i1) / 2, 1.115, name, ha="center", va="center", fontsize=8, color="#8b919c")

    # flexibility band around the skeleton
    sk_y = np.interp(x, verts, vy)
    ax.fill_between(x, sk_y - meta["flex"]["price_band"], sk_y + meta["flex"]["price_band"], color="#6aa9ff", alpha=0.10, lw=0)
    ax.vlines(x, s["low"], s["high"], color="#5a606b", lw=0.8)
    ax.plot(x, s["close"], color="#9aa0aa", lw=0.8, alpha=0.8, label="5-min close")
    ax.plot(x, s["vwap"], color="#c084fc", lw=1.2, label="VWAP (session)")
    ax.plot(x, s["sma100"], color="#38bdf8", lw=1.0, ls="--", label="SMA100 (1-min)")
    for k, (a, c, kind) in enumerate(legs):
        ax.plot([verts[k], verts[k + 1]], [vy[k], vy[k + 1]], color=col[kind], lw=3.2, solid_capstyle="round")
        # leg number only; all numbers are in the table on the right
        ax.text((verts[k] + verts[k + 1]) / 2, max(vy[k], vy[k + 1]) + 0.035, str(k + 1), ha="center", fontsize=8.5,
                color=col[kind], fontweight="bold", bbox=dict(facecolor="#0f1115", edgecolor="none", alpha=0.75, pad=0.8))
    sh = meta["flex"]["turn_time_shift_min"] / 5
    ax.errorbar(verts, vy, xerr=sh, fmt="o", color="#e5e7eb", ms=4, elinewidth=1, capsize=3, alpha=0.9)
    # fib grid of the session range on the right axis
    for f in [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]:
        ax.axhline(f, color="#3a3f4a", lw=0.6, ls=":")
    ax.set_ylim(-0.08, 1.16); ax.set_xlim(0, len(x) - 1)
    ax.set_ylabel("position in session range (0 = low, 1 = high)")
    ax2 = ax.twinx(); ax2.set_ylim(L + -0.08 * R, L + 1.16 * R); ax2.tick_params(colors="#6b7280"); ax2.set_ylabel("NQ points", color="#6b7280")
    ax.legend(loc="lower right", fontsize=8, facecolor="#0f1115", edgecolor="#3a3f4a")
    ax.set_title(f"{meta['id']}   skeleton (green up / red down / yellow sideways), shaded = price flexibility +-{meta['flex']['price_band']*100:.0f}% range, "
                 f"whiskers = turn time +-{meta['flex']['turn_time_shift_min']} min", loc="left", fontsize=10)
    ax.tick_params(labelbottom=False)

    ticks = [i for i, t in enumerate(s["time"]) if t.endswith(":00") and int(t[:2]) % 2 == 0]
    a1 = fig.add_subplot(gs[1, 0], sharex=ax)
    a1.bar(x, s["rel_volume"], color="#64748b", width=0.9); a1.axhline(1, color="#9aa0aa", lw=0.6, ls=":")
    a1.set_ylabel("rel. volume"); a1.tick_params(labelbottom=False)
    a2 = fig.add_subplot(gs[2, 0], sharex=ax)
    dr = np.array(s["delta_ratio"])
    a2.bar(x, dr, color=np.where(dr >= 0, "#3ddc84", "#ff5c5c"), width=0.9)
    a2b = a2.twinx(); cd = b.cum_delta.values; a2b.plot(x, cd, color="#e5e7eb", lw=1); a2b.set_yticks([])
    a2.set_ylabel("delta ratio\n(+cum. delta)"); a2.set_ylim(-0.6, 0.6); a2.tick_params(labelbottom=False)
    a3 = fig.add_subplot(gs[3, 0], sharex=ax)
    a3.bar(x, s["mid_cross"], color="#f2c94c", width=0.9); a3.set_ylabel("mid-point\ncrosses / 5m")
    a3.set_xticks(ticks); a3.set_xticklabels([s["time"][i] for i in ticks])

    # meta panel
    m = meta
    am = fig.add_subplot(gs[:, 1]); am.axis("off")
    L1 = [f"BLUEPRINT  {m['id']}", f"{m['instrument']}   {m['weekday']}", m["session"], "",
          f"O {m['ohlc'][0]:.2f}  H {m['ohlc'][1]:.2f}", f"L {m['ohlc'][2]:.2f}  C {m['ohlc'][3]:.2f}",
          f"range {m['range_pts']:.0f} pts = {m['range_vs_prior_sessions']}x avg of prior {m['prior_sessions_used']} sessions",
          f"open at {m['open_pos']:.2f}  close at {m['close_pos']:.2f} of range",
          f"high {m['time_of_high']}   low {m['time_of_low']}",
          f"volume {m['volume']:,}   delta ratio {m['delta_ratio_session']:+.3f}",
          f"VWAP: {m['pct_time_above_vwap']}% of time above, {m['vwap_crosses_5m']} crosses",
          f"SMA100: {m['pct_time_above_sma100']}% of time above, {m['sma100_crosses_5m']} crosses", "",
          "LEGS  #  type   time         min   pts   %rng  dR     vol  fib"]
    for lg in m["legs"]:
        fz = f"{lg['retrace_ratio']*100:.0f}% {lg['fib_zone']}" if "fib_zone" in lg else ""
        L1.append(f"     {lg['n']:>2} {lg['type'][:4]:<5} {lg['start']}-{lg['end']} {lg['minutes']:>4} {lg['move_pts']:>+5.0f} {lg['move_pct_range']:>+5.0f}"
                  f" {lg['delta_ratio']:>+.2f} {lg['rel_volume']:>4.1f}  {fz}")
    L1 += ["", "PHASES        net%  rng%  vol  dR     >VWAP >SMA  midX"]
    for ph in m["phases"]:
        L1.append(f"{ph['phase']:<12} {ph['net_pct_range']:>+5.0f} {ph['range_pct']:>5.0f} {ph['rel_volume']:>4.1f} {ph['delta_ratio']:>+.2f}"
                  f"  {ph['pct_time_above_vwap']:>4}% {ph['pct_time_above_sma100']:>4}% {ph['avg_mid_crosses']:>4.1f}")
    f = m["flex"]
    L1 += ["", "FLEXIBILITY (starting proposal)", f"price band +-{f['price_band']*100:.0f}% of range",
           f"leg duration +-{f['leg_time_stretch']*100:.0f}%, turn time +-{f['turn_time_shift_min']} min",
           f"leg amplitude x{f['leg_amplitude_scale'][0]}-{f['leg_amplitude_scale'][1]}"] + ["- " + r for r in f["integrity"]]
    am.text(0.02, 0.99, "\n".join(L1), va="top", ha="left", family="monospace", fontsize=8.1, color="#d8dbe2")
    fig.savefig(os.path.join(HERE, f"{meta['id']}.png"), dpi=110, bbox_inches="tight")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-10-02")
