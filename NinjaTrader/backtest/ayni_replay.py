"""
Tick-by-tick replay of AyniOrderFlowStrategy.cs (the fixed version) on a NinjaTrader tick export.

Mirrors the C# logic line for line where the data allows:
  - delta: each trade classified against its own bid/ask (price>=ask buy, price<=bid sell)
  - 5-second sampling on the TICK clock, momentum = mid now vs MomentumSamples samples ago
  - RunningZ: same EMA mean/variance, 50-sample warm-up, clip +/-4
  - score = Bias + W*z ..., P = logistic(score); enter when P>=thr / P<=1-thr, flat only
  - 09:35-15:55 ET window, max trades/day (counted at signal), daily loss limit on closed P&L,
    flatten outside window, stop/target in ticks from the fill price

NOT available in an NT tick export: bid/ask SIZES. So OFI, pressure and microprice are 0 for the
whole run (exactly like an NT Strategy Analyzer backtest), and the model reduces to
0.6*z(delta) + 0.3*z(momentum).

Fill assumptions (conservative, NT-backtest-like):
  - market entry fills on the NEXT trade at its ask (long) / bid (short)
  - stop: triggers when a trade prints at/through the stop, fills at that trade's price
  - target: needs a trade THROUGH the limit (IsFillLimitOnTouch=false), fills at the limit
  - if a tick could hit both, the stop is checked first

Usage:
  python ayni_replay.py "..\\..\\NQ 12-26_Sep13-Oct2.txt" --utc
"""
import argparse
import io
import os
import sys

import numpy as np
import pandas as pd
from numba import njit

TICK = 0.25
TICK_VALUE = 5.0          # NQ: $20/point -> $5/tick
HERE = os.path.dirname(os.path.abspath(__file__))


def load_ticks(path, utc):
    cache = os.path.join(HERE, "_cache", os.path.basename(path) + ".npz")
    if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(path):
        z = np.load(cache)
        return {k: z[k] for k in z.files}

    print(f"parsing {path} ...", flush=True)
    with open(path, "rb") as f:
        raw = f.read().replace(b" ", b";")
    df = pd.read_csv(io.BytesIO(raw), sep=";", header=None,
                     names=["d", "hms", "frac", "price", "bid", "ask", "vol"],
                     dtype={"d": np.int32, "hms": np.int32, "frac": np.int64,
                            "price": np.float64, "bid": np.float64, "ask": np.float64, "vol": np.float64})
    del raw
    days = {d: pd.Timestamp(str(d)).value for d in df["d"].unique()}
    hms = df["hms"].to_numpy()
    secs = (hms // 10000) * 3600 + (hms // 100 % 100) * 60 + hms % 100
    ns = df["d"].map(days).to_numpy(np.int64) + secs.astype(np.int64) * 1_000_000_000 + df["frac"].to_numpy() * 100

    # Convert to chart time (America/New_York); file is either UTC or already ET.
    idx = pd.DatetimeIndex(ns).tz_localize("UTC" if utc else "America/New_York", ambiguous="NaT", nonexistent="NaT")
    et = idx.tz_convert("America/New_York").tz_localize(None)
    t = et.asi8
    out = {
        "t": t,
        "price": df["price"].to_numpy(), "bid": df["bid"].to_numpy(), "ask": df["ask"].to_numpy(),
        "vol": df["vol"].to_numpy(),
        "hms": (et.hour * 10000 + et.minute * 100 + et.second).to_numpy(np.int32),
        "day": ((t // 86_400_000_000_000)).astype(np.int32),
    }
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    np.savez(cache, **out)
    return out


@njit(cache=True)
def zupdate(state, x, alpha):
    # state = [n, mean, var]  -- identical to RunningZ.Update in the C#
    state[0] += 1
    if state[0] == 1:
        state[1] = x
        state[2] = 0.0
        return 0.0
    d = x - state[1]
    state[1] += alpha * d
    state[2] = (1 - alpha) * (state[2] + alpha * d * d)
    if state[0] < 50 or state[2] <= 1e-12:
        return 0.0
    z = (x - state[1]) / np.sqrt(state[2])
    return max(-4.0, min(4.0, z))


@njit(cache=True)
def replay(t, px, bid, ask, vol, hms, day,
           sample_s, mom_n, norm_win, thr, w_delta, w_mom, bias,
           stop_t, tgt_t, max_trades, loss_limit, start_t, end_t):
    n = len(t)
    alpha = 2.0 / (norm_win + 1)
    zd = np.zeros(3)
    zm = np.zeros(3)
    zflat = np.zeros((3, 3))          # OFI / pressure / microprice: always fed 0 -> always z=0
    sample_ns = np.int64(sample_s) * 1_000_000_000

    mids = np.empty(n // 10 + 1000)   # sample-mid history for momentum
    n_samp = 0

    cap = 200000
    T_sig = np.empty(cap, np.int64); T_side = np.empty(cap, np.int8)
    T_fill_i = np.empty(cap, np.int64); T_fill = np.empty(cap)
    T_exit_i = np.empty(cap, np.int64); T_exit = np.empty(cap)
    T_reason = np.empty(cap, np.int8); T_p = np.empty(cap); T_zd = np.empty(cap); T_zm = np.empty(cap)
    nt = 0

    win_delta = 0.0
    next_sample = np.int64(-1)
    cur_day = -1
    trades_today = 0
    halted = False
    closed_pnl = 0.0
    day_start = 0.0

    pos = 0                 # +1 long, -1 short
    pending_entry = 0
    pending_exit = False
    stop_px = 0.0
    tgt_px = 0.0

    for i in range(n):
        p = px[i]
        # ---- order handling for this trade print ----
        if pending_entry != 0:
            pos = pending_entry
            fill = ask[i] if pos > 0 else bid[i]
            T_fill_i[nt - 1] = i; T_fill[nt - 1] = fill
            stop_px = fill - pos * stop_t * TICK
            tgt_px = fill + pos * tgt_t * TICK
            pending_entry = 0
        elif pos != 0:
            exit_px = 0.0
            reason = -1
            if pending_exit:
                exit_px = bid[i] if pos > 0 else ask[i]; reason = 2
            elif (pos > 0 and p <= stop_px) or (pos < 0 and p >= stop_px):
                exit_px = p; reason = 0
            elif (pos > 0 and p > tgt_px) or (pos < 0 and p < tgt_px):
                exit_px = tgt_px; reason = 1
            if reason >= 0:
                T_exit_i[nt - 1] = i; T_exit[nt - 1] = exit_px; T_reason[nt - 1] = reason
                closed_pnl += pos * (exit_px - T_fill[nt - 1]) / TICK * TICK_VALUE
                pos = 0
                pending_exit = False

        # ---- OnMarketData(Last): delta ----
        if p >= ask[i]:
            win_delta += vol[i]
        elif p <= bid[i]:
            win_delta -= vol[i]

        # ---- OnBarUpdate: day reset + sampling ----
        if day[i] != cur_day:
            cur_day = day[i]
            trades_today = 0
            halted = False
            day_start = closed_pnl

        if next_sample < 0:
            next_sample = t[i] + sample_ns
            continue
        if t[i] < next_sample:
            continue
        next_sample = t[i] + sample_ns

        mid = (bid[i] + ask[i]) / 2.0
        mids[n_samp] = mid
        n_samp += 1
        mom = 0.0
        if n_samp > mom_n:
            mom = (mid - mids[n_samp - 1 - mom_n]) / TICK

        lz_d = zupdate(zd, win_delta, alpha)
        for k in range(3):
            zupdate(zflat[k], 0.0, alpha)
        lz_m = zupdate(zm, mom, alpha)
        win_delta = 0.0

        score = bias + w_delta * lz_d + w_mom * lz_m
        prob = 1.0 / (1.0 + np.exp(-score))

        in_sess = hms[i] >= start_t and hms[i] < end_t
        can_trade = in_sess and (not halted) and trades_today < max_trades
        flat = pos == 0 and pending_entry == 0

        if can_trade and flat and (prob >= thr or prob <= 1 - thr):
            is_long = prob >= thr
            trades_today += 1
            T_sig[nt] = i; T_side[nt] = 1 if is_long else -1
            T_p[nt] = prob; T_zd[nt] = lz_d; T_zm[nt] = lz_m
            T_fill_i[nt] = -1; T_exit_i[nt] = -1; T_reason[nt] = -1
            nt += 1
            pending_entry = 1 if is_long else -1

        if (not in_sess or halted) and pos != 0:
            pending_exit = True

        if closed_pnl - day_start <= -loss_limit:
            halted = True

    return (T_sig[:nt], T_side[:nt], T_fill_i[:nt], T_fill[:nt], T_exit_i[:nt], T_exit[:nt],
            T_reason[:nt], T_p[:nt], T_zd[:nt], T_zm[:nt], n_samp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--utc", action="store_true", help="export timestamps are UTC (else America/New_York)")
    ap.add_argument("--commission", type=float, default=4.50, help="round-turn $ per contract (assumption)")
    ap.add_argument("--threshold", type=float, default=0.60)
    ap.add_argument("--max-trades", type=int, default=10)
    ap.add_argument("--tag", default="default")
    a = ap.parse_args()

    d = load_ticks(a.file, a.utc)
    t = d["t"]
    print(f"ticks: {len(t):,}   {pd.Timestamp(t[0])} -> {pd.Timestamp(t[-1])} ET")

    res = replay(t, d["price"], d["bid"], d["ask"], d["vol"], d["hms"], d["day"],
                 5, 12, 300, a.threshold, 0.6, 0.3, 0.0,
                 8, 12, a.max_trades, 500.0, 93500, 155500)
    sig, side, fi, fill, xi, xpx, reason, prob, zd, zm, n_samp = res

    tr = pd.DataFrame({
        "signal_time": pd.to_datetime(t[sig]), "side": np.where(side > 0, "LONG", "SHORT"),
        "P_up": prob, "z_delta": zd, "z_mom": zm,
        "entry_time": pd.to_datetime(np.where(fi >= 0, t[np.maximum(fi, 0)], 0)), "entry": fill,
        "exit_time": pd.to_datetime(np.where(xi >= 0, t[np.maximum(xi, 0)], 0)), "exit": xpx,
        "reason": pd.Categorical.from_codes(np.where(reason >= 0, reason, 3), ["stop", "target", "flatten", "open"]),
    })
    tr = tr[tr["reason"] != "open"].copy()
    tr["ticks"] = np.where(tr["side"] == "LONG", tr["exit"] - tr["entry"], tr["entry"] - tr["exit"]) / TICK
    tr["gross_$"] = tr["ticks"] * TICK_VALUE
    tr["net_$"] = tr["gross_$"] - a.commission

    # Did the signal call direction at all? Signed mid move 1 and 5 minutes after the signal.
    mid = (d["bid"] + d["ask"]) / 2
    sig_i = sig[: len(tr)] if len(tr) == len(sig) else sig[tr.index.to_numpy()]
    sgn = np.where(tr["side"] == "LONG", 1, -1)
    for mins in (1, 5):
        j = np.minimum(np.searchsorted(t, t[sig_i] + mins * 60_000_000_000), len(t) - 1)
        tr[f"fwd_{mins}m_ticks"] = sgn * (mid[j] - mid[sig_i]) / TICK

    out_csv = os.path.join(HERE, f"ayni_trades_{a.tag}.csv")
    tr.to_csv(out_csv, index=False)

    def summ(x, label):
        if len(x) == 0:
            print(f"{label:<8} no trades"); return
        w = (x["ticks"] > 0).mean()
        print(f"{label:<8} n={len(x):4d}  win={w:6.1%}  avg={x['ticks'].mean():+6.2f} ticks  "
              f"gross=${x['gross_$'].sum():+9.2f}  net=${x['net_$'].sum():+9.2f}  "
              f"fwd1m={x['fwd_1m_ticks'].mean():+5.2f}  fwd5m={x['fwd_5m_ticks'].mean():+5.2f}")

    print(f"5s samples: {n_samp:,}   breakeven win rate at 8/12 ticks, before costs: 40.0%")
    summ(tr, "ALL"); summ(tr[tr.side == "LONG"], "LONG"); summ(tr[tr.side == "SHORT"], "SHORT")
    print("exits:", tr["reason"].value_counts().to_dict())
    eq = tr["net_$"].cumsum()
    print(f"max drawdown (net): ${(eq - eq.cummax()).min():.2f}   days traded: {tr.signal_time.dt.date.nunique()}")
    print(f"trades -> {out_csv}")


if __name__ == "__main__":
    main()
