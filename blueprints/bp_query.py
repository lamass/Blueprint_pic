"""
Find the most matching BPs in the library -- by elimination first, then by shape.

  python bp_query.py --like 2026-10-02                    # whole day: same session family, closest shape
  python bp_query.py --like 2026-10-02 --as-of 11:00      # only what was known at 11:00 that day
  python bp_query.py --cat SESSION_DECISIVE_UP NYOPEN_UP  # list BPs having all these categories

As-of matching never looks past the as-of time:
  * shapes are cut at the as-of time and rescaled to the range SO FAR (0 = low so far, 1 = high so far)
  * finished periods get a U / D / S letter on that same so-far scale
  * ELIMINATION (hard): overnight 18:00-09:30 direction (+-0.25), and once finished the NY open 09:30-10:30 and
    NY midday 10:30-14:00 directions (+-0.15) must be the same -- an up NY open is never matched with a down or
    sideways NY open, a sideways overnight never with a trending one
  * RANKING (soft): RMSE between the two cut-and-rescaled shapes, + 0.03 for every finished Asia / London / NY pre
    letter that differs
"""
import argparse
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PHASE_BUCKETS = [("Asia", 0, 96), ("London", 96, 174), ("NY pre", 174, 186), ("NY open", 186, 198), ("NY midday", 198, 240), ("NY close", 240, 270)]


def load():
    return json.load(open(os.path.join(HERE, "library", "library_index.json")))["bps"]


def bars_done(hhmm):
    """number of 5-minute bars fully finished by this clock time (the bar that starts at hhmm is NOT included)"""
    h, m = map(int, hhmm.split(":"))
    mins = (h * 60 + m - 18 * 60) % (24 * 60)
    return int(mins // 5)


HARD = {"NY open", "NY midday"}
SOFT = {"Asia", "London", "NY pre"}
SOFT_PENALTY = 0.03


def prefix(shape, n):
    x = np.asarray(shape[:n], float)
    lo, hi = x.min(), x.max()
    return (x - lo) / max(hi - lo, 1e-9), hi - lo


def letter(net, th):
    return "U" if net >= th else "D" if net <= -th else "S"


def letters(x, n):
    """letters for every period finished within the first n bars, on the so-far scale x"""
    out = {}
    for name, a, c in PHASE_BUCKETS:
        if c <= n:
            out[name] = letter(x[c - 1] - (x[a - 1] if a > 0 else x[0]), 0.15)
    if n >= 186:
        out["Overnight"] = letter(x[185] - x[0], 0.25)
    return out


def match_asof(query_shape, n, lib, exclude=None, top=10):
    q, q_rng = prefix(query_shape, n)
    ql = letters(q, n)
    hard = {k: v for k, v in ql.items() if k in HARD or k == "Overnight"}
    res, eliminated = [], 0
    for b in lib:
        if b["date"] == exclude:
            continue
        c, c_rng = prefix(b["shape"], n)
        cl = letters(c, n)
        if any(cl.get(k) != v for k, v in hard.items()):
            eliminated += 1
            continue
        soft_miss = sum(1 for k in SOFT if k in ql and cl.get(k) != ql[k])
        res.append((float(np.sqrt(np.mean((c - q) ** 2))) + SOFT_PENALTY * soft_miss, b, c_rng, soft_miss))
    res.sort(key=lambda r: r[0])
    return ql, hard, eliminated, res[:top]


def match_full(b0, lib, top=10):
    fam = b0["session_family"]
    res = [(float(np.sqrt(np.mean((np.asarray(b["shape"]) - np.asarray(b0["shape"])) ** 2))), b) for b in lib
           if b["session_family"] == fam and b["date"] != b0["date"]]
    return sorted(res, key=lambda r: r[0])[:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--like"); ap.add_argument("--as-of"); ap.add_argument("--cat", nargs="*"); ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    lib = load()
    by = {b["date"]: b for b in lib}
    if a.cat:
        hits = [b for b in lib if all(t in b["categories"] for t in a.cat)]
        print(f"{len(hits)} BPs with {' + '.join(a.cat)}")
        for b in hits:
            print(f"  {b['date']} {b['weekday'][:3]}  session {b['session_type']:<14} rth {b['rth_type']:<14} {b['story']:<22} {b['phases']}")
        return
    b0 = by[a.like]
    if a.as_of:
        n = bars_done(a.as_of)
        ql, hard, elim, res = match_asof(b0["shape"], n, lib, exclude=b0["date"], top=a.top)
        print(f"query {b0['date']} as of {a.as_of} ({n} finished five-minute bars). Letters so far: {ql or 'none yet'}")
        print(f"eliminated {elim} of {len(lib) - 1} BPs on {hard or 'nothing yet'}; ranking the remaining {len(lib) - 1 - elim} by shape")
        rng0 = (max(b0["shape"][:n]) - min(b0["shape"][:n])) * b0["f"]["range"]
        print(f"query range so far {rng0:.0f} pts\n")
        print("  rank  date        score  soft-miss  range so far   rest of day (after as-of)   full-day type")
        for k, (d, b, crng, miss) in enumerate(res, 1):
            s = np.asarray(b["shape"]); move = (s[-1] - s[n - 1]) * b["f"]["range"]
            print(f"  {k:>4}  {b['date']}  {d:.3f}  {miss:>5}     {crng * b['f']['range']:>8.0f} pts   {move:+8.1f} pts to close       "
                  f"{b['session_type']} / RTH {b['rth_type']}")
        s0 = np.asarray(b0["shape"]); print(f"\n  actual {b0['date']}: {(s0[-1] - s0[n - 1]) * b0['f']['range']:+.1f} pts to close, "
                                            f"{b0['session_type']} / RTH {b0['rth_type']}")
    else:
        print(f"query {b0['date']} ({b0['session_type']}, family {b0['session_family']}) -- whole day, same family:")
        for k, (d, b) in enumerate(match_full(b0, lib, a.top), 1):
            print(f"  {k:>2}  {b['date']}  rmse {d:.3f}  {b['session_type']:<14} RTH {b['rth_type']:<14} {b['story']}")


if __name__ == "__main__":
    main()
