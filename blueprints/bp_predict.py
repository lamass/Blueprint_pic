"""
Match % and prediction track record for the BP library.

MATCH %   = 100 * (1 - shape distance / typical distance between two random library days at the same as-of time),
            floored at 0. 100% = identical shape so far, 0% = no closer than two random days.
PREDICTION (from one matched BP, made at an as-of time on a target day):
  the matched BP's move from the as-of time to 16:30, expressed in its own range-so-far, applied to the target day's
  range-so-far -> predicted rest-of-day move in points. Direction: UP if >= +25% of the range so far, DOWN if <= -25%,
  else SIDEWAYS. The actual rest-of-day move is classified the same way. HIT = same direction.
Everything a prediction uses is known at the as-of time; matches come only from EARLIER days.

  python bp_predict.py              # walk-forward over the library at 09:30, 10:30, 12:00 (top 3 each), store + summarise
Writes: Postgres bp_predictions (one row per prediction), bp_library.pred_* columns (each BP's record as a match),
        library/predictions.json, and adds match % + track record to library_index.json / index.html.
"""
import json
import os
import sys

import numpy as np
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bp_query import prefix, match_asof, bars_done  # noqa: E402
from bp_library import connect, LIB, HERE  # noqa: E402

AS_OF = ["09:30", "10:30", "12:00"]
TOP = 3
MIN_HISTORY = 40          # a day is only predicted once the library has this many earlier days
DIR_TH = 0.25


def direction(move_frac):
    return "UP" if move_frac >= DIR_TH else "DOWN" if move_frac <= -DIR_TH else "SIDEWAYS"


def ref_rmse(lib, n, rng, pairs=4000):
    """typical (median) distance between two random days' shapes cut at n bars and rescaled to their range so far"""
    S = [prefix(b["shape"], n)[0] for b in lib]
    i, j = rng.integers(0, len(S), pairs), rng.integers(0, len(S), pairs)
    keep = i != j
    return float(np.median([np.sqrt(np.mean((S[a] - S[b]) ** 2)) for a, b in zip(i[keep], j[keep])]))


def match_pct(rmse, ref):
    return round(max(0.0, 100.0 * (1.0 - rmse / ref)), 1)


def predict_rows(lib, refs):
    rows = []
    for t_i, q in enumerate(lib):
        earlier = lib[:t_i]
        if len(earlier) < MIN_HISTORY:
            continue
        s_q = np.asarray(q["shape"]); R_q = q["f"]["range"]
        for as_of in AS_OF:
            n = bars_done(as_of)
            qp, q_rng = prefix(q["shape"], n)                       # q_rng: range so far, in full-range units
            q_rng_pts = q_rng * R_q
            actual_pts = (s_q[-1] - s_q[n - 1]) * R_q
            actual_dir = direction(actual_pts / max(q_rng_pts, 1e-9))
            _, _, _, res = match_asof(q["shape"], n, earlier, top=TOP)
            for rank, (score, b, c_rng, miss) in enumerate(res, 1):
                c, _ = prefix(b["shape"], n)
                pure = float(np.sqrt(np.mean((c - qp) ** 2)))
                s_b = np.asarray(b["shape"])
                frac = (s_b[-1] - s_b[n - 1]) / max(c_rng, 1e-9)    # BP's rest-of-day move in its range-so-far units
                pred_pts = frac * q_rng_pts
                pred_dir = direction(frac)
                rows.append(dict(mode="walkforward", target_date=q["date"], as_of=as_of, rank=rank, bp_date=b["date"],
                                 match_pct=match_pct(pure, refs[as_of]), score=round(score, 4),
                                 predicted_dir=pred_dir, predicted_move_pts=round(float(pred_pts), 2),
                                 actual_dir=actual_dir, actual_move_pts=round(float(actual_pts), 2),
                                 hit=pred_dir == actual_dir, error_pts=round(float(pred_pts - actual_pts), 2)))
    return rows


def store(conn, rows):
    cur = conn.cursor()
    cur.execute("""create table if not exists bp_predictions (
        id bigserial primary key, created_at timestamptz default now(), mode text not null,
        target_date date not null, as_of time not null, rank int not null, bp_date date not null,
        match_pct real, score real, predicted_dir text, predicted_move_pts real,
        actual_dir text, actual_move_pts real, hit boolean, error_pts real,
        unique (mode, target_date, as_of, rank))""")
    cur.execute("create index if not exists bp_predictions_bp on bp_predictions (bp_date)")
    cols = ["mode", "target_date", "as_of", "rank", "bp_date", "match_pct", "score", "predicted_dir", "predicted_move_pts",
            "actual_dir", "actual_move_pts", "hit", "error_pts"]
    execute_values(cur, f"""insert into bp_predictions ({','.join(cols)}) values %s
        on conflict (mode, target_date, as_of, rank) do update set {','.join(f'{c}=excluded.{c}' for c in cols[4:])}, created_at=now()""",
                   [tuple(r[c] for c in cols) for r in rows])
    # each BP's record when it was used as a match (live predictions count too, once they have an outcome)
    for col, typ in (("pred_times_used", "int"), ("pred_hits", "int"), ("pred_success_pct", "real"), ("pred_avg_match_pct", "real")):
        cur.execute(f"alter table bp_library add column if not exists {col} {typ}")
    cur.execute("""update bp_library l set pred_times_used = s.n, pred_hits = s.h, pred_success_pct = s.pct, pred_avg_match_pct = s.mp
        from (select bp_date, count(*) n, sum(hit::int) h, round(100.0 * avg(hit::int), 1) pct, round(avg(match_pct)::numeric, 1) mp
              from bp_predictions where hit is not null group by bp_date) s where l.trade_date = s.bp_date""")
    conn.commit()


def summarise(rows):
    import collections
    print(f"{len(rows)} predictions on {len({r['target_date'] for r in rows})} days ({rows[0]['target_date']} -> {rows[-1]['target_date']})")
    base = collections.Counter((r["as_of"], r["actual_dir"]) for r in rows if r["rank"] == 1)
    for as_of in AS_OF:
        rs = [r for r in rows if r["as_of"] == as_of]
        days = len({r["target_date"] for r in rs})
        mix = {d: round(100 * base[(as_of, d)] / days) for d in ("UP", "DOWN", "SIDEWAYS")}
        line = "  ".join(f"#{k} {100 * np.mean([r['hit'] for r in rs if r['rank'] == k]):.0f}%" for k in (1, 2, 3))
        print(f"  as of {as_of}: hit rate {line} | actual rest-of-day mix {mix} (most common class = {max(mix.values())}% baseline)")
    hi = [r for r in rows if r["match_pct"] >= 70]; lo = [r for r in rows if r["match_pct"] < 50]
    print(f"  match % >= 70: {len(hi)} predictions, hit {100 * np.mean([r['hit'] for r in hi]):.0f}%  |  "
          f"match % < 50: {len(lo)} predictions, hit {100 * np.mean([r['hit'] for r in lo]) if lo else float('nan'):.0f}%")


def patch_index(rows, ref_full):
    path = os.path.join(LIB, "library_index.json")
    data = json.load(open(path))
    rec = {}
    for r in rows:
        x = rec.setdefault(r["bp_date"], {"used": 0, "hits": 0, "mp": []})
        x["used"] += 1; x["hits"] += int(r["hit"]); x["mp"].append(r["match_pct"])
    made = {}
    for r in rows:
        made.setdefault(r["target_date"], []).append({k: r[k] for k in ("as_of", "rank", "bp_date", "match_pct", "predicted_dir",
                                                                         "predicted_move_pts", "actual_dir", "actual_move_pts", "hit")})
    for b in data["bps"]:
        x = rec.get(b["date"])
        b["track"] = {"used": x["used"], "hits": x["hits"], "success_pct": round(100 * x["hits"] / x["used"], 1),
                      "avg_match_pct": round(float(np.mean(x["mp"])), 1)} if x else {"used": 0, "hits": 0, "success_pct": None, "avg_match_pct": None}
        b["predictions"] = made.get(b["date"], [])
        for nb in b["neighbours"]["session"] + b["neighbours"]["rth"]:
            nb["match_pct"] = match_pct(nb["rmse"], ref_full)
    data["match_pct_ref"] = {"full_day": round(ref_full, 4)}
    json.dump(data, open(path, "w"))
    tpl = open(os.path.join(HERE, "library_template.html"), encoding="utf-8").read()
    open(os.path.join(LIB, "index.html"), "w", encoding="utf-8").write(tpl.replace("/*__LIB__*/null", json.dumps(data)))
    json.dump({"refs": {"full_day": ref_full}, "track": {d: v for d, v in ((b["date"], b["track"]) for b in data["bps"])},
               "predictions": rows}, open(os.path.join(LIB, "predictions.json"), "w"))


def main():
    lib = sorted(json.load(open(os.path.join(LIB, "library_index.json")))["bps"], key=lambda b: b["date"])
    rng = np.random.default_rng(7)
    refs = {a: ref_rmse(lib, bars_done(a), rng) for a in AS_OF}
    ref_full = ref_rmse(lib, len(lib[0]["shape"]), rng)
    print("typical random-pair distance:", {k: round(v, 3) for k, v in refs.items()}, "full day", round(ref_full, 3))
    rows = predict_rows(lib, refs)
    store(connect(), rows)
    summarise(rows)
    patch_index(rows, ref_full)
    print("stored in Postgres bp_predictions; track record added to library_index.json / index.html / predictions.json")


if __name__ == "__main__":
    main()
