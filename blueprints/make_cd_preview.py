"""Preview ONE day page with the cumulative-delta line (shared zero with the delta-ratio bars).
   python make_cd_preview.py 2026-09-21  ->  blueprints/preview/BP_<date>_cumdelta_sharedzero.html"""
import json, os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bp_library import connect, fetch, LIB, HERE
from bp_core import build_bp, SESSION_MIN

d = sys.argv[1]
lib = sorted(f[3:13] for f in os.listdir(os.path.join(LIB, "bp")) if f.endswith(".json"))
prev = lib[lib.index(d) - 1]
own = json.load(open(os.path.join(LIB, "bp", f"BP_{d}.json")))
start = pd.Timestamp(own["meta"]["session_start"])
tail = json.load(open(os.path.join(LIB, "bp", f"BP_{prev}.json")))["_tail_1m"]
prior = pd.Series(tail["c"], index=pd.to_datetime(tail["t"]))
df = fetch(connect().cursor(), own["meta"]["contract"], start, start + pd.Timedelta(minutes=SESSION_MIN))
bp, chart, _ = build_bp(df, start, prior, pd.Timestamp(d).date(), own["meta"]["contract"])
label = pd.Timestamp(d).strftime("%a %b %d")
chart.update({"start": bp["meta"]["session_start"], "bp_offset": 0, "sessions": [{"m": 0, "label": label}],
              "desc": f"{own['meta']['contract']}, {pd.Timestamp(d).strftime('%a %b %d %Y')} (18:00 -> 16:30 ET) — cumulative delta, shared zero"})
f, s = bp["features"], bp["shape"]
page_bp = {"meta": {**bp["meta"], "high": f["high"], "low": f["low"], "range": f["range"], "open": f["open"], "close": f["close"],
                    "n_legs": len(bp["legs"]), "session": f"{bp['meta']['session_start']} -> {bp['meta']['session_end']} ET"},
           "legs": bp["legs"],
           "panels": {**{k: s[k] for k in ("vwap", "sma100", "delta_ratio", "mid_cross", "rel_volume", "cum_delta")}, "cum_delta_max_abs": s["cum_delta_max_abs"]}}
tpl = open(os.path.join(HERE, "bp_viewer_template.html"), encoding="utf-8").read()
out = os.path.join(HERE, "preview", f"BP_{d}_cumdelta_sharedzero.html")
open(out, "w", encoding="utf-8").write(tpl.replace("/*__BP__*/null", json.dumps(page_bp)).replace("/*__CHART__*/null", json.dumps(chart)))
import numpy as np
c = np.cumsum(chart["d"])
print(out, "| price", f["open"], "->", f["close"], f"({f['close'] - f['open']:+.2f})", "| cum delta end", int(c[-1]), "min", int(c.min()), "max", int(c.max()))
