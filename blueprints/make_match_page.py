"""
Real market chart of chosen days with their K closest library BPs superimposed (numbered 1..K, each with its own
on/off switch and opacity).

Matching (per day): only BPs from EARLIER sessions, same session family (UP / DOWN / NEUTRAL), ranked by RMSE of the
whole-session normalised shape. Note: uses the target day's full shape (hindsight) -- this page is for judging how
well the library's shapes fit real days, not a live signal.

  python make_match_page.py 2026-09-14 2026-09-15 --k 3
Writes blueprints/matches/match_<day1>_<day2>.html
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bp_core import build_bp, SESSION_MIN  # noqa: E402
from bp_library import connect, fetch, LIB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


PANELS = ("vwap", "sma100", "rel_volume", "delta_ratio", "mid_cross")


def overlay_of(date, rank, rmse, idx_entry, pred):
    bp = json.load(open(os.path.join(LIB, "bp", f"BP_{date}.json")))
    f, legs = bp["features"], bp["legs"]
    verts = [[legs[0]["t0"], legs[0]["p0"]]] + [[l["t1"], l["p1"]] for l in legs]
    skel = [[s["i0"] * 5 + 2.5, f["low"] + s["y0"] * f["range"]] for s in bp["skeleton"][:1]] + \
           [[s["i1"] * 5 + 2.5, f["low"] + s["y1"] * f["range"]] for s in bp["skeleton"]]
    return {"n": rank, "date": date, "weekday": bp["meta"]["weekday"], "contract": bp["meta"]["contract"], "rmse": round(rmse, 4),
            "session_type": idx_entry["session_type"], "rth_type": idx_entry["rth_type"], "story": idx_entry["story"],
            "phases": idx_entry["phases"], "high": f["high"], "low": f["low"], "open": f["open"], "range": f["range"],
            "legs": verts, "skeleton": skel, "n_legs": len(legs),
            "panels": {k: bp["shape"][k] for k in PANELS},
            # match % = 100 * (1 - distance / typical distance between two random days); see bp_predict.py
            "match_pct": round(max(0.0, 100 * (1 - rmse / pred["refs"]["full_day"])), 1),
            "track": pred["track"].get(date)}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("days", nargs="+"); ap.add_argument("--k", type=int, default=3)
    a = ap.parse_args()
    lib = json.load(open(os.path.join(LIB, "library_index.json")))["bps"]
    by = {b["date"]: b for b in lib}
    pred = json.load(open(os.path.join(LIB, "predictions.json")))
    conn = connect(); cur = conn.cursor()
    out = []
    for d in a.days:
        q = by[d]; s0 = np.asarray(q["shape"])
        cand = sorted(((float(np.sqrt(np.mean((np.asarray(b["shape"]) - s0) ** 2))), b) for b in lib
                       if b["date"] < d and b["session_family"] == q["session_family"]), key=lambda x: x[0])[: a.k]
        own = json.load(open(os.path.join(LIB, "bp", f"BP_{d}.json")))
        start = pd.Timestamp(own["meta"]["session_start"])
        prev = [b["date"] for b in lib if b["date"] < d][-1]
        tail = json.load(open(os.path.join(LIB, "bp", f"BP_{prev}.json"))).get("_tail_1m")
        prior = pd.Series(tail["c"], index=pd.to_datetime(tail["t"])) if tail else pd.Series(dtype=float)
        df = fetch(cur, own["meta"]["contract"], start, start + pd.Timedelta(minutes=SESSION_MIN))
        _, chart, _ = build_bp(df, start, prior, pd.Timestamp(d).date(), own["meta"]["contract"])
        f = own["features"]
        out.append({"date": d, "weekday": own["meta"]["weekday"], "contract": own["meta"]["contract"], "start": own["meta"]["session_start"],
                    "high": f["high"], "low": f["low"], "open": f["open"], "close": f["close"], "range": f["range"],
                    "session_type": q["session_type"], "rth_type": q["rth_type"], "story": q["story"], "phases": q["phases"],
                    "n_candidates": len([b for b in lib if b["date"] < d and b["session_family"] == q["session_family"]]),
                    "family": q["session_family"], "chart": chart,
                    "panels": {k: own["shape"][k] for k in PANELS},
                    "predictions": [{k: p[k] for k in ("as_of", "rank", "bp_date", "match_pct", "predicted_dir", "predicted_move_pts",
                                                       "actual_dir", "actual_move_pts", "hit")} for p in pred["predictions"] if p["target_date"] == d],
                    "matches": [overlay_of(b["date"], k + 1, r, b, pred) for k, (r, b) in enumerate(cand)]})
        print(d, "matches:", [(m["n"], m["date"], m["rmse"]) for m in out[-1]["matches"]])
    os.makedirs(os.path.join(HERE, "matches"), exist_ok=True)
    tpl = open(os.path.join(HERE, "match_viewer_template.html"), encoding="utf-8").read()
    path = os.path.join(HERE, "matches", "match_" + "_".join(a.days) + ".html")
    open(path, "w", encoding="utf-8").write(tpl.replace("/*__DAYS__*/null", json.dumps(out)))
    print("wrote", path)


if __name__ == "__main__":
    main()
