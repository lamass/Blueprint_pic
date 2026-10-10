"""
Orchestrator for the intraday GEX dashboard: refetches NDX/SPX option data
and regenerates gex_dashboard.html in this same folder. Meant to be run
every 5 minutes by a scheduled task (see setup_gex_task_scheduler.ps1) --
running it by hand works the same way for a one-off refresh.

The dashboard page itself reloads every 60 seconds (see gex_dashboard_template.html)
so simply leaving it open in a browser tab picks up each new file this
script writes, without you doing anything.
"""

import json
import os
import sys
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TEMPLATE_PATH = os.path.join(HERE, "gex_dashboard_template.html")
DATA_PATH = os.path.join(HERE, "gex_dashboard_data.json")
OUT_PATH = os.path.join(HERE, "gex_dashboard.html")
LOG_PATH = os.path.join(HERE, "gex_refresh_log.txt")


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def build_html():
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    out = tpl.replace("__GEX_DATA__", json.dumps(data, separators=(",", ":")))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(out)
    return data


def write_history(data):
    # Logs this snapshot's GEX/OI to Postgres for later backtesting. Kept as
    # a separate try/except from the main refresh: a Postgres hiccup here
    # shouldn't be reported as a dashboard-refresh failure, since the
    # dashboard itself already refreshed fine by this point.
    try:
        import write_gex_oi_snapshot as writer
        conn = writer.get_conn()
        try:
            writer.ensure_schema(conn)
            writer.write_snapshot(conn, data)
        finally:
            conn.close()
    except Exception:
        log("GEX/OI history write FAILED (dashboard refresh itself was OK):\n" + traceback.format_exc())


def main():
    try:
        import schwab_gex_dashboard_data
        schwab_gex_dashboard_data.main()
        data = build_html()
        log(f"OK -- refreshed {OUT_PATH}")
    except Exception:
        log("FAILED:\n" + traceback.format_exc())
        raise

    write_history(data)


if __name__ == "__main__":
    main()
