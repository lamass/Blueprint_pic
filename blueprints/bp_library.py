"""
Build the blueprint LIBRARY: one BP per NQ session (18:00 -> 16:30 ET) for every full session in Postgres ticks_nq.

  python bp_library.py            # build missing days, then categorise + write outputs
  python bp_library.py --force    # rebuild every day

Outputs (blueprints/library/):
  bp/BP_<date>.json      full BP: meta, features, skeleton, 30-pt legs, normalised shapes
  view/BP_<date>.html    overlay page for that day (drag/opacity/crosshair/axes, same as BP v2)
  library_index.json     compact index: categories + key features + shapes + nearest neighbours
  index.html             review page (filter by category, sort, open any BP)
  build_log.txt          which sessions were built / skipped and why
Postgres: table bp_library (one row per BP, categories as text[] with a GIN index for fast filtering).

CATEGORY RULES (a BP gets many tags at once; thresholds are first proposals, all in one place below)
  Direction, whole session and RTH (09:30-16:30) separately -- net = (close - open) / range of that period:
    DECISIVE_UP >= +0.60 | UP >= +0.30 | DOWN <= -0.30 | DECISIVE_DOWN <= -0.60
    otherwise SIDEWAYS if the range is small vs the prior 20 sessions (< 0.85x), else MIXED (big but two-sided)
    family: UP (UP, DECISIVE_UP) / DOWN / NEUTRAL (SIDEWAYS, MIXED) -- matching only searches inside a family
  Story: overnight (18:00-09:30) and RTH each U / D / S (net >= +-0.25 of session range) -> e.g. SIDEWAYS_THEN_UP
  Phases: Asia, London, NY pre, NY open, NY midday, NY close each U / D / S (+-0.15 of range) -> NYOPEN_UP etc.
  RTH V reversals: RTH_V_REVERSAL_UP (falls >= 40% of RTH range from the open first, closes in top 30%) and mirror
  Opening range (09:30-10:00): OR_BREAK_UP_FIRST / OR_BREAK_DOWN_FIRST / OR_NO_BREAK
  RTH open vs overnight range: OPEN_ABOVE_ON_RANGE / OPEN_UPPER_ON / OPEN_MID_ON / OPEN_LOWER_ON / OPEN_BELOW_ON_RANGE
  Where the high / low was made: HIGH_IN_<phase>, LOW_IN_<phase>
  Library-relative thirds (bottom / top third of all days):
    VOL_LOW / VOL_HIGH            range vs prior 20 sessions
    VOLUME_LOW / VOLUME_HIGH      volume vs prior 20 sessions
    CHOPPY / CLEAN_LEGS           range / total 30-pt leg travel
    RETRACE_SHALLOW / RETRACE_DEEP  median Fib ratio of the 30-pt legs
    SWIFT_LEGS / DENSE_ROTATION   median mid-point crosses per minute inside legs
    DELTA_SELL_HEAVY / DELTA_BUY_HEAVY  RTH delta ratio
  DELTA_DIVERGES: RTH moved >= 30% of its range one way while RTH delta leaned the other way
  RTH_ABOVE_VWAP (>= 80% of RTH time) / RTH_BELOW_VWAP (<= 20%) / VWAP_CHOP (top third of VWAP crosses)
  Weekday: MON..FRI
"""
import io
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bp_core import build_bp, PHASES, RTH_START, SESSION_MIN  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "library")
MIN_TRADES = 200_000
TZ = "America/New_York"


def connect():
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, "..", ".env")); pw = os.environ.get("POSTGRES_PASSWORD")
    return psycopg2.connect(host="localhost", dbname="market_data", user="postgres", password=pw)


def session_list(cur):
    cache = os.path.join(LIB, "_sessions.csv")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
        return pd.read_csv(cache, parse_dates=["trade_date"])
    cur.execute("""select instrument, (ts_utc at time zone 'America/New_York' + interval '6 hours')::date d, count(*)
                   from ticks_nq group by 1, 2""")
    df = pd.DataFrame(cur.fetchall(), columns=["instrument", "trade_date", "n"])
    df["trade_date"] = pd.to_datetime(df.trade_date)
    df.to_csv(cache, index=False)
    return df


def fetch(cur, instrument, start, end):
    s = pd.Timestamp(start).tz_localize(TZ).tz_convert("UTC"); e = pd.Timestamp(end).tz_localize(TZ).tz_convert("UTC")
    buf = io.StringIO()
    cur.copy_expert(cur.mogrify("COPY (select ts_utc, price, volume, bid, ask from ticks_nq where instrument=%s and ts_utc >= %s "
                                "and ts_utc < %s order by ts_utc, id) TO STDOUT WITH CSV", (instrument, s.to_pydatetime(), e.to_pydatetime())).decode(), buf)
    buf.seek(0)
    df = pd.read_csv(buf, header=None, names=["ts", "price", "vol", "bid", "ask"])
    df.index = pd.to_datetime(df.pop("ts"), utc=True, format="ISO8601").dt.tz_convert(TZ).dt.tz_localize(None)
    return df


def build_all(force=False):
    os.makedirs(os.path.join(LIB, "bp"), exist_ok=True); os.makedirs(os.path.join(LIB, "view"), exist_ok=True)
    tpl = open(os.path.join(HERE, "bp_viewer_template.html"), encoding="utf-8").read()
    conn = connect(); cur = conn.cursor()
    days = session_list(cur)
    best = days.sort_values("n", ascending=False).drop_duplicates("trade_date").sort_values("trade_date")
    log = []
    prior = pd.Series(dtype=float)
    built = 0
    for _, r in best.iterrows():
        d = r.trade_date.date()
        if r.trade_date.weekday() >= 5:
            continue
        if r.n < MIN_TRADES:
            log.append(f"{d} SKIP {r.instrument}: only {r.n:,} trades (holiday / partial session / roll leftover)"); continue
        start = pd.Timestamp(d) - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
        end = start + pd.Timedelta(minutes=SESSION_MIN)
        jpath = os.path.join(LIB, "bp", f"BP_{d}.json")
        if os.path.exists(jpath) and not force:
            bp = json.load(open(jpath))
            tail = bp.get("_tail_1m")
            if tail:
                prior = pd.Series(tail["c"], index=pd.to_datetime(tail["t"]))
            log.append(f"{d} cached {r.instrument}"); continue
        t0 = time.time()
        df = fetch(cur, r.instrument, start, end)
        if len(df) < MIN_TRADES or df.index[0] > start + pd.Timedelta(minutes=15) or df.index[-1] < end - pd.Timedelta(minutes=10):
            log.append(f"{d} SKIP {r.instrument}: incomplete session ({len(df):,} trades, {df.index[0] if len(df) else '-'} -> "
                       f"{df.index[-1] if len(df) else '-'})"); continue
        bp, chart, tail = build_bp(df, start, prior.iloc[-300:] if len(prior) else pd.Series(dtype=float), d, r.instrument)
        prior = tail
        bp["_tail_1m"] = {"t": [x.strftime("%Y-%m-%d %H:%M") for x in tail.index], "c": [round(float(x), 2) for x in tail.values]}
        json.dump(bp, open(jpath, "w"))
        # overlay page for this day (one session as background)
        chart.update({"start": bp["meta"]["session_start"], "bp_offset": 0,
                      "sessions": [{"m": 0, "label": pd.Timestamp(d).strftime("%a %b %d")}],
                      "desc": f"{r.instrument}, {pd.Timestamp(d).strftime('%a %b %d %Y')} (18:00 -> 16:30 ET)"})
        page_bp = {"meta": {**bp["meta"], "high": bp["features"]["high"], "low": bp["features"]["low"], "range": bp["features"]["range"],
                            "open": bp["features"]["open"], "close": bp["features"]["close"], "n_legs": len(bp["legs"]),
                            "session": f"{bp['meta']['session_start']} -> {bp['meta']['session_end']} ET"},
                   "legs": bp["legs"],
                   "panels": {k: bp["shape"][k] for k in ("vwap", "sma100", "delta_ratio", "mid_cross", "rel_volume")}}
        html = tpl.replace("/*__BP__*/null", json.dumps(page_bp)).replace("/*__CHART__*/null", json.dumps(chart))
        open(os.path.join(LIB, "view", f"BP_{d}.html"), "w", encoding="utf-8").write(html)
        built += 1
        log.append(f"{d} built {r.instrument}: {len(df):,} trades, {len(bp['legs'])} legs, range {bp['features']['range']:.2f} ({time.time()-t0:.1f}s)")
        print(log[-1], flush=True)
    open(os.path.join(LIB, "build_log.txt"), "w").write("\n".join(log))
    print(f"built {built} new BPs")
    return conn


# --------------------------------------------------------------------------- categories
def letter(x, th):
    return "U" if x >= th else "D" if x <= -th else "S"


WORD = {"U": "UP", "D": "DOWN", "S": "SIDEWAYS"}


def direction(net, range_rel):
    if net >= 0.6: return "DECISIVE_UP"
    if net >= 0.3: return "UP"
    if net <= -0.6: return "DECISIVE_DOWN"
    if net <= -0.3: return "DOWN"
    return "SIDEWAYS" if range_rel < 0.85 else "MIXED"


FAMILY = {"DECISIVE_UP": "UP", "UP": "UP", "DECISIVE_DOWN": "DOWN", "DOWN": "DOWN", "SIDEWAYS": "NEUTRAL", "MIXED": "NEUTRAL"}


def phase_of(m):
    for name, a, c in PHASES:
        if a <= m < c:
            return name.upper().replace(" ", "_")
    return "NY_CLOSE"


def categorise(bps):
    bps = sorted(bps, key=lambda b: b["meta"]["trade_date"])
    F = pd.DataFrame([{"date": b["meta"]["trade_date"], "range": b["features"]["range"], "rth_range": b["features"]["rth"]["range"],
                       "volume": b["features"]["volume"]} for b in bps])
    prev = lambda s: s.shift(1).rolling(20, min_periods=3).mean()
    F["range_rel"] = (F.range / prev(F.range)).fillna(1.0)
    F["rth_range_rel"] = (F.rth_range / prev(F.rth_range)).fillna(1.0)
    F["vol_rel"] = (F.volume / prev(F.volume)).fillna(1.0)
    for b, (_, f) in zip(bps, F.iterrows()):
        b["features"]["range_rel"] = round(float(f.range_rel), 3)
        b["features"]["rth_range_rel"] = round(float(f.rth_range_rel), 3)
        b["features"]["vol_rel"] = round(float(f.vol_rel), 3)

    def thirds(key, get):
        vals = np.array([get(b) for b in bps], float)
        lo, hi = np.nanpercentile(vals, [33.3, 66.7])
        return vals, lo, hi

    tert = {
        "VOL": thirds("range_rel", lambda b: b["features"]["range_rel"]),
        "VOLUME": thirds("vol_rel", lambda b: b["features"]["vol_rel"]),
        "CHOP": thirds("zz", lambda b: b["features"]["zz_efficiency"]),
        "RETRACE": thirds("fib", lambda b: b["features"]["median_fib"] if b["features"]["median_fib"] is not None else np.nan),
        "DENSE": thirds("cross", lambda b: b["features"]["median_leg_crosses_per_min"]),
        "DELTA": thirds("delta", lambda b: b["features"]["rth_delta_ratio"]),
        "VWAPX": thirds("vwapx", lambda b: b["features"]["rth_vwap_crosses"]),
    }
    abs_delta_med = np.median([abs(b["features"]["rth_delta_ratio"]) for b in bps])
    thresholds = {k: {"low_third_below": round(float(v[1]), 4), "high_third_above": round(float(v[2]), 4)} for k, v in tert.items()}

    for i, b in enumerate(bps):
        f = b["features"]; r = f["rth"]
        s_type = direction(f["net"], f["range_rel"])
        r_type = direction(r["net"], f["rth_range_rel"])
        on_l = letter(f["on"]["net"], 0.25)
        rth_net_sess = (r["close"] - r["open"]) / f["range"]
        rth_l = letter(rth_net_sess, 0.25)
        ph = [letter(p["net"], 0.15) for p in f["phases"]]
        tags = [f"SESSION_{s_type}", f"RTH_{r_type}", f"SESSION_FAMILY_{FAMILY[s_type]}", f"RTH_FAMILY_{FAMILY[r_type]}",
                f"{WORD[on_l]}_THEN_{WORD[rth_l]}", f"OVERNIGHT_{WORD[on_l]}"]
        for (name, _, _), l in zip(PHASES, ph):
            tags.append(f"{name.upper().replace(' ', '')}_{WORD[l]}")
        if (r["open"] - r["low"]) / r["range"] >= 0.4 and r["t_low"] < r["t_high"] and r["close_pos"] >= 0.7:
            tags.append("RTH_V_REVERSAL_UP")
        if (r["high"] - r["open"]) / r["range"] >= 0.4 and r["t_high"] < r["t_low"] and r["close_pos"] <= 0.3:
            tags.append("RTH_V_REVERSAL_DOWN")
        tags.append({"up": "OR_BREAK_UP_FIRST", "down": "OR_BREAK_DOWN_FIRST", "none": "OR_NO_BREAK"}[f["or_break_first"]])
        o = f["rth_open_in_on_range"]
        tags.append("OPEN_ABOVE_ON_RANGE" if o > 1 else "OPEN_BELOW_ON_RANGE" if o < 0 else
                    "OPEN_UPPER_ON" if o >= 2 / 3 else "OPEN_LOWER_ON" if o < 1 / 3 else "OPEN_MID_ON")
        tags += [f"HIGH_IN_{phase_of(f['t_high'])}", f"LOW_IN_{phase_of(f['t_low'])}"]
        for key, (lo_tag, hi_tag) in {"VOL": ("VOL_LOW", "VOL_HIGH"), "VOLUME": ("VOLUME_LOW", "VOLUME_HIGH"),
                                      "CHOP": ("CHOPPY", "CLEAN_LEGS"), "RETRACE": ("RETRACE_SHALLOW", "RETRACE_DEEP"),
                                      "DENSE": ("SWIFT_LEGS", "DENSE_ROTATION"), "DELTA": ("DELTA_SELL_HEAVY", "DELTA_BUY_HEAVY"),
                                      "VWAPX": (None, "VWAP_CHOP")}.items():
            vals, lo, hi = tert[key]
            x = vals[i]
            if np.isfinite(x):
                if lo_tag and x <= lo: tags.append(lo_tag)
                if x >= hi: tags.append(hi_tag)
        if abs(r["net"]) >= 0.3 and np.sign(r["net"]) != np.sign(f["rth_delta_ratio"]) and abs(f["rth_delta_ratio"]) >= abs_delta_med:
            tags.append("DELTA_DIVERGES")
        if f["rth_pct_above_vwap"] >= 80: tags.append("RTH_ABOVE_VWAP")
        if f["rth_pct_above_vwap"] <= 20: tags.append("RTH_BELOW_VWAP")
        tags.append(b["meta"]["weekday"][:3].upper())
        b["class"] = {"session_type": s_type, "rth_type": r_type, "session_family": FAMILY[s_type], "rth_family": FAMILY[r_type],
                      "story": f"{WORD[on_l]}_THEN_{WORD[rth_l]}", "phases": "-".join(ph), "categories": tags}
    return bps, thresholds


# --------------------------------------------------------------------------- similarity
def neighbours(bps, k=5):
    S = np.array([b["shape"]["session"] for b in bps], float)
    R = np.array([b["shape"]["rth"] for b in bps], float)
    for i, b in enumerate(bps):
        out = {}
        for name, M, fam_key in (("session", S, "session_family"), ("rth", R, "rth_family")):
            same = np.array([x["class"][fam_key] == b["class"][fam_key] for x in bps]); same[i] = False
            d = np.sqrt(np.mean((M - M[i]) ** 2, axis=1)); d[~same] = np.inf
            idx = [j for j in np.argsort(d)[:k] if np.isfinite(d[j])]
            out[name] = [{"date": bps[j]["meta"]["trade_date"], "rmse": round(float(d[j]), 4)} for j in idx]
        b["neighbours"] = out


# --------------------------------------------------------------------------- outputs
def write_postgres(conn, bps):
    cur = conn.cursor()
    cur.execute("""create table if not exists bp_library (
        trade_date date primary key, contract text, session_start timestamp, session_end timestamp,
        session_type text, rth_type text, session_family text, rth_family text, story text, phases text,
        categories text[], features jsonb, shape_session real[], shape_rth real[], skeleton jsonb, legs jsonb,
        neighbours jsonb, json_path text, view_path text, built_at timestamptz default now())""")
    cur.execute("create index if not exists bp_library_categories on bp_library using gin (categories)")
    cur.execute("create index if not exists bp_library_types on bp_library (session_family, rth_family, session_type, rth_type)")
    rows = []
    for b in bps:
        m, c = b["meta"], b["class"]
        rows.append((m["trade_date"], m["contract"], m["session_start"], m["session_end"], c["session_type"], c["rth_type"],
                     c["session_family"], c["rth_family"], c["story"], c["phases"], c["categories"], json.dumps(b["features"]),
                     b["shape"]["session"], b["shape"]["rth"], json.dumps(b["skeleton"]), json.dumps(b["legs"]),
                     json.dumps(b["neighbours"]), f"blueprints/library/bp/BP_{m['trade_date']}.json",
                     f"blueprints/library/view/BP_{m['trade_date']}.html"))
    execute_values(cur, """insert into bp_library (trade_date, contract, session_start, session_end, session_type, rth_type,
        session_family, rth_family, story, phases, categories, features, shape_session, shape_rth, skeleton, legs, neighbours,
        json_path, view_path) values %s on conflict (trade_date) do update set contract=excluded.contract,
        session_start=excluded.session_start, session_end=excluded.session_end, session_type=excluded.session_type,
        rth_type=excluded.rth_type, session_family=excluded.session_family, rth_family=excluded.rth_family, story=excluded.story,
        phases=excluded.phases, categories=excluded.categories, features=excluded.features, shape_session=excluded.shape_session,
        shape_rth=excluded.shape_rth, skeleton=excluded.skeleton, legs=excluded.legs, neighbours=excluded.neighbours,
        json_path=excluded.json_path, view_path=excluded.view_path, built_at=now()""", rows)
    conn.commit()


def write_index(bps, thresholds):
    keep = ["range", "net_pts", "net", "open_pos", "close_pos", "range_rel", "rth_range_rel", "vol_rel", "volume", "zz_efficiency",
            "median_fib", "median_leg_crosses_per_min", "rth_delta_ratio", "delta_ratio", "rth_pct_above_vwap", "rth_vwap_crosses",
            "n_legs", "t_high", "t_low", "or_break_first", "rth_open_in_on_range", "high", "low", "open", "close"]
    idx = []
    for b in bps:
        f = b["features"]
        idx.append({"date": b["meta"]["trade_date"], "weekday": b["meta"]["weekday"], "contract": b["meta"]["contract"],
                    **b["class"], "f": {k: f[k] for k in keep}, "rth": f["rth"], "phase_nets": [p["net"] for p in f["phases"]],
                    "shape": [round(x, 3) for x in b["shape"]["session"]], "skeleton": b["skeleton"], "neighbours": b["neighbours"]})
    data = {"built": time.strftime("%Y-%m-%d %H:%M"), "n": len(idx), "thresholds": thresholds, "bps": idx}
    json.dump(data, open(os.path.join(LIB, "library_index.json"), "w"))
    tpl = open(os.path.join(HERE, "library_template.html"), encoding="utf-8").read()
    open(os.path.join(LIB, "index.html"), "w", encoding="utf-8").write(tpl.replace("/*__LIB__*/null", json.dumps(data)))


def main():
    force = "--force" in sys.argv
    conn = build_all(force)
    bps = [json.load(open(os.path.join(LIB, "bp", f))) for f in sorted(os.listdir(os.path.join(LIB, "bp"))) if f.endswith(".json")]
    for b in bps:
        b.pop("_tail_1m", None)
    bps, thresholds = categorise(bps)
    neighbours(bps)
    write_postgres(conn, bps)
    write_index(bps, thresholds)
    print(f"library: {len(bps)} BPs -> {os.path.join(LIB, 'index.html')} and Postgres table bp_library")


if __name__ == "__main__":
    main()
